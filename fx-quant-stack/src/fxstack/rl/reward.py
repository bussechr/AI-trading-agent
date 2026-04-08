from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class RewardBreakdown:
    pnl_reward: float = 0.0
    cost_penalty: float = 0.0
    risk_penalty: float = 0.0
    action_penalty: float = 0.0
    hold_bonus: float = 0.0
    terminal_bonus: float = 0.0
    reward_scale: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return float(self.reward_scale) * (
            float(self.pnl_reward)
            - float(self.cost_penalty)
            - float(self.risk_penalty)
            - float(self.action_penalty)
            + float(self.hold_bonus)
            + float(self.terminal_bonus)
        )

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["total"] = float(self.total)
        return payload


def compute_reward_breakdown(
    *,
    realized_pnl_usd: float,
    unrealized_pnl_usd: float,
    cost_bps: float,
    drawdown_pct: float,
    target_position: float,
    filled_position: float,
    terminated: bool = False,
    truncated: bool = False,
    reward_scale: float = 1.0,
    metadata: dict[str, Any] | None = None,
) -> RewardBreakdown:
    pnl_reward = float(realized_pnl_usd) + (0.10 * float(unrealized_pnl_usd))
    cost_penalty = max(0.0, float(cost_bps) / 100.0)
    risk_penalty = max(0.0, float(drawdown_pct)) * 4.0
    action_penalty = abs(float(target_position) - float(filled_position)) * 0.25
    hold_bonus = 0.02 if abs(float(target_position)) <= 1e-9 and abs(float(filled_position)) <= 1e-9 else 0.0
    terminal_bonus = 0.15 if bool(terminated) else (-0.05 if bool(truncated) else 0.0)
    return RewardBreakdown(
        pnl_reward=float(pnl_reward),
        cost_penalty=float(cost_penalty),
        risk_penalty=float(risk_penalty),
        action_penalty=float(action_penalty),
        hold_bonus=float(hold_bonus),
        terminal_bonus=float(terminal_bonus),
        reward_scale=float(reward_scale),
        metadata=dict(metadata or {}),
    )


def compute_step_reward(**kwargs: Any) -> tuple[float, RewardBreakdown]:
    breakdown = compute_reward_breakdown(**kwargs)
    return float(breakdown.total), breakdown

