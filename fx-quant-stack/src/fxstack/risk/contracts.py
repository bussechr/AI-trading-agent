from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


OrderSide = Literal["BUY", "SELL"]
RiskVerdict = Literal["allow", "block", "reduce", "hold"]
LifecycleAction = Literal["entry", "hold", "partial_tp", "exit", "modify_sl", "tighten_stop"]


@dataclass(slots=True)
class PolicyIntent:
    pair: str
    side: str = ""
    intent: str = "UNKNOWN"
    action: str = ""
    action_score: float = 0.0
    strategy: str = ""
    playbook: str = ""
    thesis_id: str = ""
    campaign_state: str = ""
    conviction_band: str = ""
    thesis_stage: str = ""
    portfolio_posture: str = ""
    expected_edge_bps: float = 0.0
    confidence: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PolicyIntent":
        return cls(
            pair=str(payload.get("pair") or "").upper(),
            side=str(payload.get("side") or "").upper(),
            intent=str(payload.get("intent") or "UNKNOWN").upper(),
            action=str(payload.get("action") or ""),
            action_score=float(payload.get("action_score", 0.0) or 0.0),
            strategy=str(payload.get("strategy") or ""),
            playbook=str(payload.get("playbook") or ""),
            thesis_id=str(payload.get("thesis_id") or ""),
            campaign_state=str(payload.get("campaign_state") or ""),
            conviction_band=str(payload.get("conviction_band") or ""),
            thesis_stage=str(payload.get("thesis_stage") or ""),
            portfolio_posture=str(payload.get("portfolio_posture") or ""),
            expected_edge_bps=float(payload.get("expected_edge_bps", 0.0) or 0.0),
            confidence=float(payload.get("confidence", 0.0) or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class MarketState:
    pair: str
    ts: str = ""
    session_bucket: str = ""
    regime: str = ""
    spread_bps: float = 0.0
    allowed_spread_bps: float = 0.0
    marketable: bool = True
    market_open: bool = True
    data_fresh: bool = True
    freshness_secs: float | None = None
    freshness_limit_secs: float | None = None
    volatility: float = 0.0
    liquidity_score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MarketState":
        return cls(
            pair=str(payload.get("pair") or "").upper(),
            ts=str(payload.get("ts") or ""),
            session_bucket=str(payload.get("session_bucket") or ""),
            regime=str(payload.get("regime") or ""),
            spread_bps=float(payload.get("spread_bps", 0.0) or 0.0),
            allowed_spread_bps=float(payload.get("allowed_spread_bps", payload.get("max_spread_bps", 0.0)) or 0.0),
            marketable=bool(payload.get("marketable", True)),
            market_open=bool(payload.get("market_open", True)),
            data_fresh=bool(payload.get("data_fresh", True)),
            freshness_secs=(None if payload.get("freshness_secs") is None else float(payload.get("freshness_secs"))),
            freshness_limit_secs=(None if payload.get("freshness_limit_secs") is None else float(payload.get("freshness_limit_secs"))),
            volatility=float(payload.get("volatility", 0.0) or 0.0),
            liquidity_score=float(payload.get("liquidity_score", 0.0) or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class PortfolioState:
    equity: float = 0.0
    balance: float = 0.0
    peak_equity: float = 0.0
    drawdown_pct: float = 0.0
    open_position_count: int = 0
    pair_position_count: int = 0
    max_total_positions: int = 0
    max_pair_positions: int = 0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    capital_at_risk_pct: float = 0.0
    sleeve: str = ""
    replacement_pressure: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PortfolioState":
        return cls(
            equity=float(payload.get("equity", 0.0) or 0.0),
            balance=float(payload.get("balance", 0.0) or 0.0),
            peak_equity=float(payload.get("peak_equity", 0.0) or 0.0),
            drawdown_pct=float(payload.get("drawdown_pct", 0.0) or 0.0),
            open_position_count=int(payload.get("open_position_count", payload.get("positions", 0)) or 0),
            pair_position_count=int(payload.get("pair_position_count", 0) or 0),
            max_total_positions=int(payload.get("max_total_positions", 0) or 0),
            max_pair_positions=int(payload.get("max_pair_positions", 0) or 0),
            gross_exposure=float(payload.get("gross_exposure", 0.0) or 0.0),
            net_exposure=float(payload.get("net_exposure", 0.0) or 0.0),
            capital_at_risk_pct=float(payload.get("capital_at_risk_pct", 0.0) or 0.0),
            sleeve=str(payload.get("sleeve") or ""),
            replacement_pressure=float(payload.get("replacement_pressure", 0.0) or 0.0),
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class RiskRuleTrace:
    rule: str
    verdict: RiskVerdict
    reason: str = ""
    score: float | None = None
    changed_decision: bool = False
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RiskRuleTrace":
        return cls(
            rule=str(payload.get("rule") or ""),
            verdict=str(payload.get("verdict") or "hold"),  # type: ignore[arg-type]
            reason=str(payload.get("reason") or ""),
            score=(None if payload.get("score") is None else float(payload.get("score"))),
            changed_decision=bool(payload.get("changed_decision", False)),
            details=dict(payload.get("details") or {}),
        )


@dataclass(slots=True)
class ApprovedOrderIntent:
    command: str
    symbol: str
    lots: float
    close_lots: float = 0.0
    side: OrderSide | str = "BUY"
    intent: str = "ENTRY"
    action: str = "entry"
    action_score: float = 0.0
    tp_price: float | None = None
    sl_price: float | None = None
    risk_budget_pct: float = 0.0
    lifecycle_action: LifecycleAction = "entry"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_command_payload(self) -> dict[str, Any]:
        payload = {
            "cmd": str(self.command).upper(),
            "symbol": str(self.symbol).upper(),
            "lots": float(self.lots),
            "close_lots": float(self.close_lots),
            "intent": str(self.intent).upper(),
            "action": str(self.action),
            "action_score": float(self.action_score),
            "tp_price": self.tp_price,
            "sl_price": self.sl_price,
            "side": str(self.side).upper(),
            "lifecycle_action": str(self.lifecycle_action),
        }
        payload.update(dict(self.metadata or {}))
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ApprovedOrderIntent":
        return cls(
            command=str(payload.get("command") or payload.get("cmd") or ""),
            symbol=str(payload.get("symbol") or "").upper(),
            lots=float(payload.get("lots", 0.0) or 0.0),
            close_lots=float(payload.get("close_lots", 0.0) or 0.0),
            side=str(payload.get("side") or "BUY").upper(),
            intent=str(payload.get("intent") or "ENTRY").upper(),
            action=str(payload.get("action") or "entry"),
            action_score=float(payload.get("action_score", 0.0) or 0.0),
            tp_price=(None if payload.get("tp_price") is None else float(payload.get("tp_price"))),
            sl_price=(None if payload.get("sl_price") is None else float(payload.get("sl_price"))),
            risk_budget_pct=float(payload.get("risk_budget_pct", 0.0) or 0.0),
            lifecycle_action=str(payload.get("lifecycle_action") or "entry"),  # type: ignore[arg-type]
            metadata=dict(payload.get("metadata") or {}),
        )


@dataclass(slots=True)
class RiskDecision:
    pair: str
    verdict: str
    reason: str = ""
    policy_intent: PolicyIntent = field(default_factory=lambda: PolicyIntent(pair=""))
    market_state: MarketState = field(default_factory=lambda: MarketState(pair=""))
    portfolio_state: PortfolioState = field(default_factory=PortfolioState)
    trace: list[RiskRuleTrace] = field(default_factory=list)
    approved_order: ApprovedOrderIntent | None = None
    final_lots: float = 0.0
    close_lots: float = 0.0
    risk_reduction_pct: float = 0.0
    lifecycle_action: LifecycleAction = "hold"
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["policy_intent"] = self.policy_intent.to_dict()
        payload["market_state"] = self.market_state.to_dict()
        payload["portfolio_state"] = self.portfolio_state.to_dict()
        payload["trace"] = [item.to_dict() for item in self.trace]
        payload["approved_order"] = None if self.approved_order is None else self.approved_order.to_dict()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RiskDecision":
        approved = payload.get("approved_order")
        return cls(
            pair=str(payload.get("pair") or "").upper(),
            verdict=str(payload.get("verdict") or "hold"),
            reason=str(payload.get("reason") or ""),
            policy_intent=PolicyIntent.from_dict(dict(payload.get("policy_intent") or {"pair": payload.get("pair", "")})),
            market_state=MarketState.from_dict(dict(payload.get("market_state") or {"pair": payload.get("pair", "")})),
            portfolio_state=PortfolioState.from_dict(dict(payload.get("portfolio_state") or {})),
            trace=[RiskRuleTrace.from_dict(dict(item)) for item in list(payload.get("trace") or [])],
            approved_order=None if approved is None else ApprovedOrderIntent.from_dict(dict(approved)),
            final_lots=float(payload.get("final_lots", 0.0) or 0.0),
            close_lots=float(payload.get("close_lots", 0.0) or 0.0),
            risk_reduction_pct=float(payload.get("risk_reduction_pct", 0.0) or 0.0),
            lifecycle_action=str(payload.get("lifecycle_action") or "hold"),  # type: ignore[arg-type]
            metadata=dict(payload.get("metadata") or {}),
        )
