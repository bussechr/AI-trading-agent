# AGENT: ROLE: Rolling sleeve-health tracker for allocator scoring, penalties, and sleeve-level summaries.
# AGENT: ENTRYPOINT: imported by twin replay and runtime adaptive portfolio paths.
# AGENT: PRIMARY INPUTS: closed-trade events, shadow/live divergence events, sleeve IDs.
# AGENT: PRIMARY OUTPUTS: `SleeveHealthSnapshot` maps and governance penalties.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator_types.py`.
# AGENT: CALLED BY: `tools/fxstack_digital_twin_backtest.py`, `fxstack/runtime/runner.py`.
# AGENT: STATE / SIDE EFFECTS: caller-owned in-memory tracker only.
# AGENT: HANDSHAKES: allocator score penalty and sleeve summary artifact contract.
# AGENT: SEE: `docs/agents/twin-vs-prod-parity.md` -> `fxstack/strategy/allocator.py` -> `docs/agents/runtime-loop.md`
from __future__ import annotations

from collections import Counter, defaultdict, deque
from dataclasses import asdict
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


class SleeveGovernanceTracker:
    # AGENT STATE: The tracker keeps only a bounded rolling window so twin/runtime can share governance logic without persistence.
    def __init__(self, *, sleeves: list[str], max_trades: int = 64, max_divergences: int = 256) -> None:
        self._sleeves = [str(item) for item in sleeves]
        self._trade_events: dict[str, deque[dict[str, Any]]] = {
            sleeve: deque(maxlen=max_trades) for sleeve in self._sleeves
        }
        self._divergence_events: dict[str, deque[int]] = {
            sleeve: deque(maxlen=max_divergences) for sleeve in self._sleeves
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

    def record_divergence(self, *, sleeve: str, divergence: str) -> None:
        sleeve_key = str(sleeve or "")
        if sleeve_key not in self._divergence_events:
            return
        is_divergent = 1 if str(divergence or "") in {"live_only", "shadow_only", "adaptive_only"} else 0
        self._divergence_events[sleeve_key].append(is_divergent)

    def snapshot(self) -> dict[str, SleeveHealthSnapshot]:
        out: dict[str, SleeveHealthSnapshot] = {}
        for sleeve in self._sleeves:
            trades = list(self._trade_events.get(sleeve, ()))
            divergences = list(self._divergence_events.get(sleeve, ()))
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
            divergence_rate = float(sum(divergences) / len(divergences)) if divergences else 0.0
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
                - (0.12 * divergence_rate)
                - (0.10 * _clip01(drawdown_contribution / 250.0))
            )
            state = SLEEVE_HEALTHY
            if trades_count >= 5:
                if expectancy < -10.0 or profit_factor < 0.85 or divergence_rate >= 0.40:
                    state = SLEEVE_DEGRADED
                elif expectancy < 0.0 or profit_factor < 0.95 or divergence_rate >= 0.25:
                    state = SLEEVE_WATCH
            elif divergence_rate >= 0.50:
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
                live_shadow_divergence_rate=float(divergence_rate),
                session_pnl_mix=dict(session_pnl_mix),
                pair_contribution=dict(pair_contribution),
            )
        return out


def serialize_sleeve_snapshots(snapshots: dict[str, SleeveHealthSnapshot]) -> dict[str, Any]:
    return {str(sleeve): asdict(snapshot) for sleeve, snapshot in sorted(snapshots.items())}

