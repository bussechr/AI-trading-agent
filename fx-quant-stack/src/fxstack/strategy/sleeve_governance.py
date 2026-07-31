# AGENT: ROLE: Rolling realized-outcome sleeve-health tracker for allocator scoring, penalties, and sleeve-level summaries.
# AGENT: ENTRYPOINT: imported by runtime adaptive portfolio paths and isolated research.
# AGENT: PRIMARY INPUTS: closed-trade events and sleeve IDs.
# AGENT: PRIMARY OUTPUTS: `SleeveHealthSnapshot` maps and governance penalties.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator_types.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py` and isolated research tooling.
# AGENT: STATE / SIDE EFFECTS: caller-owned in-memory tracker only.
# AGENT: HANDSHAKES: allocator score penalty and sleeve summary artifact contract.
# AGENT: SEE: `docs/agents/causal-research-and-runtime-validation.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from collections import Counter, defaultdict, deque
from collections.abc import Mapping
from dataclasses import asdict
import math
from typing import Any

from fxstack.strategy.allocator_types import SleeveHealthSnapshot


SLEEVE_HEALTHY = "healthy"
SLEEVE_WATCH = "watch"
SLEEVE_DEGRADED = "degraded"
SLEEVE_GOVERNANCE_STATE_VERSION = 1

_TRADE_EVENT_FIELDS = (
    "realized_pnl_usd",
    "holding_bars",
    "partial_exit_events",
    "close_reason",
    "session_bucket",
    "pair",
)


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _profit_factor(gross_profit: float, gross_loss_abs: float) -> float:
    if gross_loss_abs <= 0.0:
        return gross_profit if gross_profit > 0.0 else 1.0
    return float(gross_profit / gross_loss_abs)


def _coerce_finite_float(value: Any, *, minimum: float | None = None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(normalized) or (minimum is not None and normalized < minimum):
        return None
    return normalized


def _coerce_nonnegative_int(value: Any) -> int | None:
    normalized = _coerce_finite_float(value, minimum=0.0)
    if normalized is None or not normalized.is_integer():
        return None
    return int(normalized)


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float) and math.isfinite(value):
        return str(value)
    return None


