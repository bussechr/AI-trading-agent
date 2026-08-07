# AGENT: ROLE: Signal families and the shared bracket arithmetic every family must survive.
# AGENT: ENTRYPOINT: `evaluate_signal` (dispatches by config.signal_family).
# AGENT: PRIMARY INPUTS: the unbroken run of valid engine-timeframe bars + the live spread.
# AGENT: PRIMARY OUTPUTS: (ScalpIntent | None, reason).
# AGENT: CALLED BY: `fxstack/scalp/loop.py`, `fxstack/scalp/backtest.py`.
"""Signal families.

The blind 2026 run falsified M1 price-dislocation in both directions: mean R
was negative fading AND joining, so the feature carried no directional
information at that horizon. Two consequences shape this module:

1. A family is a REPLACEABLE hypothesis, not the engine. Adding one must not
   touch gates, sizing, fills or authority -- only this file.
2. Every family, whatever it believes, faces the identical economics: the
   bracket must clear p* at the LIVE spread AND deliver gross worth at least
   ``min_tp_cost_ratio`` times the round-trip cost. A family that can only
   pay at zero cost is refused here, not discovered in production.

Families implemented:

- ``dislocation``: z-score of close vs EMA in ATR units, faded (revert) or
  joined (momentum) on a confirming bar. Kept because it is the falsified
  baseline every new family must beat -- deleting it would erase the control.
- ``opening_range``: the session-open range (first N bars after an open hour)
  is a structural level -- a close beyond it is a breakout entry with the
  stop at the range's far side. The blind run's gate histogram showed viable
  spreads concentrate in exactly these hours, which is why this family is
  first in the queue rather than another price-only oscillator.
- ``xs_residual``: an experimental complete-panel relative-value signal.  Its
  original cross-rate formula was invalid and its corrected form has not
  produced search-corrected information or tradable edge; it remains an
  explicit falsified/research control and is not live-dispatched.
"""

from __future__ import annotations

import datetime as dt

from fxstack.scalp.bars import M1Bar, atr_bps, ema
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.signals import ScalpIntent


def build_intent(
    *,
    last: M1Bar,
    side: str,
    stop_bps: float,
    tp_bps: float,
    spread_bps: float,
    config: ScalpConfig,
    atr: float,
    signal_strength: float,
    execution_debit_bps: float = 0.0,
) -> tuple[ScalpIntent | None, str]:
    """Apply the shared economics gates and assemble the bracket.

    Every family funnels through here so viability is defined once: the
    breakeven win rate must be reachable, and the gross must be worth more
    than a few spreads. Both are measured with the LIVE cost, never assumed.
    """
    spread_cost = max(0.0, float(spread_bps))
    recorded_cost = spread_cost + max(0.0, float(execution_debit_bps))
    stop_bps = max(float(stop_bps), config.min_stop_bps)
    if tp_bps <= 0.0 or stop_bps <= 0.0:
        return None, "degenerate_bracket"
    p_star = (stop_bps + recorded_cost) / (tp_bps + stop_bps)
    if p_star > config.p_star_max:
        return None, "bracket_cost_dead"
    if config.min_tp_cost_ratio > 0.0 and recorded_cost > 0.0:
        if tp_bps < config.min_tp_cost_ratio * recorded_cost:
            # Upside only a few spreads wide: even a winning trade barely
            # clears the friction, and slippage owns the rest.
            return None, "gross_too_small_vs_cost"
    mid = last.close
    entry = last.ask_close if side == "BUY" else last.bid_close
    if entry <= 0.0 or mid <= 0.0:
        return None, "no_entry_quote"
    stop_px = stop_bps / 1e4 * mid
    tp_px = tp_bps / 1e4 * mid
    if side == "BUY":
        sl_price, tp_price = entry - stop_px, entry + tp_px
    else:
        sl_price, tp_price = entry + stop_px, entry - tp_px
    return (
        ScalpIntent(
            symbol=last.symbol,
            side=side,
            minute_epoch=last.minute_epoch,
            ref_mid=mid,
            entry_price=entry,
            sl_price=sl_price,
            tp_price=tp_price,
            atr_bps=atr,
            stop_bps=stop_bps,
            disp_z=signal_strength,
            spread_bps=spread_cost,
            p_star=p_star,
            time_stop_bars=config.time_stop_bars,
        ),
        "",
    )


def evaluate_dislocation_family(
    *, bars: list[M1Bar], config: ScalpConfig, spread_bps: float
) -> tuple[ScalpIntent | None, str]:
    """Z-score dislocation, faded or joined. The falsified control family."""
    if len(bars) < config.min_history_bars:
        return None, "insufficient_valid_history"
    last = bars[-1]
    atr = atr_bps(bars, periods=config.atr_bars)
    if atr < config.atr_floor_bps:
        return None, "no_volatility_estimate"
    mean = ema([b.close for b in bars], periods=config.ema_bars)
    if mean <= 0.0 or last.close <= 0.0:
        return None, "degenerate_prices"
    disp_z = ((last.close - mean) / mean * 1e4) / atr
    if abs(disp_z) < config.z_entry:
        return None, "no_dislocation"
    bar_dir = last.close - last.open
    if config.signal_mode == "momentum":
        if disp_z > 0 and bar_dir <= 0:
            return None, "no_continuation_trigger"
        if disp_z < 0 and bar_dir >= 0:
            return None, "no_continuation_trigger"
        side = "BUY" if disp_z > 0 else "SELL"
    else:
        if disp_z > 0 and bar_dir >= 0:
            return None, "no_reversion_trigger"
        if disp_z < 0 and bar_dir <= 0:
            return None, "no_reversion_trigger"
        side = "SELL" if disp_z > 0 else "BUY"
    recorded_cost = max(0.0, float(spread_bps)) + max(
        0.0, float(config.execution_debit_bps)
    )
    return build_intent(
        last=last,
        side=side,
        stop_bps=config.stop_cost_multiple * recorded_cost,
        tp_bps=config.target_cost_multiple * recorded_cost,
        spread_bps=spread_bps,
        config=config,
        atr=atr,
        signal_strength=disp_z,
        execution_debit_bps=config.execution_debit_bps,
    )


def evaluate_opening_range(
    *, bars: list[M1Bar], config: ScalpConfig, spread_bps: float
) -> tuple[ScalpIntent | None, str]:
    """Session opening-range breakout.

    The range is built from the first ``or_bars`` engine bars whose minute
    falls at or after a configured session-open hour. A close beyond that
    range (by a buffer, so a wick poke is not a signal) within
    ``or_valid_bars`` of the open is the entry; the stop sits at the far side
    of the range, floored by the broker minimum, and the target is an ATR
    multiple of the same geometry every other family uses.
    """
    if len(bars) < config.min_history_bars:
        return None, "insufficient_valid_history"
    last = bars[-1]
    atr = atr_bps(bars, periods=config.atr_bars)
    if atr < config.atr_floor_bps:
        return None, "no_volatility_estimate"

    now = dt.datetime.fromtimestamp(last.minute_epoch, dt.timezone.utc)
    session_start = _session_start_epoch(now, config.or_open_hours_utc)
    if session_start is None:
        return None, "outside_opening_range_session"
    step = 60 * max(1, config.bar_minutes)
    bars_since_open = (last.minute_epoch - session_start) // step
    if bars_since_open < config.or_bars:
        return None, "opening_range_forming"
    if bars_since_open >= config.or_valid_bars:
        return None, "opening_range_window_closed"

    window = [
        b
        for b in bars
        if session_start <= b.minute_epoch < session_start + config.or_bars * step
    ]
    if len(window) < config.or_bars:
        # The range itself must be complete and unbroken; a data gap inside
        # the opening range makes the level unmeasured, not merely noisy.
        return None, "opening_range_incomplete"
    hi = max(b.high for b in window)
    lo = min(b.low for b in window)
    if hi <= lo or lo <= 0.0:
        return None, "degenerate_opening_range"
    range_px = hi - lo
    buffer_px = range_px * config.or_buffer_frac

    if last.close > hi + buffer_px:
        side = "BUY"
    elif last.close < lo - buffer_px:
        side = "SELL"
    else:
        return None, "inside_opening_range"

    # The RANGE is this family's unit of risk and reward: risk a fraction of
    # it (a failed breakout is proven by re-entering the range), target a
    # multiple of it (the range measures the session's contested distance).
    range_bps = range_px / last.close * 1e4
    stop_bps = config.or_stop_range_frac * range_bps
    tp_bps = config.or_tp_range_mult * range_bps
    strength = range_bps / atr if atr > 0 else 0.0
    return build_intent(
        last=last,
        side=side,
        stop_bps=stop_bps,
        tp_bps=tp_bps,
        spread_bps=spread_bps,
        config=config,
        atr=atr,
        signal_strength=strength,
    )


