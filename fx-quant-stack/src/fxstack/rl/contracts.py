from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd

from fxstack.backtest.harness.contracts import EconomicReport
from fxstack.risk.contracts import ApprovedOrderIntent, MarketState, PortfolioState, PolicyIntent, RiskDecision


@dataclass(slots=True)
class RLTradeAction:
    target_position: float
    close_position: bool = False
    tighten_stop: bool = False
    stop_loss: float = 0.0
    take_profit: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RLTradeAction":
        return cls(
            target_position=float(payload.get("target_position", 0.0) or 0.0),
            close_position=bool(payload.get("close_position", False)),
            tighten_stop=bool(payload.get("tighten_stop", False)),
            stop_loss=float(payload.get("stop_loss", 0.0) or 0.0),
            take_profit=float(payload.get("take_profit", 0.0) or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class RLObservation:
    ts: str
    pair: str
    market: MarketState
    portfolio: PortfolioState
    policy: PolicyIntent
    features: dict[str, float] = field(default_factory=dict)
    campaign: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["market"] = self.market.to_dict()
        payload["portfolio"] = self.portfolio.to_dict()
        payload["policy"] = self.policy.to_dict()
        return payload


@dataclass(slots=True)
class RLEpisodeEvent:
    step: int
    ts: str
    pair: str
    observation: dict[str, Any]
    action: dict[str, Any]
    reward: float
    terminated: bool
    truncated: bool
    next_observation: dict[str, Any] | None = None
    info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RLEpisodeRow:
    step: int
    ts: str
    pair: str
    target_position: float
    filled_position: float
    reward: float
    pnl_reward: float
    cost_penalty: float
    risk_penalty: float
    action_penalty: float
    terminated: bool
    truncated: bool
    market: dict[str, Any] = field(default_factory=dict)
    portfolio: dict[str, Any] = field(default_factory=dict)
    policy: dict[str, Any] = field(default_factory=dict)
    event_type: str = ""
    order_command: str = ""
    fill_price: float = 0.0
    fill_lots: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    drawdown_pct: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class RLRunConfig:
    pair: str
    timeframe: str
    max_steps: int = 0
    initial_equity: float = 0.0
    max_position_abs: float = 1.0
    action_deadband: float = 0.05
    reward_scale: float = 1.0
    transaction_cost_bps: float = 1.5
    slippage_bps: float = 0.5
    max_drawdown_pct: float = 0.25
    stale_after_secs: float = 3600.0
    max_freshness_secs: float = 3600.0
    terminate_on_drawdown: bool = True
    terminate_on_stale: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_episode_rows(rows: list[RLEpisodeRow] | list[dict[str, Any]]) -> pd.DataFrame:
    payloads = [row.to_dict() if hasattr(row, "to_dict") else dict(row) for row in list(rows or [])]
    return pd.DataFrame(payloads)


def build_episode_from_rows(
    rows: list[RLEpisodeRow] | list[dict[str, Any]],
    *,
    report: EconomicReport | None = None,
) -> dict[str, Any]:
    df = normalize_episode_rows(rows)
    if df.empty:
        summary = {
            "steps": 0,
            "reward_sum": 0.0,
            "reward_mean": 0.0,
            "terminated_steps": 0,
            "truncated_steps": 0,
        }
    else:
        summary = {
            "steps": int(len(df)),
            "reward_sum": float(df["reward"].sum()) if "reward" in df.columns else 0.0,
            "reward_mean": float(df["reward"].mean()) if "reward" in df.columns else 0.0,
            "terminated_steps": int(df["terminated"].sum()) if "terminated" in df.columns else 0,
            "truncated_steps": int(df["truncated"].sum()) if "truncated" in df.columns else 0,
        }
    return {
        "status": "ok",
        "summary": summary,
        "report": None if report is None else report.to_dict(),
        "rows": df.to_dict(orient="records"),
    }

