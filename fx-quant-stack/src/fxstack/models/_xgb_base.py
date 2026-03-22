from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import xgboost as xgb

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
        errors: list[str] = []

        def _fit_with(device: str | None) -> None:
            params = dict(self.model_params)
            if device is None:
                params.pop("device", None)
            else:
                params["device"] = device
            self.model = xgb.XGBClassifier(**params)
            fit_kwargs = {}
            if sample_weight_num is not None:
                fit_kwargs["sample_weight"] = sample_weight_num
            self.model.fit(x_num, y_num, **fit_kwargs)
            if device is None:
                self.runtime["used_device"] = "cpu_legacy"
            else:
                self.runtime["used_device"] = str(device)

        attempts: list[tuple[str, str | None, bool]] = [
            ("primary", str(self.runtime.get("selected_device", "cpu")), False),
        ]
        if str(self.runtime.get("selected_device")) == "cuda" and bool(self.runtime.get("allow_cpu_fallback", True)):
            attempts.append(("cpu_fallback", "cpu", True))
        attempts.append(("legacy_cpu", None, True))

        fit_ok = False
        for name, device, is_fallback in attempts:
            try:
                _fit_with(device)
                if is_fallback:
                    self.runtime["fallback_used"] = True
                    self.runtime["fallback_reason"] = f"{name}:{';'.join(errors)}"
                fit_ok = True
                break
            except Exception as exc:
                errors.append(f"{name}:{type(exc).__name__}:{exc}")
                continue

        if not fit_ok:
            raise RuntimeError("xgb_fit_failed:" + ";".join(errors))

        if bool(self.use_calibration):
            raw = self.model.predict_proba(x_num)
            p1 = pd.Series(raw[:, 1], index=X.index).astype(float).to_numpy()
            yy = y_num.to_numpy()
            cal = ProbabilityCalibrator()
            cal.fit(p1, yy)
            self.calibrator = cal

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

    def save(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        self.model.save_model(str(path / "model.json"))
        (path / "meta.json").write_text(
            json.dumps(
                {
                    "name": self.name,
                    "params": self.params,
                    "runtime": self.runtime,
                    "use_calibration": bool(self.use_calibration),
                    "has_calibrator": self.calibrator is not None,
                    "feature_columns": list(self.feature_columns),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if self.calibrator is not None:
            joblib.dump(self.calibrator, path / "calibrator.joblib")

    @classmethod
    def load(cls, path: Path) -> "XGBBinaryModel":
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        params = dict(meta.get("params", {}) or {})
        params["use_calibration"] = bool(meta.get("use_calibration", True))
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
        if not obj.feature_columns:
            try:
                booster = obj.model.get_booster()
                booster_feature_columns = list(getattr(booster, "feature_names", None) or [])
            except Exception:
                booster_feature_columns = []
            if booster_feature_columns:
                obj.feature_columns = booster_feature_columns
                try:
                    meta["feature_columns"] = list(obj.feature_columns)
                    (path / "meta.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
                except Exception:
                    pass
        if bool(meta.get("has_calibrator", False)):
            cp = path / "calibrator.joblib"
            if cp.exists():
                obj.calibrator = joblib.load(cp)
        return obj
