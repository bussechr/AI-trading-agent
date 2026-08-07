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
    pin_xgb_cpu_inference,
    predict_xgb_values,
    probe_xgb_cuda_capability,
    record_xgb_fit_runtime,
)
from fxstack.settings import get_settings


class BeliefRankerXGB(ModelBase):
    name = "belief_ranker_xgb"

    def __init__(self, *, params: dict | None = None) -> None:
        s = get_settings()
        p = dict(params or {})
        p.setdefault("objective", "rank:pairwise")
        p.setdefault("n_estimators", 240)
        p.setdefault("max_depth", 5)
        p.setdefault("learning_rate", 0.05)
        p.setdefault("subsample", 0.9)
        p.setdefault("colsample_bytree", 0.9)
        p.setdefault("random_state", 7)
        device = p.pop("device", s.xgb_device)
        tree_method = p.pop("tree_method", s.xgb_tree_method)
        allow_cpu_fallback = p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback)
        self.params = p
        self.runtime = build_xgb_runtime(
            requested_device=device,
            tree_method=tree_method,
            allow_cpu_fallback=allow_cpu_fallback,
            cuda_probe=probe_xgb_cuda_capability,
        )
        self.model_params = dict(self.params)
        self.model_params.setdefault("tree_method", str(self.runtime["tree_method"]))
        self.model_params["device"] = str(self.runtime["selected_device"])
        self.model = xgb.XGBRanker(**self.model_params)
        self.feature_columns: list[str] = []

    def fit(
        self, X: pd.DataFrame, y: pd.Series, *, qid: pd.Series | np.ndarray | list[int]
    ) -> None:
        self.feature_columns = list(X.columns)
        x_num = X.astype(float)
        self.model, used_device, fallback_used, fallback_reason = fit_xgb_estimator(
            xgb.XGBRanker,
            model_params=self.model_params,
            X=x_num,
            y=pd.Series(y).astype(float),
            fit_kwargs={"qid": np.asarray(qid)},
            selected_device=self.runtime["selected_device"],
            allow_cpu_fallback=self.runtime["allow_cpu_fallback"],
        )
        record_xgb_fit_runtime(
            self.runtime,
            used_device=used_device,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
        )

    def predict(self, X: pd.DataFrame) -> pd.Series:
        x_num = (
            X[self.feature_columns].astype(float)
            if self.feature_columns
            else X.astype(float)
        )
        values = predict_xgb_values(
            self.model,
            x_num,
            device=self.runtime.get(
                "inference_device", self.runtime.get("used_device", "cpu")
            ),
        )
        return pd.Series(values, index=X.index, dtype=float)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        scores = self.predict(X)
        return pd.DataFrame({"score": scores.astype(float)}, index=X.index)

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
                    "feature_columns": list(self.feature_columns),
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        stamp_artifact_payload_digest(path)

    @classmethod
    @artifact_io_locked
    def load(cls, path: Path) -> "BeliefRankerXGB":
        meta = validate_artifact_contract(
            path, label=str(path), expected_name=str(cls.name)
        )
        params = dict(meta.get("params") or {})
        params.update({"device": "cpu", "allow_cpu_fallback": True})
        obj = cls(params=params)
        obj.model.load_model(str(path / "model.json"))
        pin_xgb_cpu_inference(obj.model)
        obj.runtime = {
            **obj.runtime,
            **dict(meta.get("runtime") or {}),
            "inference_device": "cpu",
        }
        obj.feature_columns = list(meta.get("feature_columns") or [])
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj
