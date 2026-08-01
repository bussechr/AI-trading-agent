"""Scalper configuration.

Own env prefix (``FXSCALP_``) so the runner's FXSTACK_* name validator never
sees these, and the scalper process can be configured without touching the
264-field legacy Settings class. Every knob has a conservative default; the
tier tables ship with the MEASURED 2026-07-31 spreads and are meant to be
recomputed weekly from the ledger (spread-qualified universe with demotion,
per the judged design).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return int(default)


def _s(name: str, default: str) -> str:
    return str(os.environ.get(name, default) or default)


#: Per-symbol spread budgets in bps of mid, from measured IG demo spreads.
#: A symbol whose live spread exceeds its budget is vetoed by the sentinel --
#: this IS the spread-qualified universe. Crypto budgets are set at their
#: measured typical spread so the machinery exercises on 24/7 weekend ticks;
#: crypto is shadow-only regardless (contract sizes unknown to FX sizing, and
#: measured cost-dead for M5 scalping on IG -- the ledger keeps re-measuring).
DEFAULT_SPREAD_BUDGETS_BPS: dict[str, float] = {
    # Tier A (scalp-primary; budgets from measured IG-demo spreads)
    "EURUSD": 1.2,
    "USDJPY": 1.3,
    "AUDUSD": 1.5,
    # Tier B (session-conditional; measured IG-demo)
    "GBPUSD": 2.0,
    "USDCAD": 2.2,
    "USDCHF": 2.2,
    "EURGBP": 2.0,
    "EURJPY": 2.2,
    "NZDUSD": 2.2,
    # Crosses (PROVISIONAL: Dukascopy interbank p75 measured 2026-08-01 plus
    # a 1.0bps venue-markup allowance; the sentinel re-measures on live IG
    # ticks and the weekly recompute demotes anything that exceeds budget).
    # The per-entry p* gate at the LIVE spread stays the binding cost veto.
    "AUDJPY": 2.0,
    "CADJPY": 2.4,
    "CHFJPY": 2.5,
    "EURAUD": 2.6,
    "EURCAD": 2.5,
    "EURCHF": 2.3,
    "GBPCAD": 2.9,
    "GBPCHF": 3.0,
    "GBPJPY": 2.2,
    # Crypto (shadow measurement only; measured 2026-07-31 on IG demo:
    # BTC 5.2, ETH 5.4, LTC 66.8, XRP 202 -- the p* gate keeps the wide ones
    # honest while the ledger keeps re-measuring them).
    "BTCUSD": 7.0,
    "ETHUSD": 7.0,
    "LTCUSD": 70.0,
    "XRPUSD": 210.0,
}

#: Tier B pairs may only ENTER inside their liquid sessions (UTC hours,
#: half-open ranges). Tier A and crypto trade whenever the sentinel passes.
DEFAULT_SESSION_WINDOWS_UTC: dict[str, list[tuple[int, int]]] = {
    "GBPUSD": [(7, 16)],
    "USDCAD": [(12, 21)],
    "USDCHF": [(7, 16)],
    "EURGBP": [(7, 16)],
    "EURJPY": [(0, 9), (7, 16)],
    "NZDUSD": [(21, 24), (0, 6)],
    # Crosses: liquid-session entry windows (Tokyo for JPY legs, London core
    # for European legs, NY hours for CAD legs).
    "AUDJPY": [(0, 9), (7, 16)],
    "CADJPY": [(0, 9), (12, 21)],
    "CHFJPY": [(0, 9), (7, 16)],
    "GBPJPY": [(0, 9), (7, 16)],
    "EURAUD": [(0, 9), (7, 16)],
    "EURCAD": [(7, 21)],
    "EURCHF": [(7, 16)],
    "GBPCAD": [(7, 21)],
    "GBPCHF": [(7, 16)],
}

#: IG rollover/thin-liquidity hard-off window for NON-crypto symbols, UTC.
ROLLOVER_OFF_UTC: tuple[tuple[int, int], tuple[int, int]] = ((20, 45), (22, 15))


@dataclass(slots=True)
class ScalpConfig:
    bridge_url: str = field(default_factory=lambda: _s("FXSCALP_BRIDGE_URL", "http://127.0.0.1:58710"))
    api_key_file: str = field(
        default_factory=lambda: _s("FXSCALP_API_KEY_FILE", "logs/bridge_api_key.txt")
    )
    symbols: list[str] = field(
        default_factory=lambda: [
            s.strip().upper()
            for s in _s(
                "FXSCALP_SYMBOLS",
                # Every pair the broker publishes: 18 FX + 4 crypto. The
                # spread-qualified universe is enforced per-entry (budget +
                # p* at live spread), not by shrinking the watchlist.
                "EURUSD,USDJPY,AUDUSD,GBPUSD,USDCAD,USDCHF,EURGBP,EURJPY,"
                "NZDUSD,AUDJPY,CADJPY,CHFJPY,EURAUD,EURCAD,EURCHF,GBPCAD,"
                "GBPCHF,GBPJPY,BTCUSD,ETHUSD,LTCUSD,XRPUSD",
            ).split(",")
            if s.strip()
        ]
    )
    mode: str = field(default_factory=lambda: _s("FXSCALP_MODE", "shadow"))
    data_root: str = field(default_factory=lambda: _s("FXSCALP_DATA_ROOT", "data/scalp"))

    # Cadence
    poll_secs: float = field(default_factory=lambda: _f("FXSCALP_POLL_SECS", 1.0))
    # A bar with fewer ticks than this is invalid (no honest OHLC).
    min_ticks_per_bar: int = field(default_factory=lambda: _i("FXSCALP_MIN_TICKS_PER_BAR", 3))
    # Signals require this many consecutive VALID bars of history.
    min_history_bars: int = field(default_factory=lambda: _i("FXSCALP_MIN_HISTORY_BARS", 30))
    tick_stale_secs: float = field(default_factory=lambda: _f("FXSCALP_TICK_STALE_SECS", 10.0))

    # Signal geometry (dislocation family; ATR-scaled bracket).
    # signal_mode "revert" fades the dislocation (default); "momentum" joins
    # it on a confirming bar -- same measurement, opposite hypothesis. Both
    # face the identical p* viability arithmetic.
    signal_mode: str = field(default_factory=lambda: _s("FXSCALP_SIGNAL_MODE", "revert"))
    z_entry: float = field(default_factory=lambda: _f("FXSCALP_Z_ENTRY", 2.0))
    ema_bars: int = field(default_factory=lambda: _i("FXSCALP_EMA_BARS", 20))
    atr_bars: int = field(default_factory=lambda: _i("FXSCALP_ATR_BARS", 14))
    tp_atr_mult: float = field(default_factory=lambda: _f("FXSCALP_TP_ATR_MULT", 1.5))
    sl_atr_mult: float = field(default_factory=lambda: _f("FXSCALP_SL_ATR_MULT", 1.0))
    # Broker min-stop floor expressed in bps of mid (~5 pips on EURUSD).
    min_stop_bps: float = field(default_factory=lambda: _f("FXSCALP_MIN_STOP_BPS", 4.5))
    # ATR floor: history quieter than this is an unmeasurable/frozen market,
    # not an opportunity (near-zero ATR makes z explode on the first real move).
    atr_floor_bps: float = field(default_factory=lambda: _f("FXSCALP_ATR_FLOOR_BPS", 0.3))
    time_stop_bars: int = field(default_factory=lambda: _i("FXSCALP_TIME_STOP_BARS", 20))
    cooldown_bars: int = field(default_factory=lambda: _i("FXSCALP_COOLDOWN_BARS", 3))
    # Bracket viability gate: reject geometry whose breakeven win rate
    # p* = (SL+cost)/(TP+SL) exceeds this. The panel's arithmetic, applied
    # per-entry with the LIVE spread instead of an assumed one.
    p_star_max: float = field(default_factory=lambda: _f("FXSCALP_P_STAR_MAX", 0.55))

    # Sentinel
    spread_budgets_bps: dict[str, float] = field(
        default_factory=lambda: dict(DEFAULT_SPREAD_BUDGETS_BPS)
    )
    spread_window: int = field(default_factory=lambda: _i("FXSCALP_SPREAD_WINDOW", 300))
    spread_z_limit: float = field(default_factory=lambda: _f("FXSCALP_SPREAD_Z_LIMIT", 3.0))

    # Risk (shadow bookkeeping in R; FX lots via the existing fail-closed sizer)
    risk_fraction: float = field(default_factory=lambda: _f("FXSCALP_RISK_FRACTION", 0.01))
    # Ceiling on equity share committable as margin across the scalp book;
    # sizing clips lots so the broker can never bounce an approved order.
    margin_utilization_cap: float = field(
        default_factory=lambda: _f("FXSCALP_MARGIN_UTILIZATION_CAP", 0.25)
    )
    max_concurrent: int = field(default_factory=lambda: _i("FXSCALP_MAX_CONCURRENT", 4))
    daily_loss_stop_r: float = field(default_factory=lambda: _f("FXSCALP_DAILY_LOSS_STOP_R", -3.0))

    def api_key(self) -> str:
        path = Path(self.api_key_file)
        if not path.is_absolute():
            path = Path.cwd() / path
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.mode not in ("shadow", "live"):
            errors.append(f"mode {self.mode!r} must be shadow|live")
        # mode == "live" additionally requires a valid arming certificate at
        # startup (checked by ScalpLoop) and per-order server-side approval
        # via /v2/scalp/commands -- config alone can never arm live trading.
        if not self.symbols:
            errors.append("FXSCALP_SYMBOLS is empty")
        for sym in self.symbols:
            if sym not in self.spread_budgets_bps:
                errors.append(
                    f"symbol {sym} has no spread budget -- unqualified symbols do not trade"
                )
        if not 0.0 < self.risk_fraction <= 0.02:
            errors.append(
                f"risk_fraction {self.risk_fraction} outside (0, 0.02]; the demo "
                "aggression tier beyond 2% is not wired until live mode exists"
            )
        if self.tp_atr_mult <= 0 or self.sl_atr_mult <= 0:
            errors.append("bracket multiples must be positive")
        if self.daily_loss_stop_r >= 0:
            errors.append("daily_loss_stop_r must be negative (it is a loss limit)")
        if not 0.0 < self.p_star_max < 1.0:
            errors.append(f"p_star_max {self.p_star_max} must be in (0, 1)")
        if self.z_entry <= 0:
            errors.append(f"z_entry {self.z_entry} must be > 0")
        if self.signal_mode not in ("revert", "momentum"):
            errors.append(f"signal_mode {self.signal_mode!r} must be revert|momentum")
        if self.min_stop_bps < 0 or self.atr_floor_bps < 0:
            errors.append("min_stop_bps and atr_floor_bps must be >= 0")
        if self.time_stop_bars < 1:
            errors.append(f"time_stop_bars {self.time_stop_bars} must be >= 1")
        if self.cooldown_bars < 0:
            errors.append(f"cooldown_bars {self.cooldown_bars} must be >= 0")
        if self.poll_secs <= 0 or self.tick_stale_secs <= 0:
            errors.append("poll_secs and tick_stale_secs must be > 0")
        if self.min_ticks_per_bar < 1 or self.min_history_bars < 5:
            errors.append("min_ticks_per_bar >= 1 and min_history_bars >= 5 required")
        if not 0.0 < self.margin_utilization_cap <= 1.0:
            errors.append(
                f"margin_utilization_cap {self.margin_utilization_cap} must be in (0, 1]"
            )
        return errors
