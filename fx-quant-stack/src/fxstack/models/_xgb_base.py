from __future__ import annotations

import json
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import (
    artifact_io_locked,
    stamp_artifact_payload_digest,
    validate_artifact_contract,
)
from fxstack.models.base import ModelBase
from fxstack.models._xgb_runtime import (
    build_xgb_runtime,
    fit_xgb_estimator,
    normalize_sample_weight,
    pin_xgb_cpu_inference,
    predict_xgb_probabilities,
    probe_xgb_cuda_capability,
    record_xgb_fit_runtime,
)
from fxstack.settings import get_settings
from fxstack.training.calibration import ProbabilityCalibrator


class XGBBinaryModel(ModelBase):
    name = "xgb_binary"

    def __init__(self, *, params: dict | None = None) -> None:
        s = get_settings()
        p = dict(params or {})
        p.setdefault("objective", "binary:logistic")
        p.setdefault("n_estimators", 300)
        p.setdefault("max_depth", 4)
        p.setdefault("learning_rate", 0.05)
        p.setdefault("subsample", 0.9)
        p.setdefault("colsample_bytree", 0.9)
        p.setdefault("random_state", 7)
        p.setdefault("use_calibration", True)
        p.setdefault("calibration_fraction", 0.20)
        p.setdefault("calibration_min_rows", 64)
        p.setdefault("calibration_embargo_rows", 24)

        requested_device = p.pop("device", s.xgb_device)
        tree_method = (
            str(p.pop("tree_method", s.xgb_tree_method) or "hist").strip().lower()
            or "hist"
        )
        allow_cpu_fallback = p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback)

        self.use_calibration = bool(p.pop("use_calibration", True))
        self.calibration_fraction = min(
            0.5, max(0.05, float(p.pop("calibration_fraction", 0.20)))
        )
        self.calibration_min_rows = max(16, int(p.pop("calibration_min_rows", 64)))
        self.calibration_embargo_rows = max(
            0, int(p.pop("calibration_embargo_rows", 24))
        )
        self.params = p
        self.runtime = build_xgb_runtime(
            requested_device=requested_device,
            tree_method=tree_method,
            allow_cpu_fallback=allow_cpu_fallback,
            cuda_probe=probe_xgb_cuda_capability,
        )
        self.model_params = dict(self.params)
        self.model_params.setdefault("tree_method", tree_method)
        self.model_params["device"] = str(self.runtime["selected_device"])
        self.model = xgb.XGBClassifier(**self.model_params)
        self.calibrator: ProbabilityCalibrator | None = None
        self.calibration_diagnostics: dict[str, object] = {
            "method": "disabled"
            if not self.use_calibration
            else "chronological_holdout_calibration_v2",
            "fitted": False,
        }
        self.feature_columns: list[str] = []

    def _prepare_X(self, X: pd.DataFrame) -> pd.DataFrame:
        x_in = X.copy()
        if self.feature_columns:
            missing = [c for c in self.feature_columns if c not in x_in.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            x_in = x_in[self.feature_columns]
        return x_in.astype(float)

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        if y is None:
            raise ValueError("y is required for XGBBinaryModel")
        self.feature_columns = list(X.columns)
        x_num = self._prepare_X(X)
        y_num = y.astype(int)
        sample_weight_num = normalize_sample_weight(sample_weight, index=X.index)

        def _fit_model(
            x_fit: pd.DataFrame,
            y_fit: pd.Series,
            weight_fit: np.ndarray | None,
        ) -> tuple[xgb.XGBClassifier, str, bool, str]:
            fit_kwargs: dict[str, object] = {}
            if weight_fit is not None:
                fit_kwargs["sample_weight"] = weight_fit
            return fit_xgb_estimator(
                xgb.XGBClassifier,
                model_params=self.model_params,
                X=x_fit,
                y=y_fit,
                fit_kwargs=fit_kwargs,
                selected_device=self.runtime.get("selected_device", "cpu"),
                allow_cpu_fallback=self.runtime.get("allow_cpu_fallback", True),
            )

        self.calibrator = None
        if bool(self.use_calibration):
            row_count = int(len(x_num))
            calibration_rows = max(
                int(self.calibration_min_rows),
                int(np.ceil(float(row_count) * float(self.calibration_fraction))),
            )
            calibration_start = int(row_count - calibration_rows)
            fit_end = int(calibration_start - int(self.calibration_embargo_rows))
            split_valid = bool(
                calibration_start > 0
                and fit_end >= int(self.calibration_min_rows)
                and int(y_num.iloc[:fit_end].nunique()) >= 2
                and int(y_num.iloc[calibration_start:].nunique()) >= 2
            )
            self.calibration_diagnostics = {
                "method": "chronological_holdout_calibration_v2",
                "fitted": False,
                "rows": row_count,
                "fit_rows": max(0, fit_end),
                "embargo_rows": int(self.calibration_embargo_rows),
                "calibration_rows": max(0, row_count - calibration_start),
                "calibration_start": max(0, calibration_start),
                "reason": "split_invalid" if not split_valid else "",
            }
            if split_valid:
                preliminary_weight = (
                    sample_weight_num[:fit_end]
                    if sample_weight_num is not None
                    else None
                )
                preliminary, preliminary_device, _, _ = _fit_model(
                    x_num.iloc[:fit_end],
                    y_num.iloc[:fit_end],
                    preliminary_weight,
                )
                raw_calibration = predict_xgb_probabilities(
                    preliminary,
                    x_num.iloc[calibration_start:],
                    device=preliminary_device,
                )[:, 1]
                cal = ProbabilityCalibrator()
                cal.fit(raw_calibration, y_num.iloc[calibration_start:].to_numpy())
                self.calibrator = cal
                self.calibration_diagnostics.update(
                    {
                        "fitted": True,
                        "calibrator_method": str(cal.method),
                        "reason": "",
                        "raw_probability_min": float(np.min(raw_calibration)),
                        "raw_probability_max": float(np.max(raw_calibration)),
                        "raw_probability_unique": int(np.unique(raw_calibration).size),
                        "calibration_positive_share": float(
                            y_num.iloc[calibration_start:].mean()
                        ),
                    }
                )

        fitted_model, used_device, fallback_used, fallback_reason = _fit_model(
            x_num,
            y_num,
            sample_weight_num,
        )
        self.model = fitted_model
        record_xgb_fit_runtime(
            self.runtime,
            used_device=used_device,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def predict(self, X: pd.DataFrame) -> pd.Series:
        probabilities = predict_xgb_probabilities(
            self.model,
            self._prepare_X(X),
            device=self.runtime.get(
                "inference_device", self.runtime.get("used_device", "cpu")
            ),
        )
        return pd.Series((probabilities[:, 1] >= 0.5).astype(int), index=X.index)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        p = predict_xgb_probabilities(
            self.model,
            self._prepare_X(X),
            device=self.runtime.get(
                "inference_device", self.runtime.get("used_device", "cpu")
            ),
        )
        p1 = pd.Series(p[:, 1], index=X.index).astype(float).to_numpy()
        if self.calibrator is not None:
            p1 = self.calibrator.transform(p1)
        p1s = pd.Series(p1, index=X.index).clip(lower=0.0, upper=1.0)
        return pd.DataFrame({"p0": 1.0 - p1s, "p1": p1s}, index=X.index)

    @artifact_io_locked
    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path / "model.json"))
        (path / "meta.json").write_text(
            json.dumps(
                {
                    "name": self.name,
                    **feature_contract_metadata(),
                    "params": self.params,
                    "runtime": self.runtime,
                    "use_calibration": bool(self.use_calibration),
                    "has_calibrator": self.calibrator is not None,
                    "calibration_config": {
                        "fraction": float(self.calibration_fraction),
                        "min_rows": int(self.calibration_min_rows),
                        "embargo_rows": int(self.calibration_embargo_rows),
                    },
                    "calibration_diagnostics": dict(self.calibration_diagnostics),
                    "feature_columns": list(self.feature_columns),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if self.calibrator is not None:
            joblib.dump(self.calibrator, path / "calibrator.joblib")
        else:
            (path / "calibrator.joblib").unlink(missing_ok=True)
        stamp_artifact_payload_digest(path)

    @classmethod
    @artifact_io_locked
    def load(cls, path: Path) -> "XGBBinaryModel":
        meta = validate_artifact_contract(
            path, label=str(path), expected_name=str(cls.name)
        )
        params = dict(meta.get("params", {}) or {})
        params["use_calibration"] = bool(meta.get("use_calibration", True))
        calibration_config = dict(meta.get("calibration_config") or {})
        if calibration_config:
            params["calibration_fraction"] = float(
                calibration_config.get("fraction", 0.20)
            )
            params["calibration_min_rows"] = int(calibration_config.get("min_rows", 64))
            params["calibration_embargo_rows"] = int(
                calibration_config.get("embargo_rows", 24)
            )
        params["device"] = "cpu"
        params["allow_cpu_fallback"] = True
        obj = cls(params=params)
        obj.model.load_model(str(path / "model.json"))
        pin_xgb_cpu_inference(obj.model)
        rt = dict(meta.get("runtime") or {})
        if rt:
            obj.runtime = {
                **obj.runtime,
                **rt,
                "requested_device": str(
                    rt.get(
                        "requested_device", obj.runtime.get("requested_device", "cpu")
                    )
                ),
                "used_device": str(
                    rt.get("used_device", rt.get("selected_device", "cpu"))
                ),
                "inference_device": "cpu",
            }
        obj.feature_columns = list(meta.get("feature_columns") or [])
        obj.calibration_diagnostics = dict(
            meta.get("calibration_diagnostics") or obj.calibration_diagnostics
        )
        if not obj.feature_columns:
            try:
                booster = obj.model.get_booster()
                booster_feature_columns = list(
                    getattr(booster, "feature_names", None) or []
                )
            except Exception:
                booster_feature_columns = []
            if booster_feature_columns:
                obj.feature_columns = booster_feature_columns
        if bool(meta.get("has_calibrator", False)):
            cp = path / "calibrator.joblib"
            if cp.exists():
                obj.calibrator = joblib.load(cp)
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj
