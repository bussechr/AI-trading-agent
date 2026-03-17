from __future__ import annotations

from src.trader.domain.risk_envelope import compute_adaptive_risk_envelope


def test_risk_envelope_within_bands():
    env = compute_adaptive_risk_envelope(volatility=0.02, trend_prob=0.8)
    assert 0.06 <= float(env.soft_dd_pct) <= 0.09
    assert 0.10 <= float(env.hard_dd_pct) <= 0.12
    assert 0.02 <= float(env.daily_breaker_pct) <= 0.03


def test_risk_envelope_regime_labels():
    trend = compute_adaptive_risk_envelope(volatility=0.004, trend_prob=0.75)
    range_ = compute_adaptive_risk_envelope(volatility=0.004, trend_prob=0.25)
    trans = compute_adaptive_risk_envelope(volatility=0.004, trend_prob=0.50)
    assert trend.regime == "trend"
    assert range_.regime == "range"
    assert trans.regime == "transition"


def test_risk_envelope_high_vol_tightens_limits():
    low_vol = compute_adaptive_risk_envelope(volatility=0.002, trend_prob=0.5)
    high_vol = compute_adaptive_risk_envelope(volatility=0.02, trend_prob=0.5)
    assert float(high_vol.soft_dd_pct) <= float(low_vol.soft_dd_pct)
    assert float(high_vol.hard_dd_pct) <= float(low_vol.hard_dd_pct)
    assert float(high_vol.daily_breaker_pct) <= float(low_vol.daily_breaker_pct)
