from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import xgboost as xgb

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import (
    artifact_io_locked,
    stamp_artifact_payload_digest,
    validate_artifact_contract,
)
from fxstack.models._xgb_runtime import (
    build_xgb_runtime,
    fit_xgb_estimator,
    normalize_sample_weight,
    pin_xgb_cpu_inference,
    predict_xgb_probabilities,
    probe_xgb_cuda_capability,
)
from fxstack.models.base import ModelBase
from fxstack.training.calibration import ProbabilityCalibrator, build_time_ordered_calibration_split


class XGBMulticlassModel(ModelBase):
    name = "xgb_multiclass"

    def __init__(self, *, params: dict | None = None) -> None:
        from fxstack.settings import get_settings

        s = get_settings()
        p = dict(params or {})
        self.classes_: list[int] = list(p.pop("classes", []))
        p.setdefault("objective", "multi:softprob")
        p.setdefault("n_estimators", 350)
        p.setdefault("max_depth", 4)
        p.setdefault("learning_rate", 0.05)
        p.setdefault("subsample", 0.9)
        p.setdefault("colsample_bytree", 0.9)
        p.setdefault("random_state", 7)
        p.setdefault("use_calibration", True)
        p.setdefault("calibration_fraction", 0.2)
        p.setdefault("calibration_min_fit_rows", 64)
        p.setdefault("calibration_min_rows", 32)

        requested_device = p.pop("device", s.xgb_device)
        tree_method = (
            str(p.pop("tree_method", s.xgb_tree_method) or "hist").strip().lower()
            or "hist"
        )
        allow_cpu_fallback = p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback)

        self.use_calibration = bool(p.pop("use_calibration", True))
        self.calibration_fraction = float(max(0.05, min(0.5, p.pop("calibration_fraction", 0.2))))
        self.calibration_min_fit_rows = int(max(1, p.pop("calibration_min_fit_rows", 64)))
        self.calibration_min_rows = int(max(1, p.pop("calibration_min_rows", 32)))
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
        self.calibrators: dict[int, ProbabilityCalibrator] = {}
        self.calibration_provenance: dict[str, Any] = {
            "enabled": bool(self.use_calibration),
            "status": "not_fitted",
            "strategy": "time_ordered_holdout",
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

    def _fit_estimator(
        self,
        X: pd.DataFrame,
        y: pd.Series,
        sample_weight: np.ndarray | None,
    ) -> tuple[xgb.XGBClassifier, dict[str, Any]]:
        fit_kwargs: dict[str, object] = {}
        if sample_weight is not None:
            fit_kwargs["sample_weight"] = sample_weight
        estimator, used_device, fallback_used, fallback_reason = fit_xgb_estimator(
            xgb.XGBClassifier,
            model_params=self.model_params,
            X=X,
            y=y,
            fit_kwargs=fit_kwargs,
            selected_device=self.runtime.get("selected_device", "cpu"),
            allow_cpu_fallback=self.runtime.get("allow_cpu_fallback", True),
        )
        return estimator, {
            "used_device": used_device,
            "inference_device": used_device,
            "fallback_used": fallback_used,
            "fallback_reason": fallback_reason,
        }

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        if y is None:
            raise ValueError("y is required for XGBMulticlassModel")
        self.feature_columns = list(X.columns)
        x_num = self._prepare_X(X)
        y_num = pd.Series(y).reset_index(drop=True)
        if len(y_num) != len(X.index):
            raise ValueError("y must have the same length as X")
        y_num.index = X.index
        y_num = y_num.astype(int)
        self.classes_ = sorted(int(x) for x in pd.unique(y_num))
        self.model_params["num_class"] = max(len(self.classes_), 2)
        sample_weight_num = normalize_sample_weight(sample_weight, index=X.index)
        self.calibrators = {}
        self.calibration_provenance = {
            "enabled": bool(self.use_calibration),
            "status": "disabled" if not self.use_calibration else "skipped",
            "strategy": "time_ordered_holdout",
            "rows": int(len(x_num)),
            "requested_fraction": float(self.calibration_fraction),
            "min_fit_rows": int(self.calibration_min_fit_rows),
            "min_calibration_rows": int(self.calibration_min_rows),
        }

        if self.use_calibration:
            split = build_time_ordered_calibration_split(
                y_num,
                fraction=self.calibration_fraction,
                min_fit_rows=self.calibration_min_fit_rows,
                min_calibration_rows=self.calibration_min_rows,
            )
            if split is None:
                self.calibration_provenance["reason"] = "insufficient_class_complete_holdout"
            else:
                fit_weight = None if sample_weight_num is None else sample_weight_num[split.fit_idx]
                calibration_estimator, calibration_runtime = self._fit_estimator(
                    x_num.iloc[split.fit_idx],
                    y_num.iloc[split.fit_idx],
                    fit_weight,
                )
                raw = predict_xgb_probabilities(
                    calibration_estimator,
                    x_num.iloc[split.calibration_idx],
                    device=calibration_runtime["inference_device"],
                )
                calibrators: dict[int, ProbabilityCalibrator] = {}
                calibration_targets = y_num.iloc[split.calibration_idx].to_numpy(dtype=int)
                for idx, klass in enumerate(self.classes_):
                    calibrator = ProbabilityCalibrator()
                    calibrator.fit(raw[:, idx], (calibration_targets == int(klass)).astype(int))
                    if calibrator.is_fitted:
                        calibrators[int(klass)] = calibrator
                if len(calibrators) == len(self.classes_):
                    self.calibrators = calibrators
                    self.calibration_provenance.update(
                        {
                            "status": "fitted",
                            "fit_rows": int(len(split.fit_idx)),
                            "calibration_rows": int(len(split.calibration_idx)),
                            "actual_fraction": float(split.actual_fraction),
                            "calibration_runtime": calibration_runtime,
                        }
                    )
                else:
                    self.calibration_provenance["reason"] = "calibrator_rejected_holdout"

        self.model, final_runtime = self._fit_estimator(x_num, y_num, sample_weight_num)
        self.runtime.update(final_runtime)
        self.calibration_provenance["refit_rows"] = int(len(x_num))

    def predict(self, X: pd.DataFrame) -> pd.Series:
        proba = self.predict_proba(X)
        labels = [int(c.replace("p", "")) for c in proba.columns]
        out = proba.to_numpy().argmax(axis=1)
        return pd.Series([labels[int(i)] for i in out], index=X.index)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        raw = predict_xgb_probabilities(
            self.model,
            self._prepare_X(X),
            device=self.runtime.get(
                "inference_device", self.runtime.get("used_device", "cpu")
            ),
        )
        calibrated = raw.copy()
        if self.calibrators:
            for idx, klass in enumerate(self.classes_):
                calibrator = self.calibrators.get(int(klass))
                if calibrator is not None:
                    calibrated[:, idx] = calibrator.transform(calibrated[:, idx])
        calibrated = np.clip(calibrated, 0.0, 1.0)
        row_sum = calibrated.sum(axis=1, keepdims=True)
        row_sum[row_sum <= 0.0] = 1.0
        calibrated = calibrated / row_sum
        cols = [f"p{int(klass)}" for klass in self.classes_]
        return pd.DataFrame(calibrated[:, : len(cols)], columns=cols, index=X.index)

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
                    "calibration_config": {
                        "fraction": float(self.calibration_fraction),
                        "min_fit_rows": int(self.calibration_min_fit_rows),
                        "min_calibration_rows": int(self.calibration_min_rows),
                    },
                    "calibration_provenance": dict(self.calibration_provenance),
                    "classes": list(self.classes_),
                    "has_calibrators": bool(self.calibrators),
                    "feature_columns": list(self.feature_columns),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if self.calibrators:
            import joblib

            joblib.dump(self.calibrators, path / "calibrators.joblib")
        else:
            (path / "calibrators.joblib").unlink(missing_ok=True)
        stamp_artifact_payload_digest(path)

    @classmethod
    @artifact_io_locked
    def load(cls, path: Path) -> "XGBMulticlassModel":
        meta = validate_artifact_contract(
            path, label=str(path), expected_name=str(cls.name)
        )
        params = dict(meta.get("params", {}) or {})
        calibration_config = dict(meta.get("calibration_config") or {})
        params["use_calibration"] = bool(meta.get("use_calibration", True))
        params["calibration_fraction"] = float(calibration_config.get("fraction", 0.2))
        params["calibration_min_fit_rows"] = int(calibration_config.get("min_fit_rows", 64))
        params["calibration_min_rows"] = int(calibration_config.get("min_calibration_rows", 32))
        params["device"] = "cpu"
        params["allow_cpu_fallback"] = True
        params["classes"] = list(meta.get("classes") or [])
        obj = cls(params=params)
        obj.model.load_model(str(path / "model.json"))
        pin_xgb_cpu_inference(obj.model)
        obj.classes_ = [int(x) for x in meta.get("classes") or []]
        obj.calibration_provenance = dict(meta.get("calibration_provenance") or obj.calibration_provenance)
        obj.feature_columns = list(meta.get("feature_columns") or [])
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
        rt = dict(meta.get("runtime") or {})
        if rt:
            obj.runtime = {**obj.runtime, **rt, "inference_device": "cpu"}
        cp = path / "calibrators.joblib"
        if cp.exists():
            import joblib

            obj.calibrators = dict(joblib.load(cp) or {})
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj
