from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import numpy as np
import pandas as pd

from fxstack.live.policy import compute_expected_edge_bps, compute_structure_timing_diagnostics, gate_decision, normalize_spread_bps


REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL_PATH = REPO_ROOT / "tools" / "fxstack_digital_twin_backtest.py"
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))


def _load_twin_module():
    spec = importlib.util.spec_from_file_location("fxstack_digital_twin_backtest_policy_test", TOOL_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_compute_expected_edge_bps_from_ret_1() -> None:
    out = compute_expected_edge_bps({"ret_1": 0.0012})
    assert round(float(out), 6) == 12.0


def test_structure_timing_uses_finite_htf_values_only() -> None:
    mod = _load_twin_module()
    row = {
        "trend_slope_60": 0.0030,
        "trend_strength_60": 1.5,
        "h1_trend_slope_20": float("nan"),
        "h4_trend_slope_20": float("nan"),
        "d_trend_slope_20": float("nan"),
        "h1_trend_strength_20": float("nan"),
        "h4_trend_strength_20": float("nan"),
        "d_trend_strength_20": float("nan"),
    }

    live = compute_structure_timing_diagnostics(row, side="long")
    twin = mod._htf_alignment_score_series(pd.DataFrame([row]), side_sign=np.array([1.0], dtype=float))

    assert float(live.htf_alignment_score) == 1.0
    assert float(twin.iloc[0]) == 1.0
    assert float(live.htf_alignment_score) == float(twin.iloc[0])


def test_normalize_spread_bps_from_price_units_eurusd() -> None:
    spread_bps, source = normalize_spread_bps(row={"pair": "EURUSD", "mid_close": 1.1000, "spread": 0.00011})
    assert source == "row.spread_price"
    assert round(float(spread_bps), 6) == 1.0


def test_normalize_spread_bps_from_tick_pips_usdjpy() -> None:
    spread_bps, source = normalize_spread_bps(
        tick={"symbol": "USDJPY", "bid": 150.0, "ask": 150.006, "spread_pips": 0.6, "digits": 3},
        pair="USDJPY",
    )
    assert source == "tick.spread_pips"
    assert float(spread_bps) > 0.0


def test_gate_decision_emits_threshold_snapshot() -> None:
    out = gate_decision(
        swing_prob=0.7,
        entry_prob=0.7,
        trade_prob=0.7,
        spread_bps=0.6,
        expected_edge_bps=6.0,
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        spread_unit_source="tick.spread_bps",
    )
    assert out.allowed is True
    assert out.reason == "approved"
    assert float(out.threshold_snapshot["max_spread_bps"]) == 2.5
    assert out.spread_unit_source == "tick.spread_bps"


def test_gate_decision_allows_small_edge_shortfall_with_rescue_margin() -> None:
    tolerated = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=2.8,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        min_expected_edge_rescue_margin_bps=0.5,
        spread_unit_source="tick.spread_bps",
    )
    blocked = gate_decision(
        swing_prob=0.74,
        entry_prob=0.76,
        trade_prob=0.75,
        spread_bps=0.6,
        expected_edge_bps=2.3,
        side="long",
        min_swing_prob=0.58,
        min_entry_prob=0.62,
        min_trade_prob=0.60,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
        min_expected_edge_rescue_margin_bps=0.5,
        spread_unit_source="tick.spread_bps",
    )

    assert tolerated.allowed is True
    assert tolerated.reason == "approved"
    assert float(tolerated.threshold_snapshot["min_expected_edge_rescue_margin_bps"]) == 0.5
    assert blocked.allowed is False
    assert blocked.reason == "edge_below_hurdle"
