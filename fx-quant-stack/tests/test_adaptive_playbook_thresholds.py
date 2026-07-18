from __future__ import annotations

from fxstack.backtest.adaptive_policy import _adaptive_playbook_thresholds, evaluate_adaptive_entry


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
    assert sparse["adaptive_rejection_reason"] == "low_adaptive_quality"
    assert sparse["heuristic_penalty_score"] > complete["heuristic_penalty_score"]
    assert sparse["adaptive_entry_quality"] < complete["adaptive_entry_quality"]