def _session_start_epoch(now: dt.datetime, open_hours: list[int]) -> int | None:
    """Epoch of the most recent configured session open at or before ``now``."""
    best: int | None = None
    for hour in open_hours:
        start = now.replace(hour=int(hour), minute=0, second=0, microsecond=0)
        if start > now:
            continue
        candidate = int(start.timestamp())
        if best is None or candidate > best:
            best = candidate
    return best


def evaluate_xs_residual(
    *,
    bars: list[M1Bar],
    config: ScalpConfig,
    spread_bps: float,
    features: dict[str, float] | None,
) -> tuple[ScalpIntent | None, str]:
    """Experimental cross-sectional residual reversion.

    ``residual`` is what this pair did BEYOND the move the dollar implied for
    it.  The first implementation accidentally reduced non-USD crosses to
    their own trailing return and let USD targets contaminate their factor.
    Those results are invalid.  The corrected feature is kept so subsequent
    screens and backtests can falsify the intended hypothesis explicitly: a
    positive residual is faded with a SELL, and vice versa.

    The signal is unusable without a complete cross-section, so a missing or
    thin feature set produces no trade rather than a degraded one.
    """
    if len(bars) < config.min_history_bars:
        return None, "insufficient_valid_history"
    if not features:
        return None, "no_cross_section"
    coherence = float(features.get("usd_coherence") or 0.0)
    residual = float(features.get("residual") or 0.0)
    if coherence < config.xs_coherence_floor:
        # A residual is only meaningful against a BROAD dollar move; without
        # breadth there is no factor to be residual to.
        return None, "cross_section_incoherent"
    last = bars[-1]
    atr = atr_bps(bars, periods=config.atr_bars)
    if atr < config.atr_floor_bps:
        return None, "no_volatility_estimate"
    if abs(residual) < config.xs_residual_entry_bps:
        return None, "residual_too_small"
    side = "SELL" if residual > 0 else "BUY"
    return build_intent(
        last=last,
        side=side,
        stop_bps=config.sl_atr_mult * atr,
        tp_bps=config.tp_atr_mult * atr,
        spread_bps=spread_bps,
        config=config,
        atr=atr,
        signal_strength=residual / atr if atr > 0 else 0.0,
    )


def evaluate_signal(
    *, bars: list[M1Bar], config: ScalpConfig, spread_bps: float
) -> tuple[ScalpIntent | None, str]:
    """Dispatch to the configured family. Unknown family = refuse, never guess."""
    if config.signal_family == "opening_range":
        return evaluate_opening_range(bars=bars, config=config, spread_bps=spread_bps)
    if config.signal_family == "dislocation":
        return evaluate_dislocation_family(
            bars=bars, config=config, spread_bps=spread_bps
        )
    if config.signal_family == "xs_residual":
        # The single-symbol live dispatcher cannot invent a cross-section.
        # Offline panel replay calls evaluate_xs_residual directly; live stays
        # fail-closed until it has an aligned, parity-tested panel producer.
        return None, "cross_section_required"
    return None, f"unknown_signal_family:{config.signal_family}"
