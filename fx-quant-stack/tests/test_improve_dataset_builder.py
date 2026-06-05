"""Convert real scored features into the loop's scored-signals schema."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxstack.improve.dataset_builder import ColumnMap, build_scored_signals, write_scored_signals
from fxstack.improve.evaluator import REQUIRED_COLUMNS, evaluate_config
from fxstack.improve.knobs import default_config


def _feature_frame(n: int = 200) -> pd.DataFrame:
    rng = np.random.default_rng(0)
    return pd.DataFrame({
        "ts": pd.date_range("2024-01-01", periods=n, freq="h", tz="UTC"),
        "pair": np.where(rng.random(n) > 0.5, "EURUSD", "GBPUSD"),
        "p_swing": rng.uniform(0.4, 0.9, n),
        "p_entry": rng.uniform(0.4, 0.9, n),
        "p_trade": rng.uniform(0.4, 0.9, n),
        "spread": rng.uniform(0.00005, 0.0003, n),   # fraction (0.5-3 bps)
        "fwd_ret": rng.normal(0.0, 0.001, n),         # fraction
    })


def _cols() -> ColumnMap:
    return ColumnMap(swing_prob="p_swing", entry_prob="p_entry", trade_prob="p_trade",
                     spread="spread", fwd_ret="fwd_ret", pair="pair", ts="ts")


def test_build_produces_canonical_schema():
    out = build_scored_signals(_feature_frame(), columns=_cols())
    for col in REQUIRED_COLUMNS:
        assert col in out.columns
    assert (out["spread_bps"] >= 0).all()
    # 0.0001 fraction spread -> 1 bp
    assert out["spread_bps"].between(0.4, 3.1).all()


def test_units_convert_fraction_to_bps():
    df = _feature_frame(10)
    df["spread"] = 0.0001  # exactly 1 bp
    df["fwd_ret"] = 0.0010  # exactly 10 bps
    out = build_scored_signals(df, columns=_cols())
    assert np.allclose(out["spread_bps"], 1.0)
    assert np.allclose(out["fwd_ret_bps"], 10.0)


def test_expected_edge_derived_from_trade_prob_when_absent():
    df = _feature_frame(50)
    df["p_trade"] = 0.6
    out = build_scored_signals(df, columns=_cols(), edge_scale_bps=10.0)
    # (0.6 - 0.5) * 10 = 1.0 bps
    assert np.allclose(out["expected_edge_bps"], 1.0)


def test_explicit_edge_column_is_used():
    df = _feature_frame(20)
    df["edge_bps"] = 4.2
    cols = _cols()
    cols.expected_edge = "edge_bps"
    out = build_scored_signals(df, columns=cols)
    assert np.allclose(out["expected_edge_bps"], 4.2)


def test_missing_column_raises():
    df = _feature_frame().drop(columns=["p_trade"])
    with pytest.raises(ValueError):
        build_scored_signals(df, columns=_cols())


def test_output_feeds_evaluator():
    out = build_scored_signals(_feature_frame(500), columns=_cols())
    metrics = evaluate_config(default_config(), out)
    assert set(metrics) >= {"trades", "sharpe", "max_drawdown_pct"}


def test_write_roundtrip(tmp_path):
    out = build_scored_signals(_feature_frame(30), columns=_cols())
    info = write_scored_signals(out, tmp_path / "scored.parquet")
    assert info["ok"] is True
    assert info["rows"] == len(out)
    assert (tmp_path / "scored.parquet").exists()
