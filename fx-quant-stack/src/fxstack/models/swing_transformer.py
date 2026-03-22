from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset
from transformers import TimeSeriesTransformerConfig

from fxstack.models.base import ModelBase
from fxstack.training.calibration import ProbabilityCalibrator


@dataclass(slots=True)
class _SwingTfParams:
    window_size: int = 96
    d_model: int = 64
    n_heads: int = 4
    n_layers: int = 2
    lr: float = 1e-3
    epochs: int = 5
    batch_size: int = 64
    require_cuda: bool = False


class _SwingTfNet(nn.Module):
    def __init__(self, *, in_features: int, cfg: TimeSeriesTransformerConfig, window_size: int) -> None:
        super().__init__()
        self.in_features = int(in_features)
        self.window_size = int(window_size)
        self.d_model = int(cfg.d_model)

        self.input_proj = nn.Linear(self.in_features, self.d_model)
        self.pos_emb = nn.Parameter(torch.zeros(1, self.window_size, self.d_model))
        enc_layer = nn.TransformerEncoderLayer(
            d_model=self.d_model,
            nhead=int(cfg.encoder_attention_heads),
            dim_feedforward=max(self.d_model * 2, 128),
            dropout=float(cfg.dropout),
            batch_first=True,
            activation="gelu",
        )
        self.encoder = nn.TransformerEncoder(enc_layer, num_layers=int(cfg.encoder_layers))
        self.head = nn.Linear(self.d_model, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        # x: [batch, window, features]
        z = self.input_proj(x)
        z = z + self.pos_emb[:, : z.shape[1], :]
        h = self.encoder(z)
        last = h[:, -1, :]
        return self.head(last)


class SwingTransformer(ModelBase):
    name = "swing_transformer"

    def __init__(
        self,
        *,
        window_size: int = 96,
        d_model: int = 64,
        n_heads: int = 4,
        n_layers: int = 2,
        lr: float = 1e-3,
        epochs: int = 5,
        batch_size: int = 64,
        require_cuda: bool = False,
    ) -> None:
        self.params = _SwingTfParams(
            window_size=max(8, int(window_size)),
            d_model=max(16, int(d_model)),
            n_heads=max(1, int(n_heads)),
            n_layers=max(1, int(n_layers)),
            lr=float(lr),
            epochs=max(1, int(epochs)),
            batch_size=max(8, int(batch_size)),
            require_cuda=bool(require_cuda),
        )
        if self.params.require_cuda and not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for SwingTransformer but not available")
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.feature_columns: list[str] = []
        self.calibrator: ProbabilityCalibrator | None = None
        self.n_features: int = 0
        self.hf_config: TimeSeriesTransformerConfig | None = None
        self.model: _SwingTfNet | None = None

    def _build(self, n_features: int) -> None:
        self.n_features = int(n_features)
        if self.n_features <= 0:
            raise ValueError("n_features must be > 0")

        self.hf_config = TimeSeriesTransformerConfig(
            prediction_length=1,
            context_length=int(self.params.window_size),
            d_model=int(self.params.d_model),
            encoder_layers=int(self.params.n_layers),
            encoder_attention_heads=int(self.params.n_heads),
            dropout=0.1,
        )
        self.model = _SwingTfNet(
            in_features=self.n_features,
            cfg=self.hf_config,
            window_size=int(self.params.window_size),
        ).to(self.device)

    def _to_sequences(self, X: pd.DataFrame, *, training: bool = False, y: pd.Series | None = None) -> tuple[torch.Tensor, torch.Tensor | None]:
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

        x_t = torch.from_numpy(seq).to(self.device)
        if training:
            if y is None:
                raise ValueError("y is required for training")
            y_t = torch.from_numpy(y.astype(int).to_numpy(dtype=np.float32).reshape(-1, 1)).to(self.device)
            return x_t, y_t
        return x_t, None

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        if y is None:
            raise ValueError("y is required for SwingTransformer")
        self.feature_columns = list(X.columns)
        self._build(n_features=X.shape[1])

        x_t, y_t = self._to_sequences(X, training=True, y=y)
        ds = TensorDataset(x_t, y_t)
        dl = DataLoader(ds, batch_size=self.params.batch_size, shuffle=True)

        assert self.model is not None
        opt = torch.optim.Adam(self.model.parameters(), lr=self.params.lr)
        loss_fn = nn.BCEWithLogitsLoss()

        self.model.train()
        for _ in range(self.params.epochs):
            for xb, yb in dl:
                opt.zero_grad(set_to_none=True)
                logits = self.model(xb)
                loss = loss_fn(logits, yb)
                loss.backward()
                opt.step()

        self.model.eval()
        with torch.no_grad():
            logits = self.model(x_t)
            p_raw = torch.sigmoid(logits).reshape(-1).detach().cpu().numpy()
        cal = ProbabilityCalibrator()
        cal.fit(p_raw, y.astype(int).to_numpy())
        self.calibrator = cal

    def predict(self, X: pd.DataFrame) -> pd.Series:
        p1 = self.predict_proba(X)["p1"]
        return (p1 >= 0.5).astype(int)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        if self.model is None:
            raise RuntimeError("model is not fitted")
        x_in = X.copy()
        if self.feature_columns:
            missing = [c for c in self.feature_columns if c not in x_in.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            x_in = x_in[self.feature_columns]

        x_t, _ = self._to_sequences(x_in, training=False)
        self.model.eval()
        with torch.no_grad():
            logits = self.model(x_t)
            p1 = torch.sigmoid(logits).reshape(-1).detach().cpu().numpy().astype(float)

        if self.calibrator is not None:
            p1 = self.calibrator.transform(p1)
        p1 = np.clip(p1, 0.0, 1.0)
        return pd.DataFrame({"p0": 1.0 - p1, "p1": p1}, index=X.index)

    def save(self, path: Path) -> None:
        if self.model is None or self.hf_config is None:
            raise RuntimeError("model is not fitted")
        path.mkdir(parents=True, exist_ok=True)
        torch.save(self.model.state_dict(), str(path / "weights.pt"))
        meta = {
            "name": self.name,
            "params": {
                "window_size": int(self.params.window_size),
                "d_model": int(self.params.d_model),
                "n_heads": int(self.params.n_heads),
                "n_layers": int(self.params.n_layers),
                "lr": float(self.params.lr),
                "epochs": int(self.params.epochs),
                "batch_size": int(self.params.batch_size),
                "require_cuda": bool(self.params.require_cuda),
            },
            "n_features": int(self.n_features),
            "feature_columns": list(self.feature_columns),
            "hf_config": self.hf_config.to_dict(),
            "device": str(self.device),
            "created_at": float(time.time()),
        }
        (path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        if self.calibrator is not None:
            joblib.dump(self.calibrator, path / "calibrator.joblib")

    @classmethod
    def load(cls, path: Path) -> "SwingTransformer":
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        params = dict(meta.get("params") or {})
        obj = cls(
            window_size=int(params.get("window_size", 96)),
            d_model=int(params.get("d_model", 64)),
            n_heads=int(params.get("n_heads", 4)),
            n_layers=int(params.get("n_layers", 2)),
            lr=float(params.get("lr", 1e-3)),
            epochs=int(params.get("epochs", 5)),
            batch_size=int(params.get("batch_size", 64)),
            require_cuda=bool(params.get("require_cuda", False)),
        )
        obj.feature_columns = list(meta.get("feature_columns") or [])
        obj._build(n_features=int(meta.get("n_features", len(obj.feature_columns) or 1)))
        assert obj.model is not None
        obj.model.load_state_dict(torch.load(str(path / "weights.pt"), map_location=obj.device))
        obj.model.eval()
        cp = path / "calibrator.joblib"
        if cp.exists():
            obj.calibrator = joblib.load(cp)
        return obj
