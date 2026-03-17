from __future__ import annotations

import time

from src.trader.interfaces.dto import RiskEnvelopeState


def _clip(val: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, val)))


def compute_adaptive_risk_envelope(
    *,
    volatility: float,
    trend_prob: float,
    soft_band: tuple[float, float] = (0.06, 0.09),
    hard_band: tuple[float, float] = (0.10, 0.12),
    daily_band: tuple[float, float] = (0.02, 0.03),
    now_ts: float | None = None,
) -> RiskEnvelopeState:
    """
    Adapt risk thresholds based on volatility and regime confidence.

    Higher volatility and weaker trend confidence tighten drawdown limits.
    """
    now = float(time.time() if now_ts is None else now_ts)
    vol = float(max(0.0, volatility))
    p_trend = _clip(float(trend_prob), 0.0, 1.0)

    # Normalize volatility into [0, 1] around common H1 FX ranges.
    vol_norm = _clip(vol / 0.01, 0.0, 1.0)

    # Confidence high -> slightly looser thresholds. Vol high -> tighter thresholds.
    relax = (p_trend - 0.5) * 0.20
    tighten = (vol_norm - 0.5) * 0.30
    mix = _clip(0.5 + relax - tighten, 0.0, 1.0)

    soft_dd = soft_band[0] + (soft_band[1] - soft_band[0]) * mix
    hard_dd = hard_band[0] + (hard_band[1] - hard_band[0]) * mix
    daily = daily_band[0] + (daily_band[1] - daily_band[0]) * mix

    if p_trend >= 0.62:
        regime = "trend"
    elif p_trend <= 0.38:
        regime = "range"
    else:
        regime = "transition"

    return RiskEnvelopeState(
        soft_dd_pct=float(soft_dd),
        hard_dd_pct=float(hard_dd),
        daily_breaker_pct=float(daily),
        regime=str(regime),
        volatility=float(vol),
        updated_at=now,
    )
