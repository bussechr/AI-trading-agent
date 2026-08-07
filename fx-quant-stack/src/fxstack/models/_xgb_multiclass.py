from __future__ import annotations

import json
from pathlib import Path

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
from fxstack.training.calibration import ProbabilityCalibrator


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

        requested_device = p.pop("device", s.xgb_device)
        tree_method = (
            str(p.pop("tree_method", s.xgb_tree_method) or "hist").strip().lower()
            or "hist"
        )
        allow_cpu_fallback = p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback)

        self.use_calibration = bool(p.pop("use_calibration", True))
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
            raise ValueError("y is required for XGBMulticlassModel")
        self.feature_columns = list(X.columns)
        x_num = self._prepare_X(X)
        y_num = pd.Series(y, index=X.index).astype(int)
        self.classes_ = sorted(int(x) for x in pd.unique(y_num))
        self.model_params["num_class"] = max(len(self.classes_), 2)
        sample_weight_num = normalize_sample_weight(sample_weight, index=X.index)
        fit_kwargs: dict[str, object] = {}
        if sample_weight_num is not None:
            fit_kwargs["sample_weight"] = sample_weight_num
        self.model, used_device, fallback_used, fallback_reason = fit_xgb_estimator(
            xgb.XGBClassifier,
            model_params=self.model_params,
            X=x_num,
            y=y_num,
            fit_kwargs=fit_kwargs,
            selected_device=self.runtime.get("selected_device", "cpu"),
            allow_cpu_fallback=self.runtime.get("allow_cpu_fallback", True),
        )
        record_xgb_fit_runtime(
            self.runtime,
            used_device=used_device,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

        self.calibrators = {}
        if bool(self.use_calibration):
            raw = predict_xgb_probabilities(
                self.model, x_num, device=self.runtime["inference_device"]
            )
            for idx, klass in enumerate(self.classes_):
                cal = ProbabilityCalibrator()
                cal.fit(raw[:, idx], (y_num.to_numpy() == int(klass)).astype(int))
                self.calibrators[int(klass)] = cal

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
                cal = self.calibrators.get(int(klass))
                if cal is not None:
                    calibrated[:, idx] = cal.transform(calibrated[:, idx])
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
        params["use_calibration"] = bool(meta.get("use_calibration", True))
        params["device"] = "cpu"
        params["allow_cpu_fallback"] = True
        params["classes"] = list(meta.get("classes") or [])
        obj = cls(params=params)
        obj.model.load_model(str(path / "model.json"))
        pin_xgb_cpu_inference(obj.model)
        obj.classes_ = [int(x) for x in meta.get("classes") or []]
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
