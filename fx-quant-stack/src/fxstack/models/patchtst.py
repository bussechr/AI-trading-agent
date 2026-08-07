from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd

from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models.artifact_contract import (
    artifact_io_locked,
    stamp_artifact_payload_digest,
    validate_artifact_contract,
)
from fxstack.models.base import ModelBase
from fxstack.training.calibration import ProbabilityCalibrator

try:  # pragma: no cover - optional dependency boundary
    import torch
    from torch.utils.data import DataLoader, TensorDataset
except Exception as exc:  # pragma: no cover - optional dependency boundary
    torch = None  # type: ignore[assignment]
    DataLoader = None  # type: ignore[assignment]
    TensorDataset = None  # type: ignore[assignment]
    _TORCH_IMPORT_ERROR = exc
else:  # pragma: no cover - optional dependency boundary
    _TORCH_IMPORT_ERROR = None

try:  # pragma: no cover - optional dependency boundary
    from transformers.models.patchtst.configuration_patchtst import PatchTSTConfig
    from transformers.models.patchtst.modeling_patchtst import PatchTSTForClassification
except Exception as exc:  # pragma: no cover - optional dependency boundary
    PatchTSTConfig = None  # type: ignore[assignment]
    PatchTSTForClassification = None  # type: ignore[assignment]
    _TRANSFORMERS_IMPORT_ERROR = exc
else:  # pragma: no cover - optional dependency boundary
    _TRANSFORMERS_IMPORT_ERROR = None


def patchtst_dependencies_available() -> bool:
    return bool(torch is not None and PatchTSTConfig is not None and PatchTSTForClassification is not None)


def patchtst_dependency_error_detail() -> str:
    parts: list[str] = []
    if _TORCH_IMPORT_ERROR is not None:
        parts.append(f"torch:{type(_TORCH_IMPORT_ERROR).__name__}")
    if _TRANSFORMERS_IMPORT_ERROR is not None:
        parts.append(f"transformers:{type(_TRANSFORMERS_IMPORT_ERROR).__name__}")
    return ",".join(parts) or "missing_optional_patchtst_stack"


def _ensure_patchtst_stack() -> None:
    if patchtst_dependencies_available():
        return
    raise RuntimeError(
        "PatchTST training requires the research stack (`torch` and `transformers`) in the selected interpreter. "
        f"Details: {patchtst_dependency_error_detail()}"
    )


@dataclass(slots=True)
class _PatchTSTParams:
    window_size: int = 96
    patch_length: int = 12
    stride: int = 6
    d_model: int = 64
    num_layers: int = 2
    num_heads: int = 4
    dropout: float = 0.1
    lr: float = 1e-3
    epochs: int = 5
    batch_size: int = 64
    require_cuda: bool = False


