from __future__ import annotations

import json
from functools import lru_cache
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
from fxstack.settings import get_settings
from fxstack.training.calibration import ProbabilityCalibrator


@lru_cache(maxsize=1)
def probe_xgb_cuda_capability() -> dict[str, object]:
    try:
        X = np.array(
            [
                [0.1, 1.0, 0.0, 0.2],
                [0.2, 0.9, 0.1, 0.3],
                [0.8, 0.2, 0.9, 0.6],
                [0.9, 0.1, 0.8, 0.7],
                [0.3, 0.7, 0.2, 0.4],
                [0.7, 0.3, 0.7, 0.5],
            ],
            dtype=np.float32,
        )
        y = np.array([0, 0, 1, 1, 0, 1], dtype=np.int32)
        model = xgb.XGBClassifier(
            objective="binary:logistic",
            n_estimators=4,
            max_depth=2,
            learning_rate=0.2,
            tree_method="hist",
            device="cuda",
        )
        model.fit(X, y)
        return {"ok": True, "detail": "cuda_fit_ok"}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return bool(value)
    txt = str(value).strip().lower()
    return txt in {"1", "true", "yes", "on", "y"}


def _normalize_xgb_device(value: object) -> str:
    txt = str(value or "").strip().lower()
    if txt in {"cuda", "cpu", "auto"}:
        return txt
    return "auto"


