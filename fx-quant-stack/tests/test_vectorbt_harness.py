"""Tests for the vectorbt research harness (numpy-canonical, vbt-optional)."""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
from fxstack.improve.knobs import default_config
from fxstack.research.vectorbt_harness import run_vectorbt_research

_METRIC_KEYS = {
    "trades",
    "win_rate",
    "total_net_bps",
    "mean_net_bps",
    "sharpe",
    "sortino",
    "calmar",
    "max_drawdown_pct",
    "exposure",
    "profit_factor",
}


def _assert_finite_or_pos_inf(value: float) -> None:
    # profit_factor may legitimately be +inf (no losing trades); everything else
    # must be finite. Negative inf / NaN is never acceptable.
    assert not math.isnan(value)
    assert value != float("-inf")


def test_metrics_present_and_bounded() -> None:
    ds = build_synthetic_dataset(rows=3000, seed=7)
    out = run_vectorbt_research(default_config(), ds)

    assert _METRIC_KEYS.issubset(out)
    assert out["backend"] in {"numpy", "vectorbt"}

    assert isinstance(out["trades"], int) and out["trades"] >= 0
    assert 0.0 <= out["win_rate"] <= 1.0
    assert 0.0 <= out["exposure"] <= 1.0
    assert 0.0 <= out["max_drawdown_pct"] <= 100.0
    assert out["profit_factor"] >= 0.0
    for key in _METRIC_KEYS:
        _assert_finite_or_pos_inf(float(out[key]))


def test_deterministic() -> None:
    ds = build_synthetic_dataset(rows=2500, seed=11)
    a = run_vectorbt_research(default_config(), ds)
    b = run_vectorbt_research(default_config(), ds)
    assert a == b


def test_empty_frame_is_safe() -> None:
    out = run_vectorbt_research(default_config(), pd.DataFrame())
    assert out["trades"] == 0
    assert out["backend"] == "numpy"
    for key in _METRIC_KEYS:
        v = float(out[key])
        assert math.isfinite(v)
        assert v == 0.0


def test_none_frame_is_safe() -> None:
    out = run_vectorbt_research(default_config(), None)
    assert out["trades"] == 0
    assert all(math.isfinite(float(out[k])) for k in _METRIC_KEYS)


def test_zero_trade_frame_is_safe() -> None:
    # A real frame whose rows can never pass the gate (probabilities all below the
    # min gate, spread above the cap) must still yield finite zeros.
    ds = build_synthetic_dataset(rows=500, seed=3).copy()
    ds["swing_prob"] = 0.0
    ds["entry_prob"] = 0.0
    ds["trade_prob"] = 0.0
    ds["spread_bps"] = 99.0
    out = run_vectorbt_research(default_config(), ds)
    assert out["trades"] == 0
    assert out["exposure"] == 0.0
    for key in _METRIC_KEYS:
        assert math.isfinite(float(out[key]))


def test_trade_count_matches_evaluator() -> None:
    ds = build_synthetic_dataset(rows=4000, seed=1729)
    cfg = default_config()
    harness = run_vectorbt_research(cfg, ds)
    evaluated = evaluate_config(cfg, ds)
    assert harness["trades"] == int(evaluated["trades"])
    # The shared metrics computed both ways must agree to numerical tolerance.
    assert harness["win_rate"] == pytest.approx(evaluated["win_rate"], abs=1e-9)
    assert harness["mean_net_bps"] == pytest.approx(evaluated["mean_net_bps"], abs=1e-6)
    assert harness["total_net_bps"] == pytest.approx(evaluated["total_net_bps"], abs=1e-6)
    assert harness["sharpe"] == pytest.approx(evaluated["sharpe"], abs=1e-6)
    assert harness["max_drawdown_pct"] == pytest.approx(
        evaluated["max_drawdown_pct"], abs=1e-6
    )


def test_missing_required_column_raises() -> None:
    ds = build_synthetic_dataset(rows=100, seed=1).drop(columns=["fwd_ret_bps"])
    with pytest.raises(ValueError, match="missing columns"):
        run_vectorbt_research(default_config(), ds)


def test_tighter_gate_reduces_trades() -> None:
    ds = build_synthetic_dataset(rows=4000, seed=21)
    loose = default_config()
    tight = default_config()
    tight["gates"]["min_swing_prob"] = 0.80
    tight["gates"]["min_entry_prob"] = 0.85
    tight["gates"]["min_trade_prob"] = 0.85
    n_loose = run_vectorbt_research(loose, ds)["trades"]
    n_tight = run_vectorbt_research(tight, ds)["trades"]
    assert n_tight <= n_loose
    assert n_loose > 0


def test_profit_factor_no_losses_is_inf() -> None:
    # Construct a frame where every taken trade is a winner -> gross_loss == 0.
    n = 50
    ds = pd.DataFrame(
        {
            "swing_prob": np.full(n, 0.95),
            "entry_prob": np.full(n, 0.95),
            "trade_prob": np.full(n, 0.95),
            "expected_edge_bps": np.full(n, 20.0),
            "spread_bps": np.full(n, 1.0),
            "fwd_ret_bps": np.full(n, 50.0),
        }
    )
    out = run_vectorbt_research(default_config(), ds)
    assert out["trades"] == n
    assert out["profit_factor"] == float("inf")
    assert out["win_rate"] == 1.0
