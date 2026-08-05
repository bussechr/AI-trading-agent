"""The scalper's veto layer: spread sentinel and session router.

Both are pure functions over observable state, both return a REASON string
("" means pass), and both are absolute -- no signal opinion can override them.
This mirrors the conjunctive-entry doctrine of the old stack with none of its
ceremony.
"""

from __future__ import annotations

import datetime as dt
import statistics
from collections import deque

from fxstack.providers.ig_mt4_catalog import IG_MT4_CRYPTO_CFD_SYMBOLS
from fxstack.scalp.config import ROLLOVER_OFF_UTC, ScalpConfig

CRYPTO_SYMBOLS = frozenset(IG_MT4_CRYPTO_CFD_SYMBOLS)


class SpreadSentinel:
    """Rolling per-symbol spread stats; vetoes over-budget or spiking spreads."""

    def __init__(self, config: ScalpConfig) -> None:
        self.config = config
        self._window: dict[str, deque[float]] = {
            s: deque(maxlen=int(config.spread_window)) for s in config.symbols
        }
        self._last_tick_epoch: dict[str, float] = {}

    def observe(self, *, symbol: str, spread_bps: float, ts_epoch: float) -> None:
        sym = str(symbol).upper()
        if sym in self._window and spread_bps >= 0.0:
            self._window[sym].append(float(spread_bps))
            self._last_tick_epoch[sym] = max(
                self._last_tick_epoch.get(sym, 0.0), float(ts_epoch)
            )

    def current_spread_bps(self, symbol: str) -> float:
        window = self._window.get(str(symbol).upper())
        return float(window[-1]) if window else 0.0

    def veto_reason(self, *, symbol: str, now_epoch: float) -> str:
        sym = str(symbol).upper()
        window = self._window.get(sym)
        if not window:
            return "no_spread_observations"
        last_tick = self._last_tick_epoch.get(sym, 0.0)
        if now_epoch - last_tick > self.config.tick_stale_secs:
            return "tick_stale"
        budget = self.config.budget_for(sym)
        if budget <= 0.0:
            return "symbol_unqualified"
        current = float(window[-1])
        if current <= 0.0:
            # A zero/absent spread is an unmeasured cost, not a free market.
            return "spread_unresolved"
        if current > budget:
            return "spread_over_budget"
        if len(window) >= 30:
            mean = statistics.fmean(window)
            stdev = statistics.pstdev(window)
            if stdev > 1e-9 and (current - mean) / stdev > self.config.spread_z_limit:
                return "spread_spike"
        return ""


def session_veto_reason(
    *, symbol: str, now_epoch: float, config: ScalpConfig
) -> str:
    """Pair-hour gating. Crypto is 24/7; FX obeys rollover-off and, for
    session-conditional pairs, their configured liquid windows."""

    sym = str(symbol).upper()
    now = dt.datetime.fromtimestamp(float(now_epoch), dt.timezone.utc)
    if sym in CRYPTO_SYMBOLS:
        return ""
    # FX weekend: closed Friday 21:00 UTC -> Sunday 21:00 UTC.
    weekday = now.weekday()  # Mon=0 .. Sun=6
    minutes = now.hour * 60 + now.minute
    if weekday == 4 and minutes >= 21 * 60:
        return "fx_weekend_closed"
    if weekday == 5:
        return "fx_weekend_closed"
    if weekday == 6 and minutes < 21 * 60:
        return "fx_weekend_closed"
    (start_h, start_m), (end_h, end_m) = ROLLOVER_OFF_UTC
    if start_h * 60 + start_m <= minutes < end_h * 60 + end_m:
        return "rollover_window"
    from fxstack.scalp.config import DEFAULT_SESSION_WINDOWS_UTC

    windows = DEFAULT_SESSION_WINDOWS_UTC.get(sym)
    if windows:
        hour = now.hour
        if not any(start <= hour < end for start, end in windows):
            return "outside_liquid_session"
    return ""