def _normalize_sample_weight(values: pd.Series | None, *, index: pd.Index) -> np.ndarray | None:
    if values is None:
        return None
    arr = pd.Series(values, index=index).astype(float)
    arr = arr.replace([np.inf, -np.inf], np.nan).fillna(1.0)
    arr = arr.clip(lower=1e-6)
    return arr.to_numpy(dtype=float)


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

        requested_device = _normalize_xgb_device(p.pop("device", s.xgb_device))
        tree_method = str(p.pop("tree_method", s.xgb_tree_method) or "hist").strip().lower() or "hist"
        allow_cpu_fallback = _truthy(p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback))
        cuda_probe = probe_xgb_cuda_capability()

        runtime_device = "cpu"
        runtime_note = ""
        if requested_device == "cpu":
            runtime_device = "cpu"
        elif requested_device == "cuda":
            if bool(cuda_probe.get("ok")):
                runtime_device = "cuda"
            elif allow_cpu_fallback:
                runtime_device = "cpu"
                runtime_note = f"cuda_unavailable_fallback:{cuda_probe.get('detail', '')}"
            else:
                raise RuntimeError(f"XGBoost CUDA requested but unavailable: {cuda_probe.get('detail', '')}")
        else:
            if bool(cuda_probe.get("ok")):
                runtime_device = "cuda"
            else:
                runtime_device = "cpu"
                runtime_note = f"cuda_probe_failed:{cuda_probe.get('detail', '')}"

        self.use_calibration = bool(p.pop("use_calibration", True))
        self.calibration_fraction = min(0.5, max(0.05, float(p.pop("calibration_fraction", 0.20))))
        self.calibration_min_rows = max(16, int(p.pop("calibration_min_rows", 64)))
        self.calibration_embargo_rows = max(0, int(p.pop("calibration_embargo_rows", 24)))
        self.params = p
        self.runtime = {
            "requested_device": requested_device,
            "tree_method": tree_method,
            "allow_cpu_fallback": bool(allow_cpu_fallback),
            "selected_device": runtime_device,
            "used_device": runtime_device,
            "fallback_used": False,
            "fallback_reason": runtime_note,
            "cuda_probe": dict(cuda_probe),
        }
        self.model_params = dict(self.params)
        self.model_params.setdefault("tree_method", tree_method)
        self.model_params["device"] = runtime_device
        self.model = xgb.XGBClassifier(**self.model_params)
        self.calibrator: ProbabilityCalibrator | None = None
        self.calibration_diagnostics: dict[str, object] = {
            "method": "disabled" if not self.use_calibration else "chronological_holdout_calibration_v2",
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
        sample_weight_num = _normalize_sample_weight(sample_weight, index=X.index)

        def _fit_model(
            x_fit: pd.DataFrame,
            y_fit: pd.Series,
            weight_fit: np.ndarray | None,
        ) -> tuple[xgb.XGBClassifier, str, bool, str]:
            errors: list[str] = []

            def _fit_with(device: str | None) -> xgb.XGBClassifier:
                params = dict(self.model_params)
                if device is None:
                    params.pop("device", None)
                else:
                    params["device"] = device
                fitted = xgb.XGBClassifier(**params)
                fit_kwargs = {}
                if weight_fit is not None:
                    fit_kwargs["sample_weight"] = weight_fit
                fitted.fit(x_fit, y_fit, **fit_kwargs)
                return fitted

            attempts: list[tuple[str, str | None, bool]] = [
                ("primary", str(self.runtime.get("selected_device", "cpu")), False),
            ]
            if str(self.runtime.get("selected_device")) == "cuda" and bool(self.runtime.get("allow_cpu_fallback", True)):
                attempts.append(("cpu_fallback", "cpu", True))
            attempts.append(("legacy_cpu", None, True))

            for name, device, is_fallback in attempts:
                try:
                    fitted = _fit_with(device)
                    used_device = "cpu_legacy" if device is None else str(device)
                    fallback_reason = f"{name}:{';'.join(errors)}" if is_fallback else ""
                    return fitted, used_device, bool(is_fallback), fallback_reason
                except Exception as exc:
                    errors.append(f"{name}:{type(exc).__name__}:{exc}")
            raise RuntimeError("xgb_fit_failed:" + ";".join(errors))

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
                preliminary_weight = sample_weight_num[:fit_end] if sample_weight_num is not None else None
                preliminary, _, _, _ = _fit_model(
                    x_num.iloc[:fit_end],
                    y_num.iloc[:fit_end],
                    preliminary_weight,
                )
                raw_calibration = np.asarray(
                    preliminary.predict_proba(x_num.iloc[calibration_start:])[:, 1],
                    dtype=float,
                )
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
                        "calibration_positive_share": float(y_num.iloc[calibration_start:].mean()),
                    }
                )

        fitted_model, used_device, fallback_used, fallback_reason = _fit_model(
            x_num,
            y_num,
            sample_weight_num,
        )
        self.model = fitted_model
        self.runtime["used_device"] = str(used_device)
        if fallback_used:
            self.runtime["fallback_used"] = True
            self.runtime["fallback_reason"] = str(fallback_reason)

    def predict(self, X: pd.DataFrame) -> pd.Series:
        out = self.model.predict(self._prepare_X(X))
        return pd.Series(out, index=X.index)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        p = self.model.predict_proba(self._prepare_X(X))
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
        meta = validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        params = dict(meta.get("params", {}) or {})
        params["use_calibration"] = bool(meta.get("use_calibration", True))
        calibration_config = dict(meta.get("calibration_config") or {})
        if calibration_config:
            params["calibration_fraction"] = float(calibration_config.get("fraction", 0.20))
            params["calibration_min_rows"] = int(calibration_config.get("min_rows", 64))
            params["calibration_embargo_rows"] = int(calibration_config.get("embargo_rows", 24))
        params["device"] = "cpu"
        params["allow_cpu_fallback"] = True
        obj = cls(params=params)
        obj.model.load_model(str(path / "model.json"))
        rt = dict(meta.get("runtime") or {})
        if rt:
            obj.runtime = {
                **obj.runtime,
                **rt,
                "requested_device": str(rt.get("requested_device", obj.runtime.get("requested_device", "cpu"))),
                    "used_device": str(rt.get("used_device", rt.get("selected_device", "cpu"))),
            }
        obj.feature_columns = list(meta.get("feature_columns") or [])
        obj.calibration_diagnostics = dict(meta.get("calibration_diagnostics") or obj.calibration_diagnostics)
        if not obj.feature_columns:
            try:
                booster = obj.model.get_booster()
                booster_feature_columns = list(getattr(booster, "feature_names", None) or [])
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
