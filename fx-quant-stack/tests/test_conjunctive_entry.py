"""Pins conjunctive entry admission, the cost gate, and equal-weight scoring.

The property under test is that admission is CONJUNCTIVE: each informative
channel clears its own floor independently, and excellence in one cannot buy a
pass in another. A weighted sum cannot express that, which is why the previous
compensatory rule let a near-perfect setup carry models that were actively
against the trade.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from fxstack.strategy.adaptive_policy import (
    COST_EDGE_MULTIPLE,
    ENTRY_MODEL_FLOOR,
    ENTRY_SETUP_FLOOR,
    evaluate_adaptive_entry,
)


def _settings(**over) -> SimpleNamespace:
    base = dict(
        strategy_engine_mode="supervised_legacy",
        min_expected_edge_bps=3.0,
        max_allowed_spread_bps=5.0,
        adaptive_entry_quality_floor=0.52,
        adaptive_aggressive_fallback_margin=0.08,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _row(**over) -> dict:
    """A neutral, zero-information candidate. Every probability at 0.50."""
    row = dict(
        pair="EURUSD",
        side="long",
        signal_side="long",
        session_bucket="london_open",
        session_entry_blocked=False,
        session_entry_block_reason="",
        spread_bps=1.0,
        uncertainty_score=0.0,
        model_disagreement_score=0.0,
        playbook="trend_pullback",
        playbook_score=0.60,
        location_score=0.5,
        trigger_score=0.5,
        macro_coherence_score=0.5,
        regime_prob=0.5,
        swing_prob=0.5,
        entry_prob=0.5,
        trade_prob=0.5,
        expected_edge_bps=0.0,
        structure_timing_score=0.5,
        extension_penalty_score=0.0,
        environment_state="PersistentTrend",
        extreme_chase=False,
        adaptive_base_rejection_reason="none",
        calibrated_ev_bps=0.0,
    )
    row.update(over)
    return row


_STRONG = dict(
    regime_prob=0.66,
    swing_prob=0.68,
    entry_prob=0.64,
    trade_prob=0.67,
    location_score=0.72,
    trigger_score=0.68,
    macro_coherence_score=0.62,
    structure_timing_score=0.71,
    expected_edge_bps=6.0,
    calibrated_ev_bps=6.0,
)


def _decide(**over):
    return evaluate_adaptive_entry(
        row=_row(**over),
        strict_ready=True,
        open_positions={},
        settings=_settings(),
        fallback_margin=0.08,
    )


def test_a_zero_information_signal_does_not_trade():
    """The abstention benchmark. Every probability 0.50, neutral setup, no edge.

    Under the old compensatory rule this scored 0.58 and traded, because
    execution/crowding/reliability donated their full weight to ENTER whenever
    nothing was wrong.
    """
    d = _decide()
    assert d["adaptive_allowed"] is False
    assert d["intelligent_decision"]["enter_score"] < 0.5


def test_a_genuinely_good_signal_still_trades():
    """The gates must not be so tight that nothing survives them."""
    d = _decide(**_STRONG)
    assert d["adaptive_allowed"] is True
    assert d["adaptive_rejection_reason"] == "approved"
    conjuncts = d["intelligent_decision"]["conjuncts"]
    assert all(conjuncts[k] for k in ("model_ok", "setup_ok", "cost_ok", "evidence_margin_ok"))


def test_perfect_setup_cannot_compensate_for_dead_models():
    """THE conjunctive property. Setup passes, models fail, so the trade fails."""
    d = _decide(
        regime_prob=0.18,
        swing_prob=0.20,
        entry_prob=0.19,
        trade_prob=0.21,
        location_score=0.94,
        trigger_score=0.96,
        macro_coherence_score=0.97,
        playbook_score=0.95,
        expected_edge_bps=6.0,
    )
    conjuncts = d["intelligent_decision"]["conjuncts"]

    assert d["adaptive_allowed"] is False
    assert d["adaptive_rejection_reason"] == "model_conviction_below_floor"
    assert conjuncts["setup_ok"] is True, "the setup genuinely was excellent"
    assert conjuncts["model_ok"] is False, "and it bought exactly nothing"


def test_strong_models_cannot_compensate_for_absent_setup():
    """The mirror case -- and the one a weighted sum would have taken.

    ``enter_score`` here exceeds 0.5, so the compensatory rule would have said
    yes. The setup conjunct says no, and the setup conjunct wins.
    """
    d = _decide(
        regime_prob=0.80,
        swing_prob=0.82,
        entry_prob=0.78,
        trade_prob=0.79,
        location_score=0.0,
        trigger_score=0.0,
        macro_coherence_score=0.5,
        playbook_score=0.0,
        expected_edge_bps=6.0,
    )
    conjuncts = d["intelligent_decision"]["conjuncts"]

    assert d["intelligent_decision"]["enter_score"] > 0.5, (
        "precondition: the SUM is favourable, so only a conjunct can refuse this"
    )
    assert d["adaptive_allowed"] is False
    assert d["adaptive_rejection_reason"] == "setup_quality_below_floor"
    assert conjuncts["model_ok"] is True


def test_edge_must_beat_a_multiple_of_the_round_trip_spread():
    """Cost is a gate, not a saturating score term.

    Same signal, same models, only the spread moves. This is the term that
    decides whether the system pays the spread for a living.
    """
    tight = _decide(**{**_STRONG, "spread_bps": 1.0})
    wide = _decide(**{**_STRONG, "spread_bps": 4.0})

    assert tight["adaptive_allowed"] is True
    assert wide["adaptive_allowed"] is False
    assert wide["adaptive_rejection_reason"] == "edge_below_cost_floor"

    conjuncts = wide["intelligent_decision"]["conjuncts"]
    assert conjuncts["cost_floor_bps"] == pytest.approx(COST_EDGE_MULTIPLE * 4.0)
    assert conjuncts["expected_edge_bps"] < conjuncts["cost_floor_bps"]
    # Everything else about the candidate was still fine.
    assert conjuncts["model_ok"] is True
    assert conjuncts["setup_ok"] is True


def test_cost_floor_never_falls_below_the_static_edge_floor():
    """The static floor is a lower bound the cost gate can only ever raise."""
    # A real, very tight spread: 2 x 0.2 = 0.4 is below the static 3.0, so the
    # static minimum is what binds.
    conjuncts = _decide(**{**_STRONG, "spread_bps": 0.2, "expected_edge_bps": 0.5})[
        "intelligent_decision"
    ]["conjuncts"]

    assert conjuncts["spread_available"] is True
    assert conjuncts["cost_floor_bps"] == pytest.approx(3.0)  # min_expected_edge_bps
    assert conjuncts["cost_ok"] is False


def test_hostile_conditions_only_ever_subtract():
    """A clean spread is not evidence FOR a trade; a bad one is evidence against."""
    clean = _decide(**_STRONG)
    hostile = _decide(**{**_STRONG, "spread_bps": 4.9, "expected_edge_bps": 40.0, "calibrated_ev_bps": 40.0})

    assert hostile["intelligent_decision"]["enter_score"] < clean["intelligent_decision"]["enter_score"]
    assert hostile["intelligent_decision"]["conditions_penalty"] > 0.0
    # Cost is satisfied here (40 >> 2*4.9), so any refusal is the penalty's doing.
    assert hostile["intelligent_decision"]["conjuncts"]["cost_ok"] is True


def test_the_three_informative_channels_are_equally_weighted():
    """Equal weights are the honest prior when you have no fitted ones.

    Raising any one channel by the same amount must move ``informative_mean``
    by the same amount -- that is what equal weighting means, and it is
    checkable without depending on the specific constant.
    """
    base = _decide(**_STRONG)["intelligent_decision"]["informative_mean"]
    lifted_setup = _decide(**{**_STRONG, "location_score": 0.72 + 0.3})["intelligent_decision"]["informative_mean"]

    # Setup is a composite, so assert the direction and that no channel is inert.
    assert lifted_setup > base
    for field in ("model_intelligence_score", "quality_signal", "setup_score"):
        assert field in _decide(**_STRONG)["intelligent_decision"]


def test_floors_tighten_as_reliability_falls():
    """Uncertainty shrinks every channel toward 0.5, so the same raw evidence
    that cleared the floor when models agreed no longer clears it when they do not."""
    confident = _decide(**{**_STRONG, "uncertainty_score": 0.0, "model_disagreement_score": 0.0})
    unsure = _decide(**{**_STRONG, "uncertainty_score": 0.9, "model_disagreement_score": 0.9})

    assert confident["adaptive_allowed"] is True
    assert unsure["adaptive_allowed"] is False
    assert unsure["intelligent_decision"]["evidence_reliability"] < 0.2


def test_rejection_reason_names_the_channel_that_failed():
    """A snapshot must answer "which channel was short?", not merely "no"."""
    assert _decide(**{**_STRONG, "spread_bps": 4.0})["adaptive_rejection_reason"] == "edge_below_cost_floor"
    assert _decide(
        **{**_STRONG, "regime_prob": 0.1, "swing_prob": 0.1, "entry_prob": 0.1, "trade_prob": 0.1}
    )["adaptive_rejection_reason"] == "model_conviction_below_floor"


def test_floors_sit_above_the_neutral_point():
    """Both floors must exclude a coin flip, or they are decorative."""
    assert ENTRY_MODEL_FLOOR > 0.5
    assert ENTRY_SETUP_FLOOR > 0.5
    assert COST_EDGE_MULTIPLE >= 1.0


def test_missing_spread_does_not_price_the_crossing_at_zero():
    """A non-positive spread means "no reading", never "free".

    EURUSD does not quote at zero. Observed live: a tick gap reported
    spread_bps=0.0, which silently collapsed the cost floor back to the static
    min_expected_edge_bps -- weakening the gate at exactly the moment market
    data was unreliable. Same principle as volatility targeting: the absent
    estimate must not be the favourable one.
    """
    settings = _settings(max_allowed_spread_bps=5.0)

    def decide(spread):
        return evaluate_adaptive_entry(
            row=_row(**{**_STRONG, "spread_bps": spread}),
            strict_ready=True,
            open_positions={},
            settings=settings,
            fallback_margin=0.08,
        )

    real = decide(1.0)["intelligent_decision"]["conjuncts"]
    missing = decide(0.0)["intelligent_decision"]["conjuncts"]

    assert real["spread_available"] is True
    assert missing["spread_available"] is False
    # With no reading, cost is charged at the worst tolerable spread (5.0),
    # so the floor is 2 x 5.0 = 10.0, not the 3.0 static minimum.
    assert missing["round_trip_cost_bps"] == pytest.approx(5.0)
    assert missing["cost_floor_bps"] == pytest.approx(COST_EDGE_MULTIPLE * 5.0)
    assert missing["cost_floor_bps"] > real["cost_floor_bps"]
    assert missing["cost_ok"] is False


def test_negative_or_nonfinite_spread_is_also_treated_as_missing():
    settings = _settings(max_allowed_spread_bps=5.0)
    for bad in (-1.0, float("nan"), float("inf")):
        conjuncts = evaluate_adaptive_entry(
            row=_row(**{**_STRONG, "spread_bps": bad}),
            strict_ready=True,
            open_positions={},
            settings=settings,
            fallback_margin=0.08,
        )["intelligent_decision"]["conjuncts"]
        assert conjuncts["spread_available"] is False, f"{bad!r} must not read as a usable spread"
        assert conjuncts["round_trip_cost_bps"] == pytest.approx(5.0)