class _PatchTSTBinaryClassifier(ModelBase):
    name = "patchtst"

    def __init__(
        self,
        *,
        window_size: int = 96,
        patch_length: int = 12,
        stride: int = 6,
        d_model: int = 64,
        num_layers: int = 2,
        num_heads: int = 4,
        dropout: float = 0.1,
        lr: float = 1e-3,
        epochs: int = 5,
        batch_size: int = 64,
        require_cuda: bool = False,
    ) -> None:
        _ensure_patchtst_stack()
        assert torch is not None
        self.params = _PatchTSTParams(
            window_size=max(8, int(window_size)),
            patch_length=max(2, int(patch_length)),
            stride=max(1, int(stride)),
            d_model=max(16, int(d_model)),
            num_layers=max(1, int(num_layers)),
            num_heads=max(1, int(num_heads)),
            dropout=max(0.0, float(dropout)),
            lr=float(lr),
            epochs=max(1, int(epochs)),
            batch_size=max(8, int(batch_size)),
            require_cuda=bool(require_cuda),
        )
        if self.params.require_cuda and not torch.cuda.is_available():
            raise RuntimeError(f"CUDA is required for {self.__class__.__name__} but not available")
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.feature_columns: list[str] = []
        self.n_features: int = 0
        self.model: Any | None = None
        self.calibrator: ProbabilityCalibrator | None = None

    def _build(self, n_features: int) -> None:
        _ensure_patchtst_stack()
        assert PatchTSTConfig is not None and PatchTSTForClassification is not None
        self.n_features = int(n_features)
        config = PatchTSTConfig(
            num_input_channels=max(1, int(n_features)),
            context_length=int(self.params.window_size),
            patch_length=min(int(self.params.patch_length), int(self.params.window_size)),
            patch_stride=min(int(self.params.stride), int(self.params.window_size)),
            d_model=int(self.params.d_model),
            num_hidden_layers=int(self.params.num_layers),
            num_attention_heads=int(self.params.num_heads),
            ffn_dim=max(int(self.params.d_model) * 4, 128),
            attention_dropout=float(self.params.dropout),
            positional_dropout=float(self.params.dropout),
            ff_dropout=float(self.params.dropout),
            head_dropout=float(self.params.dropout),
            use_cls_token=True,
            scaling="std",
            num_targets=2,
        )
        self.model = PatchTSTForClassification(config).to(self.device)

    def _to_sequences(self, X: pd.DataFrame) -> Any:
        assert torch is not None
        arr = X.astype(float).to_numpy(dtype=np.float32)
        if arr.ndim != 2 or arr.shape[0] == 0:
            raise ValueError("X must be a non-empty 2D frame")
        n_rows, n_feat = arr.shape
        win = int(self.params.window_size)
        seq = np.zeros((n_rows, win, n_feat), dtype=np.float32)
        for i in range(n_rows):
            start = max(0, i - win + 1)
            cur = arr[start : i + 1]
            if cur.shape[0] < win:
                pad = np.repeat(cur[:1], win - cur.shape[0], axis=0)
                cur = np.vstack([pad, cur])
            seq[i] = cur[-win:]
        return torch.from_numpy(seq).to(self.device)

    def _aligned_frame(self, X: pd.DataFrame) -> pd.DataFrame:
        x_in = X.copy()
        if self.feature_columns:
            missing = [c for c in self.feature_columns if c not in x_in.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            x_in = x_in[self.feature_columns]
        return x_in

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        if y is None:
            raise ValueError(f"y is required for {self.__class__.__name__}")
        _ensure_patchtst_stack()
        assert torch is not None and TensorDataset is not None and DataLoader is not None
        self.feature_columns = list(X.columns)
        self._build(n_features=X.shape[1])
        assert self.model is not None

        x_t = self._to_sequences(X)
        y_t = torch.from_numpy(pd.Series(y).astype(int).to_numpy(dtype=np.int64)).to(self.device)
        if sample_weight is not None:
            w_t = torch.from_numpy(pd.Series(sample_weight).astype(float).to_numpy(dtype=np.float32)).to(self.device)
        else:
            w_t = torch.ones(len(X), dtype=torch.float32, device=self.device)
        ds = TensorDataset(x_t, y_t, w_t)
        dl = DataLoader(ds, batch_size=int(self.params.batch_size), shuffle=True)

        counts = pd.Series(y).value_counts().to_dict()
        neg = float(counts.get(0, 0) or 0.0)
        pos = float(counts.get(1, 0) or 0.0)
        total = max(1.0, neg + pos)
        class_weights = torch.tensor(
            [
                total / max(1.0, 2.0 * neg) if neg > 0.0 else 1.0,
                total / max(1.0, 2.0 * pos) if pos > 0.0 else 1.0,
            ],
            dtype=torch.float32,
            device=self.device,
        )
        loss_fn = torch.nn.CrossEntropyLoss(weight=class_weights, reduction="none")
        optimizer = torch.optim.Adam(self.model.parameters(), lr=float(self.params.lr))

        self.model.train()
        for _ in range(int(self.params.epochs)):
            for xb, yb, wb in dl:
                optimizer.zero_grad(set_to_none=True)
                outputs = self.model(past_values=xb, return_dict=True)
                logits = outputs.prediction_logits
                loss = loss_fn(logits, yb)
                loss = (loss * wb) / torch.clamp(wb.mean(), min=1e-6)
                loss.mean().backward()
                optimizer.step()

        self.model.eval()
        with torch.no_grad():
            logits = self.model(past_values=x_t, return_dict=True).prediction_logits
            p_raw = torch.softmax(logits, dim=-1)[:, 1].detach().cpu().numpy().astype(float)
        cal = ProbabilityCalibrator()
        cal.fit(p_raw, pd.Series(y).astype(int).to_numpy())
        self.calibrator = cal

    def predict(self, X: pd.DataFrame) -> pd.Series:
        p1 = self.predict_proba(X)["p1"]
        return (p1 >= 0.5).astype(int)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        _ensure_patchtst_stack()
        assert torch is not None
        if self.model is None:
            raise RuntimeError("model is not fitted")
        x_in = self._aligned_frame(X)
        x_t = self._to_sequences(x_in)
        self.model.eval()
        with torch.no_grad():
            logits = self.model(past_values=x_t, return_dict=True).prediction_logits
            probs = torch.softmax(logits, dim=-1).detach().cpu().numpy().astype(float)
        p1 = np.asarray(probs[:, 1], dtype=float)
        if self.calibrator is not None:
            p1 = self.calibrator.transform(p1)
        p1 = np.clip(p1, 0.0, 1.0)
        return pd.DataFrame({"p0": 1.0 - p1, "p1": p1}, index=X.index)

    @artifact_io_locked
    def save(self, path: Path) -> None:
        if self.model is None:
            raise RuntimeError("model is not fitted")
        path.mkdir(parents=True, exist_ok=True)
        self.model.save_pretrained(str(path))
        meta = {
            "name": self.name,
            **feature_contract_metadata(),
            "params": {
                "window_size": int(self.params.window_size),
                "patch_length": int(self.params.patch_length),
                "stride": int(self.params.stride),
                "d_model": int(self.params.d_model),
                "num_layers": int(self.params.num_layers),
                "num_heads": int(self.params.num_heads),
                "dropout": float(self.params.dropout),
                "lr": float(self.params.lr),
                "epochs": int(self.params.epochs),
                "batch_size": int(self.params.batch_size),
                "require_cuda": bool(self.params.require_cuda),
            },
            "n_features": int(self.n_features),
            "feature_columns": list(self.feature_columns),
            "device": str(self.device),
            "created_at": float(time.time()),
        }
        (path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        if self.calibrator is not None:
            joblib.dump(self.calibrator, path / "calibrator.joblib")
        else:
            (path / "calibrator.joblib").unlink(missing_ok=True)
        stamp_artifact_payload_digest(path)

    @classmethod
    @artifact_io_locked
    def load(cls, path: Path) -> "_PatchTSTBinaryClassifier":
        _ensure_patchtst_stack()
        assert PatchTSTForClassification is not None
        meta = validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        params = dict(meta.get("params") or {})
        obj = cls(
            window_size=int(params.get("window_size", 96)),
            patch_length=int(params.get("patch_length", 12)),
            stride=int(params.get("stride", 6)),
            d_model=int(params.get("d_model", 64)),
            num_layers=int(params.get("num_layers", 2)),
            num_heads=int(params.get("num_heads", 4)),
            dropout=float(params.get("dropout", 0.1)),
            lr=float(params.get("lr", 1e-3)),
            epochs=int(params.get("epochs", 5)),
            batch_size=int(params.get("batch_size", 64)),
            require_cuda=bool(params.get("require_cuda", False)),
        )
        obj.feature_columns = list(meta.get("feature_columns") or [])
        obj.n_features = int(meta.get("n_features", len(obj.feature_columns) or 1))
        obj.model = PatchTSTForClassification.from_pretrained(str(path)).to(obj.device)
        obj.model.eval()
        cp = path / "calibrator.joblib"
        if cp.exists():
            obj.calibrator = joblib.load(cp)
        validate_artifact_contract(path, label=str(path), expected_name=str(cls.name))
        return obj


class SwingPatchTST(_PatchTSTBinaryClassifier):
    name = "swing_patchtst"


class IntradayPatchTST(_PatchTSTBinaryClassifier):
    name = "intraday_patchtst"
