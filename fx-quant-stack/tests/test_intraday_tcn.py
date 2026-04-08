from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

torch = pytest.importorskip("torch")

from fxstack.models import intraday_tcn
from fxstack.models.intraday_tcn import IntradayTCN, _LegacyConvFallback


def _frame(rows: int = 24) -> tuple[pd.DataFrame, pd.Series]:
    idx = pd.date_range("2026-01-01", periods=rows, freq="H", tz="UTC")
    x1 = pd.Series(range(rows), dtype=float)
    x2 = pd.Series([float((i % 5) - 2) for i in range(rows)])
    X = pd.DataFrame({"ret_1": x1 / 100.0, "vol_20": x2.abs() + 0.1})
    y = pd.Series(((x1 + x2) > x1.median()).astype(int))
    X.index = idx
    y.index = idx
    return X, y


def _legacy_artifact(tmp_path: Path, X: pd.DataFrame) -> Path:
    model = IntradayTCN(window_size=6, hidden_channels=8, epochs=1, batch_size=4, require_cuda=False)
    model.feature_columns = list(X.columns)
    model.n_features = X.shape[1]
    model.backbone = _LegacyConvFallback(X.shape[1], model.params.hidden_channels).to(model.device)
    model.head = torch.nn.Linear(model.params.hidden_channels, 1).to(model.device)
    model.backbone_kind = "legacy_conv_fallback"
    path = tmp_path / "legacy_intraday_tcn"
    model.save(path)
    return path


def test_intraday_tcn_causal_fallback_records_backbone_kind(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    X, y = _frame()
    monkeypatch.setattr(intraday_tcn, "_PTCN", None)
    model = IntradayTCN(window_size=6, hidden_channels=8, epochs=1, batch_size=4, require_cuda=False)
    model.fit(X, y)

    path = tmp_path / "causal_intraday_tcn"
    model.save(path)
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["backbone_kind"] == "causal_conv_fallback"

    loaded = IntradayTCN.load(path)
    assert loaded.backbone_kind == "causal_conv_fallback"
    out = loaded.predict_proba(X)
    assert list(out.columns) == ["p0", "p1"]
    assert len(out) == len(X)


def test_intraday_tcn_rejects_legacy_noncausal_fallback_without_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    X, _ = _frame()
    path = _legacy_artifact(tmp_path, X)
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    meta["backbone_kind"] = "legacy_conv_fallback"
    (path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    monkeypatch.delenv("FXSTACK_INTRADAY_TCN_ALLOW_NONCAUSAL_FALLBACK", raising=False)

    with pytest.raises(RuntimeError, match="legacy non-causal IntradayTCN fallback artifact"):
        IntradayTCN.load(path)


def test_intraday_tcn_legacy_noncausal_fallback_loads_with_explicit_opt_in(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    X, _ = _frame()
    path = _legacy_artifact(tmp_path, X)
    monkeypatch.setenv("FXSTACK_INTRADAY_TCN_ALLOW_NONCAUSAL_FALLBACK", "1")

    loaded = IntradayTCN.load(path)
    assert loaded.backbone_kind == "legacy_conv_fallback"
    out = loaded.predict_proba(X)
    assert len(out) == len(X)
