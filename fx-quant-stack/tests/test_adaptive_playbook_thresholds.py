from __future__ import annotations

from fxstack.backtest.adaptive_policy import _adaptive_playbook_thresholds


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
