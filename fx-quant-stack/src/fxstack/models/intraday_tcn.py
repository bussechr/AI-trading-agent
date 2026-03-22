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

from fxstack.models.base import ModelBase
from fxstack.training.calibration import ProbabilityCalibrator

try:  # pragma: no cover - optional import surface
    from pytorch_tcn import TCN as _PTCN
except Exception:  # pragma: no cover - fallback path
    _PTCN = None


@dataclass(slots=True)
class _TCNParams:
    window_size: int = 128
    hidden_channels: int = 32
    lr: float = 1e-3
    epochs: int = 5
    batch_size: int = 64
    require_cuda: bool = False


class _ConvFallback(nn.Module):
    def __init__(self, in_channels: int, hidden_channels: int) -> None:
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(in_channels, hidden_channels, kernel_size=3, padding=2, dilation=1),
            nn.ReLU(),
            nn.Conv1d(hidden_channels, hidden_channels, kernel_size=3, padding=4, dilation=2),
            nn.ReLU(),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        y = self.net(x)
        if y.ndim == 3:
            return y[:, :, -1]
        return y


class IntradayTCN(ModelBase):
    name = "intraday_tcn"

    def __init__(
        self,
        *,
        window_size: int = 128,
        hidden_channels: int = 32,
        lr: float = 1e-3,
        epochs: int = 5,
        batch_size: int = 64,
        require_cuda: bool = False,
    ) -> None:
        self.params = _TCNParams(
            window_size=max(4, int(window_size)),
            hidden_channels=max(8, int(hidden_channels)),
            lr=float(lr),
            epochs=max(1, int(epochs)),
            batch_size=max(8, int(batch_size)),
            require_cuda=bool(require_cuda),
        )
        self.feature_columns: list[str] = []
        self.calibrator: ProbabilityCalibrator | None = None

        if self.params.require_cuda and not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for IntradayTCN but not available")
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.backbone: nn.Module | None = None
        self.head: nn.Module | None = None
        self.n_features: int = 0

    def _build(self, n_features: int) -> None:
        self.n_features = int(n_features)
        if self.n_features <= 0:
            raise ValueError("n_features must be > 0")

        if _PTCN is not None:
            self.backbone = _PTCN(
                num_inputs=self.n_features,
                num_channels=[self.params.hidden_channels, self.params.hidden_channels],
                kernel_size=3,
                dropout=0.1,
                causal=True,
            )
        else:
            self.backbone = _ConvFallback(self.n_features, self.params.hidden_channels)

        self.head = nn.Linear(self.params.hidden_channels, 1)
        self.backbone.to(self.device)
        self.head.to(self.device)

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

        # NCL layout for pytorch-tcn.
        x_t = torch.from_numpy(np.transpose(seq, (0, 2, 1))).to(self.device)

        if training:
            if y is None:
                raise ValueError("y is required for training")
            y_t = torch.from_numpy(y.astype(int).to_numpy(dtype=np.float32).reshape(-1, 1)).to(self.device)
            return x_t, y_t
        return x_t, None

    def _forward_logits(self, x_t: torch.Tensor) -> torch.Tensor:
        if self.backbone is None or self.head is None:
            raise RuntimeError("model is not initialized")
        h = self.backbone(x_t)
        if isinstance(h, tuple):
            h = h[0]
        if h.ndim == 3:
            # Handle NCL or NLC output variants.
            if h.shape[1] == self.params.hidden_channels:
                h = h[:, :, -1]
            else:
                h = h[:, -1, :]
        logits = self.head(h)
        return logits

    def fit(
        self,
        X: pd.DataFrame,
        y: pd.Series | None = None,
        sample_weight: pd.Series | None = None,
    ) -> None:
        if y is None:
            raise ValueError("y is required for IntradayTCN")
        self.feature_columns = list(X.columns)
        self._build(n_features=X.shape[1])

        x_t, y_t = self._to_sequences(X, training=True, y=y)
        ds = TensorDataset(x_t, y_t)
        dl = DataLoader(ds, batch_size=self.params.batch_size, shuffle=True)

        assert self.backbone is not None and self.head is not None
        opt = torch.optim.Adam(list(self.backbone.parameters()) + list(self.head.parameters()), lr=self.params.lr)
        loss_fn = nn.BCEWithLogitsLoss()

        self.backbone.train()
        self.head.train()
        for _ in range(self.params.epochs):
            for xb, yb in dl:
                opt.zero_grad(set_to_none=True)
                logits = self._forward_logits(xb)
                loss = loss_fn(logits, yb)
                loss.backward()
                opt.step()

        self.backbone.eval()
        self.head.eval()
        with torch.no_grad():
            logits = self._forward_logits(x_t)
            p_raw = torch.sigmoid(logits).reshape(-1).detach().cpu().numpy()
        cal = ProbabilityCalibrator()
        cal.fit(p_raw, y.astype(int).to_numpy())
        self.calibrator = cal

    def predict(self, X: pd.DataFrame) -> pd.Series:
        p1 = self.predict_proba(X)["p1"]
        return (p1 >= 0.5).astype(int)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        if self.n_features <= 0:
            raise RuntimeError("model is not fitted")
        x_in = X.copy()
        if self.feature_columns:
            missing = [c for c in self.feature_columns if c not in x_in.columns]
            if missing:
                raise ValueError(f"missing feature columns: {','.join(missing)}")
            x_in = x_in[self.feature_columns]

        x_t, _ = self._to_sequences(x_in, training=False)
        assert self.backbone is not None and self.head is not None
        self.backbone.eval()
        self.head.eval()
        with torch.no_grad():
            logits = self._forward_logits(x_t)
            p1 = torch.sigmoid(logits).reshape(-1).detach().cpu().numpy().astype(float)

        if self.calibrator is not None:
            p1 = self.calibrator.transform(p1)
        p1 = np.clip(p1, 0.0, 1.0)
        out = pd.DataFrame({"p0": 1.0 - p1, "p1": p1}, index=X.index)
        return out

    def save(self, path: Path) -> None:
        if self.backbone is None or self.head is None:
            raise RuntimeError("model is not fitted")
        path.mkdir(parents=True, exist_ok=True)

        torch.save(
            {
                "backbone": self.backbone.state_dict(),
                "head": self.head.state_dict(),
            },
            str(path / "weights.pt"),
        )
        meta = {
            "name": self.name,
            "params": {
                "window_size": int(self.params.window_size),
                "hidden_channels": int(self.params.hidden_channels),
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

    @classmethod
    def load(cls, path: Path) -> "IntradayTCN":
        meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
        params = dict(meta.get("params") or {})
        obj = cls(
            window_size=int(params.get("window_size", 128)),
            hidden_channels=int(params.get("hidden_channels", 32)),
            lr=float(params.get("lr", 1e-3)),
            epochs=int(params.get("epochs", 5)),
            batch_size=int(params.get("batch_size", 64)),
            require_cuda=bool(params.get("require_cuda", False)),
        )
        obj.feature_columns = list(meta.get("feature_columns") or [])
        obj._build(n_features=int(meta.get("n_features", len(obj.feature_columns) or 1)))
        weights = torch.load(str(path / "weights.pt"), map_location=obj.device)
        assert obj.backbone is not None and obj.head is not None
        obj.backbone.load_state_dict(dict(weights.get("backbone") or {}))
        obj.head.load_state_dict(dict(weights.get("head") or {}))
        cp = path / "calibrator.joblib"
        if cp.exists():
            obj.calibrator = joblib.load(cp)
        obj.backbone.eval()
        obj.head.eval()
        return obj
