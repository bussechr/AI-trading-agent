from __future__ import annotations

from fxstack.strategy.adaptive_policy import _adaptive_playbook_thresholds, evaluate_adaptive_entry


def test_adaptive_playbook_thresholds_apply_slack_with_safe_floor() -> None:
    class SlackySettings:
        adaptive_playbook_threshold_slack = 0.03

    class AggressiveSlackSettings:
        adaptive_playbook_threshold_slack = 0.10

    thresholds = _adaptive_playbook_thresholds(SlackySettings())
    clamped = _adaptive_playbook_thresholds(AggressiveSlackSettings())

    assert thresholds["trend_pullback"] == 0.53
    assert thresholds["failed_breakout_reversal"] == 0.59
    assert clamped["trend_pullback"] == 0.50
    assert min(clamped.values()) >= 0.50


def test_adaptive_entry_fails_closed_when_risk_diagnostics_are_missing() -> None:
    class Settings:
        strategy_engine_mode = "supervised_legacy"
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0

    complete_row = {
        "pair": "EURUSD",
        "signal_side": "long",
        "session_bucket": "london",
        "playbook": "trend_pullback",
        "playbook_score": 0.74,
        "location_score": 0.72,
        "trigger_score": 0.69,
        "macro_coherence_score": 0.67,
        "environment_state": "PersistentTrend",
        "spread_bps": 1.0,
        "uncertainty_score": 0.08,
        "model_disagreement_score": 0.05,
        "structure_timing_score": 0.72,
        "extension_penalty_score": 0.12,
        "regime_prob": 0.78,
        "swing_prob": 0.76,
        "entry_prob": 0.74,
        "trade_prob": 0.73,
        "expected_edge_bps": 8.0,
    }
    complete = evaluate_adaptive_entry(
        row=complete_row,
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )
    sparse_row = dict(complete_row)
    for key in ("model_disagreement_score", "structure_timing_score", "extension_penalty_score"):
        sparse_row.pop(key)
    sparse = evaluate_adaptive_entry(
        row=sparse_row,
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )

    assert complete["adaptive_allowed"] is True
    assert sparse["adaptive_allowed"] is False
    assert sparse["adaptive_rejection_reason"] == "missing_intelligent_evidence"
    assert sparse["intelligent_decision"]["missing_evidence_fields"] == [
        "model_disagreement_score",
        "structure_timing_score",
        "extension_penalty_score",
    ]
    assert sparse["heuristic_penalty_score"] > complete["heuristic_penalty_score"]
    assert sparse["adaptive_entry_quality"] < complete["adaptive_entry_quality"]


def _trend_probe_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "pair": "EURUSD",
        "signal_side": "long",
        "session_bucket": "london",
        "playbook": "no_trade",
        "playbook_score": 0.40,
        "location_score": 0.20,
        "trigger_score": 0.20,
        "macro_coherence_score": 0.90,
        "environment_state": "PersistentTrend",
        "trend_persistence_score": 0.90,
        "htf_alignment_score": 0.90,
        "spread_bps": 1.0,
        "uncertainty_score": 0.20,
        "model_disagreement_score": 0.10,
        "structure_timing_score": 0.80,
        "extension_penalty_score": 0.20,
        "regime_prob": 0.90,
        "swing_prob": 0.90,
        "directional_swing_confidence": 0.90,
        "entry_prob": 0.30,
        "trade_prob": 0.30,
        "expected_edge_bps": 5.0,
        "baseline_rejection_reason": "low_entry_prob",
    }
    row.update(overrides)
    return row