def _normalize_trade_event(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or any(field not in value for field in _TRADE_EVENT_FIELDS):
        return None

    realized_pnl_usd = _coerce_finite_float(value.get("realized_pnl_usd"))
    holding_bars = _coerce_finite_float(value.get("holding_bars"), minimum=0.0)
    partial_exit_events = _coerce_nonnegative_int(value.get("partial_exit_events"))
    close_reason = _coerce_text(value.get("close_reason"))
    session_bucket = _coerce_text(value.get("session_bucket"))
    pair = _coerce_text(value.get("pair"))
    if (
        realized_pnl_usd is None
        or holding_bars is None
        or partial_exit_events is None
        or close_reason is None
        or session_bucket is None
        or pair is None
    ):
        return None
    return {
        "realized_pnl_usd": realized_pnl_usd,
        "holding_bars": holding_bars,
        "partial_exit_events": partial_exit_events,
        "close_reason": close_reason,
        "session_bucket": session_bucket,
        "pair": pair,
    }


#: Closed trades a sleeve needs before realized expectancy is allowed to move
#: its allocation at all. Below this, expectancy is noise and letting it size
#: the book is worse than sizing flat.
MIN_EXPECTANCY_TRADES = 8

#: Expectancy (USD per closed trade) at which a sleeve earns its full base
#: allocation. Deliberately modest: this is "demonstrably not losing", not
#: "demonstrably excellent".
EXPECTANCY_FULL_ALLOCATION_USD = 10.0

#: Floor on the allocation multiplier. A demonstrably losing sleeve is starved,
#: not switched off -- zero would remove the only source of new evidence about
#: whether it has recovered, which is how a sleeve gets permanently stuck.
MIN_EXPECTANCY_ALLOCATION_SCALE = 0.25


def sleeve_expectancy_allocation_scale(
    snapshot: SleeveHealthSnapshot | None,
    *,
    min_trades: int = MIN_EXPECTANCY_TRADES,
) -> tuple[float, str]:
    """Scale a sleeve's allocation by what it has actually earned per trade.

    Returns ``(scale, reason)`` where ``scale`` multiplies the sleeve's risk
    budget. This is the piece that lets REALIZED outcomes govern capital rather
    than the model's opinion of its own setups: a sleeve that has been paid
    keeps its size, one that has been paying keeps shrinking.

    Ramps linearly on expectancy from the floor at <= 0 USD/trade to full
    allocation at :data:`EXPECTANCY_FULL_ALLOCATION_USD`. Linear on purpose --
    a curve would imply a confidence about the expectancy-to-optimal-size
    mapping that nothing here has earned. Profit factor is deliberately NOT
    mixed in: it is already inside ``score``/``state``, and double-counting one
    signal as two is how a single bad streak turns into a compounding penalty.
    """

    if snapshot is None:
        return 1.0, "no_snapshot"
    try:
        trades = int(getattr(snapshot, "trades", 0))
        expectancy = float(getattr(snapshot, "expectancy_usd", 0.0))
    except (TypeError, ValueError, OverflowError):
        return 1.0, "unreadable_snapshot"
    if not math.isfinite(expectancy):
        return 1.0, "non_finite_expectancy"
    if trades < int(min_trades):
        return 1.0, f"insufficient_trades:{trades}<{int(min_trades)}"
    if expectancy >= EXPECTANCY_FULL_ALLOCATION_USD:
        return 1.0, "expectancy_full"
    if expectancy <= 0.0:
        return MIN_EXPECTANCY_ALLOCATION_SCALE, "expectancy_non_positive"
    span = 1.0 - MIN_EXPECTANCY_ALLOCATION_SCALE
    scale = MIN_EXPECTANCY_ALLOCATION_SCALE + (
        span * (expectancy / EXPECTANCY_FULL_ALLOCATION_USD)
    )
    return float(_clip01(scale)), "expectancy_ramp"


def sleeve_health_penalty(snapshot: SleeveHealthSnapshot) -> float:
    if str(snapshot.state) == SLEEVE_DEGRADED:
        return 0.12
    if str(snapshot.state) == SLEEVE_WATCH:
        return 0.05
    return 0.0


def sleeve_entry_block_reason(
    *,
    snapshot: SleeveHealthSnapshot | None,
    expected_sleeve: str,
) -> str:
    """Return a hard entry block for unusable or degraded sleeve governance."""

    sleeve = str(expected_sleeve or "").strip()
    if not sleeve or snapshot is None:
        return "sleeve_governance_unavailable"
    if str(getattr(snapshot, "sleeve", "") or "").strip() != sleeve:
        return "sleeve_governance_mismatch"
    try:
        score = float(getattr(snapshot, "score"))
    except (TypeError, ValueError, OverflowError):
        return "sleeve_governance_invalid"
    state = str(getattr(snapshot, "state", "") or "").strip().lower()
    if not math.isfinite(score) or not 0.0 <= score <= 1.0 or state not in {
        SLEEVE_HEALTHY,
        SLEEVE_WATCH,
        SLEEVE_DEGRADED,
    }:
        return "sleeve_governance_invalid"
    if state == SLEEVE_DEGRADED:
        return "sleeve_governance_degraded"
    return ""


class SleeveGovernanceTracker:
    # AGENT STATE: The tracker keeps only bounded realized-trade outcomes; comparison telemetry has no governance input.
    def __init__(self, *, sleeves: list[str], max_trades: int = 64) -> None:
        self._sleeves = [str(item) for item in sleeves]
        maxlen_probe: deque[dict[str, Any]] = deque(maxlen=max_trades)
        self._max_trades = int(maxlen_probe.maxlen or 0)
        self._trade_events: dict[str, deque[dict[str, Any]]] = {
            sleeve: deque(maxlen=self._max_trades) for sleeve in self._sleeves
        }

    def record_trade(
        self,
        *,
        sleeve: str,
        realized_pnl_usd: float,
        holding_bars: float,
        partial_exit_events: int,
        close_reason: str,
        session_bucket: str,
        pair: str,
    ) -> None:
        sleeve_key = str(sleeve or "")
        if sleeve_key not in self._trade_events:
            return
        self._trade_events[sleeve_key].append(
            {
                "realized_pnl_usd": float(realized_pnl_usd),
                "holding_bars": float(holding_bars),
                "partial_exit_events": int(partial_exit_events),
                "close_reason": str(close_reason or ""),
                "session_bucket": str(session_bucket or ""),
                "pair": str(pair or ""),
            }
        )

    def export_state(self) -> dict[str, Any]:
        """Return a deterministic, bounded, strictly JSON-safe event snapshot."""

        trade_events: dict[str, list[dict[str, Any]]] = {}
        for sleeve in self._sleeves:
            normalized_events: list[dict[str, Any]] = []
            for event in self._trade_events.get(sleeve, ()):
                normalized = _normalize_trade_event(event)
                if normalized is not None:
                    normalized_events.append(normalized)
            if self._max_trades <= 0:
                normalized_events = []
            else:
                normalized_events = normalized_events[-self._max_trades :]
            trade_events[sleeve] = normalized_events
        return {
            "schema_version": SLEEVE_GOVERNANCE_STATE_VERSION,
            "max_trades": self._max_trades,
            "trade_events": trade_events,
        }

    def restore_state(self, payload: Any) -> None:
        """Restore valid configured-sleeve events without trusting persisted limits."""

        if not isinstance(payload, Mapping):
            return
        schema_version = _coerce_nonnegative_int(payload.get("schema_version"))
        if schema_version != SLEEVE_GOVERNANCE_STATE_VERSION:
            return
        persisted_events = payload.get("trade_events")
        if not isinstance(persisted_events, Mapping):
            return

        for sleeve in self._sleeves:
            raw_events = persisted_events.get(sleeve)
            if not isinstance(raw_events, list):
                continue
            target = self._trade_events[sleeve]
            if not raw_events or self._max_trades <= 0:
                target.clear()
                continue

            normalized_events: list[dict[str, Any]] = []
            for event in raw_events[-self._max_trades :]:
                normalized = _normalize_trade_event(event)
                if normalized is not None:
                    normalized_events.append(normalized)
            # Treat an all-malformed non-empty list as unusable input, not an
            # instruction to erase a valid in-memory history.
            if not normalized_events:
                continue
            target.clear()
            target.extend(normalized_events)

    def snapshot(self) -> dict[str, SleeveHealthSnapshot]:
        out: dict[str, SleeveHealthSnapshot] = {}
        for sleeve in self._sleeves:
            trades = list(self._trade_events.get(sleeve, ()))
            trades_count = len(trades)
            pnl_values = [float(item.get("realized_pnl_usd", 0.0)) for item in trades]
            win_rate = float(sum(1 for pnl in pnl_values if pnl > 0.0) / trades_count) if trades_count else 0.0
            expectancy = float(sum(pnl_values) / trades_count) if trades_count else 0.0
            gross_profit = float(sum(max(0.0, pnl) for pnl in pnl_values))
            gross_loss_abs = float(sum(abs(min(0.0, pnl)) for pnl in pnl_values))
            profit_factor = _profit_factor(gross_profit, gross_loss_abs)
            avg_holding = float(sum(float(item.get("holding_bars", 0.0)) for item in trades) / trades_count) if trades_count else 0.0
            partial_frequency = (
                float(sum(1 for item in trades if int(item.get("partial_exit_events", 0)) > 0) / trades_count)
                if trades_count
                else 0.0
            )
            replacement_exit_share = (
                float(sum(1 for item in trades if str(item.get("close_reason") or "") == "adaptive_replacement_exit") / trades_count)
                if trades_count
                else 0.0
            )
            drawdown_contribution = gross_loss_abs
            session_pnl_mix: dict[str, float] = dict(
                sorted(
                    Counter({}).items()
                )
            )
            if trades:
                session_acc = defaultdict(float)
                pair_acc = defaultdict(float)
                for item in trades:
                    session_acc[str(item.get("session_bucket") or "")] += float(item.get("realized_pnl_usd", 0.0))
                    pair_acc[str(item.get("pair") or "")] += float(item.get("realized_pnl_usd", 0.0))
                session_pnl_mix = {k: float(v) for k, v in sorted(session_acc.items())}
                pair_contribution = {k: float(v) for k, v in sorted(pair_acc.items())}
            else:
                pair_contribution = {}

            score = _clip01(
                0.50
                + (0.12 * win_rate)
                + (0.12 * _clip01((profit_factor - 0.75) / 1.25))
                + (0.10 * _clip01((expectancy + 30.0) / 60.0))
                - (0.08 * partial_frequency)
                - (0.10 * replacement_exit_share)
                - (0.10 * _clip01(drawdown_contribution / 250.0))
            )
            state = SLEEVE_HEALTHY
            if trades_count >= 5:
                if expectancy < -10.0 or profit_factor < 0.85:
                    state = SLEEVE_DEGRADED
                elif expectancy < 0.0 or profit_factor < 0.95:
                    state = SLEEVE_WATCH

            out[sleeve] = SleeveHealthSnapshot(
                sleeve=sleeve,
                score=float(score),
                state=str(state),
                trades=int(trades_count),
                win_rate=float(win_rate),
                expectancy_usd=float(expectancy),
                profit_factor=float(profit_factor),
                avg_holding_bars=float(avg_holding),
                partial_frequency=float(partial_frequency),
                replacement_exit_share=float(replacement_exit_share),
                drawdown_contribution_usd=float(drawdown_contribution),
                session_pnl_mix=dict(session_pnl_mix),
                pair_contribution=dict(pair_contribution),
            )
        return out


def serialize_sleeve_snapshots(snapshots: dict[str, SleeveHealthSnapshot]) -> dict[str, Any]:
    return {str(sleeve): asdict(snapshot) for sleeve, snapshot in sorted(snapshots.items())}
