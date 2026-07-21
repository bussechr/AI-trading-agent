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
from dataclasses import asdict
import math
from typing import Any

from fxstack.strategy.allocator_types import SleeveHealthSnapshot


SLEEVE_HEALTHY = "healthy"
SLEEVE_WATCH = "watch"
SLEEVE_DEGRADED = "degraded"


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _profit_factor(gross_profit: float, gross_loss_abs: float) -> float:
    if gross_loss_abs <= 0.0:
        return gross_profit if gross_profit > 0.0 else 1.0
    return float(gross_profit / gross_loss_abs)


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
        self._trade_events: dict[str, deque[dict[str, Any]]] = {
            sleeve: deque(maxlen=max_trades) for sleeve in self._sleeves
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