def test_intelligent_entry_has_no_probability_cliff() -> None:
    class Settings:
        strategy_engine_mode = "supervised_legacy"
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_playbooks = ["trend_pullback"]

    accepted = evaluate_adaptive_entry(
        row=_trend_probe_row(),
        strict_ready=False,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )
    below_floor = evaluate_adaptive_entry(
        row=_trend_probe_row(entry_prob=0.299),
        strict_ready=False,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )
    above_old_floor = evaluate_adaptive_entry(
        row=_trend_probe_row(entry_prob=0.301),
        strict_ready=False,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )

    assert accepted["adaptive_allowed"] is True
    assert accepted["adaptive_entry_mode"] == "intelligent_utility"
    assert accepted["adaptive_trend_probe_used"] is False
    assert accepted["playbook"] == "trend_pullback"
    assert accepted["adaptive_recovered_strict_reasons"] == ["low_entry_prob"]
    assert below_floor["adaptive_allowed"] is True
    assert above_old_floor["adaptive_allowed"] is True
    assert abs(
        below_floor["intelligent_decision"]["enter_score"]
        - above_old_floor["intelligent_decision"]["enter_score"]
    ) < 0.01


def test_strategy_reasons_are_evidence_not_independent_vetoes() -> None:
    class Settings:
        strategy_engine_mode = "supervised_legacy"
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_playbooks = ["trend_pullback"]

    for row in (
        _trend_probe_row(spread_bps=3.0),
        _trend_probe_row(uncertainty_score=0.30),
        _trend_probe_row(baseline_rejection_reason="edge_below_hurdle"),
        _trend_probe_row(baseline_rejection_reason="low_swing_prob"),
        _trend_probe_row(session_entry_blocked=True),
    ):
        decision = evaluate_adaptive_entry(
            row=row,
            strict_ready=False,
            open_positions={},
            settings=Settings(),
            fallback_margin=0.08,
        )
        assert decision["adaptive_allowed"] is True
        assert decision["intelligent_decision"]["selected_action"] == "enter"


def test_intelligent_entry_selects_abstention_from_combined_counterevidence() -> None:
    class Settings:
        strategy_engine_mode = "supervised_legacy"
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_playbooks = ["trend_pullback"]

    decision = evaluate_adaptive_entry(
        row=_trend_probe_row(
            playbook_score=0.10,
            location_score=0.10,
            trigger_score=0.10,
            macro_coherence_score=0.10,
            trend_persistence_score=0.10,
            htf_alignment_score=0.10,
            regime_prob=0.15,
            swing_prob=0.15,
            directional_swing_confidence=0.15,
            entry_prob=0.15,
            trade_prob=0.15,
            expected_edge_bps=-8.0,
            uncertainty_score=0.90,
            model_disagreement_score=0.90,
            structure_timing_score=0.10,
            extension_penalty_score=0.90,
            session_entry_blocked=True,
        ),
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is False
    assert decision["adaptive_rejection_reason"] == "intelligent_no_trade"
    assert decision["intelligent_decision"]["no_trade_score"] > decision["intelligent_decision"]["enter_score"]


def test_playbook_allowlist_remains_an_operator_scope_not_a_quality_gate() -> None:
    class Settings:
        strategy_engine_mode = "supervised_legacy"
        max_allowed_spread_bps = 2.5
        min_expected_edge_bps = 3.0
        adaptive_playbooks = ["trend_pullback"]

    trend = evaluate_adaptive_entry(
        row=_trend_probe_row(
            playbook="trend_pullback",
            playbook_score=0.90,
            location_score=0.90,
            trigger_score=0.90,
            entry_prob=0.90,
            trade_prob=0.90,
            baseline_rejection_reason="",
        ),
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )
    nontrend = evaluate_adaptive_entry(
        row=_trend_probe_row(
            playbook="range_mean_reversion",
            playbook_score=0.90,
            location_score=0.90,
            trigger_score=0.90,
            environment_state="BalancedRange",
            trend_persistence_score=0.30,
            entry_prob=0.90,
            trade_prob=0.90,
            baseline_rejection_reason="",
        ),
        strict_ready=True,
        open_positions={},
        settings=Settings(),
        fallback_margin=0.08,
    )

    assert trend["adaptive_allowed"] is True
    assert trend["adaptive_entry_mode"] == "intelligent_utility"
    assert trend["playbook"] == "trend_pullback"
    assert nontrend["adaptive_allowed"] is False
    assert nontrend["adaptive_rejection_reason"] == "playbook_scope_blocked"
