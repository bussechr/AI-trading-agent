"""Safety tests for the change-set allowlist -- the 'code disposes' core."""

from __future__ import annotations

from fxstack.improve.knobs import (
    KNOBS_BY_NAME,
    apply_change_set,
    default_config,
    knob_values,
    validate_change_set,
)


def _base() -> dict:
    return {
        "gates": {
            "min_swing_prob": 0.58,
            "min_entry_prob": 0.62,
            "min_trade_prob": 0.60,
            "min_expected_edge_bps": 3.0,
            "min_expected_edge_rescue_margin_bps": 0.5,
            "max_allowed_spread_bps": 3.0,
        },
        "cost_model": {"slippage_bps": 0.25},
        "risk": {
            "max_total_positions": 5,
            "max_pair_positions": 2,
            "default_order_lots": 0.08,
            "max_pair_exposure": 0.015,
            "max_total_exposure": 0.05,
        },
        "portfolio": {"max_realized_corr_share": 0.75},
    }


def test_unknown_knob_is_rejected_not_applied():
    res = validate_change_set({"evil_knob": 999, "min_swing_prob": 0.66}, incumbent=_base())
    assert "evil_knob" not in res.sanitized
    assert {"knob": "evil_knob", "reason": "unknown_knob"} in res.rejected
    assert res.sanitized["min_swing_prob"] == 0.66


def test_values_are_clamped_to_hard_bounds():
    res = validate_change_set({"min_swing_prob": 5.0, "min_entry_prob": -1.0}, incumbent=_base())
    assert res.sanitized["min_swing_prob"] == KNOBS_BY_NAME["min_swing_prob"].hi
    assert res.sanitized["min_entry_prob"] == KNOBS_BY_NAME["min_entry_prob"].lo
    assert any(a["reason"] == "clamped_to_bounds" for a in res.adjusted)


def test_risk_caps_may_not_loosen_vs_incumbent():
    base = _base()
    # Try to ENLARGE position size, position count, and exposure -- all must be blocked
    # back to the incumbent value (which is below the hard bound here).
    res = validate_change_set(
        {
            "default_order_lots": 0.10,
            "max_total_positions": 6,
            "max_total_exposure": 0.06,
            "max_allowed_spread_bps": 5.0,
        },
        incumbent=base,
    )
    assert res.sanitized["default_order_lots"] == base["risk"]["default_order_lots"]
    assert res.sanitized["max_total_positions"] == base["risk"]["max_total_positions"]
    assert res.sanitized["max_total_exposure"] == base["risk"]["max_total_exposure"]
    assert res.sanitized["max_allowed_spread_bps"] == base["gates"]["max_allowed_spread_bps"]
    assert all(a["reason"] == "risk_loosening_blocked" for a in res.adjusted)


def test_risk_caps_may_tighten():
    base = _base()
    res = validate_change_set(
        {"default_order_lots": 0.05, "max_total_positions": 3, "max_allowed_spread_bps": 2.0},
        incumbent=base,
    )
    assert res.sanitized["default_order_lots"] == 0.05
    assert res.sanitized["max_total_positions"] == 3
    assert res.sanitized["max_allowed_spread_bps"] == 2.0


def test_apply_change_set_is_pure():
    base = _base()
    applied = apply_change_set(base, {"min_swing_prob": 0.7})
    assert applied["gates"]["min_swing_prob"] == 0.7
    assert base["gates"]["min_swing_prob"] == 0.58  # original untouched


def test_knob_values_roundtrip_default_config():
    cfg = default_config()
    values = knob_values(cfg)
    # Every registered knob present in the default config is read back.
    assert values["min_swing_prob"] == cfg["gates"]["min_swing_prob"]
    assert values["max_total_positions"] == cfg["risk"]["max_total_positions"]
    assert set(values).issubset(set(KNOBS_BY_NAME))


def test_int_knobs_coerce_to_int():
    res = validate_change_set({"max_total_positions": 3.9}, incumbent=_base())
    assert res.sanitized["max_total_positions"] == 4
    assert isinstance(res.sanitized["max_total_positions"], int)
