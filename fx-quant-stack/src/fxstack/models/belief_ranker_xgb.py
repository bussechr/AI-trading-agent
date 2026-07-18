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
from fxstack.models._xgb_base import probe_xgb_cuda_capability
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
        device = str(p.pop("device", s.xgb_device) or "auto").strip().lower()
        if device not in {"auto", "cuda", "cpu"}:
            device = "auto"
        allow_cpu_fallback = bool(p.pop("allow_cpu_fallback", s.xgb_allow_cpu_fallback))
        cuda_probe = probe_xgb_cuda_capability()
        selected_device = "cuda" if device in {"auto", "cuda"} and bool(cuda_probe.get("ok")) else "cpu"
        if device == "cuda" and selected_device != "cuda" and not allow_cpu_fallback:
            raise RuntimeError(f"XGBoost CUDA requested but unavailable: {cuda_probe.get('detail', '')}")
        self.params = p
        self.runtime = {
            "requested_device": device,
            "selected_device": selected_device,
            "allow_cpu_fallback": allow_cpu_fallback,
            "cuda_probe": dict(cuda_probe),
        }
        self.model_params = dict(self.params)
        self.model_params.setdefault("tree_method", str(s.xgb_tree_method or "hist"))
        self.model_params["device"] = selected_device
        self.model = xgb.XGBRanker(**self.model_params)
        self.feature_columns: list[str] = []

    def fit(self, X: pd.DataFrame, y: pd.Series, *, qid: pd.Series | np.ndarray | list[int]) -> None:
        self.feature_columns = list(X.columns)
        x_num = X.astype(float)
        self.model.fit(x_num, pd.Series(y).astype(float), qid=np.asarray(qid))

    def predict(self, X: pd.DataFrame) -> pd.Series:
        x_num = X[self.feature_columns].astype(float) if self.feature_columns else X.astype(float)
        return pd.Series(self.model.predict(x_num), index=X.index, dtype=float)

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
        meta = validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        obj = cls(params=dict(meta.get("params") or {}))
        obj.model.load_model(str(path / "model.json"))
        obj.runtime = dict(meta.get("runtime") or obj.runtime)
        obj.feature_columns = list(meta.get("feature_columns") or [])
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj
