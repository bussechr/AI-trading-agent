from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fxstack.risk.contracts import (
    ApprovedOrderIntent,
    LifecycleAction,
    MarketState,
    PortfolioState,
    PolicyIntent,
    RiskDecision,
    RiskRuleTrace,
)


@dataclass(slots=True)
class RiskKernelConfig:
    max_spread_bps: float = 0.0
    max_session_spread_bps: dict[str, float] = field(default_factory=dict)
    freshness_limit_secs: float = 0.0
    max_total_positions: int = 0
    max_pair_positions: int = 0
    max_drawdown_pct: float = 0.0
    max_gross_exposure: float = 0.0
    max_net_exposure: float = 0.0
    min_lots: float = 0.01
    lot_step: float = 0.01
    max_lots: float = 0.0
    allow_lifecycle_overrides: bool = True
    session_spread_overrides: dict[str, float] = field(default_factory=dict)
    lifecycle_exit_verdicts: tuple[str, ...] = ("exit", "partial_tp")
    lifecycle_hold_verdicts: tuple[str, ...] = ("hold",)
    freshness_fail_verdict: str = "block"
    marketability_fail_verdict: str = "block"
    spread_fail_verdict: str = "block"
    exposure_fail_verdict: str = "block"
    drawdown_fail_verdict: str = "block"
    rollout_mode: str = ""
    rollout_pair_allowlisted: bool = False
    rollout_budget_scale: float = 1.0
    rollout_max_total_positions: int = 0
    rollout_max_pair_positions: int = 0
    rollout_max_gross_exposure: float = 0.0
    rollout_max_net_exposure: float = 0.0
    order_builder: Callable[[PolicyIntent, MarketState, PortfolioState], ApprovedOrderIntent | None] | None = None


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _round_lots(value: float, *, min_lot: float, lot_step: float, max_lot: float) -> float:
    lots = max(0.0, float(value))
    step = max(1e-9, float(lot_step))
    lots = (lots // step) * step
    lots = max(float(min_lot), lots)
    if max_lot > 0.0:
        lots = min(float(max_lot), lots)
    return round(float(lots), 8)


def _verdict_for_rule(rule: str, allowed: bool, *, fail_verdict: str) -> str:
    if allowed:
        return "allow"
    if str(fail_verdict).strip():
        return str(fail_verdict)
    return "block"


def _rule_trace(rule: str, verdict: str, reason: str, *, score: float | None = None, changed: bool = False, details: dict[str, Any] | None = None) -> RiskRuleTrace:
    return RiskRuleTrace(
        rule=str(rule),
        verdict=str(verdict),  # type: ignore[arg-type]
        reason=str(reason),
        score=score,
        changed_decision=bool(changed),
        details=dict(details or {}),
    )


def _session_spread_limit(config: RiskKernelConfig, session_bucket: str) -> float:
    bucket = str(session_bucket or "").strip().lower()
    if bucket and bucket in config.session_spread_overrides:
        return float(config.session_spread_overrides[bucket])
    return float(config.max_spread_bps)


def _effective_positive_limit(base_value: float, override_value: float) -> float:
    base = float(base_value)
    override = float(override_value)
    if override <= 0.0:
        return base
    if base <= 0.0:
        return override
    return min(base, override)


def _effective_positive_int_limit(base_value: int, override_value: int) -> int:
    base = int(base_value or 0)
    override = int(override_value or 0)
    if override <= 0:
        return base
    if base <= 0:
        return override
    return min(base, override)


def _rollout_budget_scale(config: RiskKernelConfig) -> float:
    if str(config.rollout_mode or "").strip().lower() != "canary":
        return 1.0
    return _clamp(float(config.rollout_budget_scale), 0.0, 1.0)


def _entry_budget_plan(*, intent: PolicyIntent, portfolio: PortfolioState, config: RiskKernelConfig) -> dict[str, Any]:
    requested_target_risk_pct = float(intent.metadata.get("target_risk_pct", 0.0) or 0.0)
    requested_lots = float(intent.metadata.get("requested_lots", intent.metadata.get("planned_entry_lots", 0.0)) or 0.0)
    budget_scale = _rollout_budget_scale(config)
    rollout_active = bool(str(config.rollout_mode or "").strip().lower() == "canary" and config.rollout_pair_allowlisted)
    if requested_target_risk_pct > 0.0:
        source = "target_risk_pct"
        raw_lots_requested = float(portfolio.equity) * float(requested_target_risk_pct)
        effective_target_risk_pct = float(requested_target_risk_pct) * float(budget_scale if rollout_active else 1.0)
        raw_lots_effective = float(portfolio.equity) * float(effective_target_risk_pct)
    else:
        source = "requested_lots"
        raw_lots_requested = float(requested_lots)
        effective_target_risk_pct = 0.0
        raw_lots_effective = float(requested_lots) * float(budget_scale if rollout_active else 1.0)
    return {
        "source": str(source),
        "budget_scale": float(budget_scale if rollout_active else 1.0),
        "requested_target_risk_pct": float(requested_target_risk_pct),
        "effective_target_risk_pct": float(effective_target_risk_pct),
        "requested_lots": float(requested_lots),
        "raw_lots_requested": float(raw_lots_requested),
        "raw_lots_effective": float(raw_lots_effective),
        "reduced_budget": bool(rollout_active and raw_lots_effective + 1e-12 < raw_lots_requested),
    }


def _final_order(
    *,
    intent: PolicyIntent,
    market: MarketState,
    portfolio: PortfolioState,
    config: RiskKernelConfig,
    lifecycle_action: LifecycleAction,
    close_lots: float,
) -> tuple[ApprovedOrderIntent | None, dict[str, Any]]:
    builder = config.order_builder
    if builder is not None:
        built = builder(intent, market, portfolio)
        return built, {"source": "custom_builder", "budget_scale": 1.0}

    side_up = str(intent.side).upper()
    if lifecycle_action == "hold":
        return None, {}
    if lifecycle_action == "tighten_stop":
        sl_price = float(intent.metadata.get("sl_price", 0.0) or 0.0)
        if sl_price <= 0.0:
            return None, {}
        return (
            ApprovedOrderIntent(
                command="MODIFY_SL",
                symbol=str(intent.pair).upper(),
                lots=0.0,
                close_lots=0.0,
                side=side_up if side_up in {"BUY", "SELL"} else "BUY",
                intent="ADJUST_MODEL",
                action="tighten_stop",
                action_score=float(intent.action_score),
                sl_price=sl_price,
                lifecycle_action="tighten_stop",
                metadata=dict(intent.metadata or {}),
            ),
            {},
        )
    if lifecycle_action in {"exit", "partial_tp"}:
        close_lots = float(intent.metadata.get("close_lots", 0.0) or close_lots)
        return (
            ApprovedOrderIntent(
                command="CLOSE" if lifecycle_action == "exit" else "CLOSE_PARTIAL",
                symbol=str(intent.pair).upper(),
                lots=0.0 if lifecycle_action == "exit" else float(close_lots),
                close_lots=float(close_lots),
                side=side_up if side_up in {"BUY", "SELL"} else "BUY",
                intent=str(intent.intent or "EXIT_MODEL").upper(),
                action="exit" if lifecycle_action == "exit" else "partial_tp",
                action_score=float(intent.action_score),
                lifecycle_action=lifecycle_action,
                metadata=dict(intent.metadata or {}),
            ),
            {},
        )
    if side_up not in {"BUY", "SELL"}:
        return None, {}

    budget_plan = _entry_budget_plan(intent=intent, portfolio=portfolio, config=config)
    raw_lots = float(budget_plan.get("raw_lots_effective", 0.0))
    if float(budget_plan.get("effective_target_risk_pct", 0.0)) > 0.0:
        raw_lots = _clamp(
            raw_lots,
            float(config.min_lots),
            float(config.max_lots) if float(config.max_lots) > 0.0 else float("inf"),
        )
    final_lots = _round_lots(raw_lots, min_lot=config.min_lots, lot_step=config.lot_step, max_lot=config.max_lots)
    if final_lots <= 0.0:
        return None, budget_plan
    budget_plan["final_lots"] = float(final_lots)
    return (
        ApprovedOrderIntent(
            command="BUY" if side_up == "BUY" else "SELL",
            symbol=str(intent.pair).upper(),
            lots=float(final_lots),
            close_lots=0.0,
            side=side_up,
            intent=str(intent.intent).upper(),
            action=str(intent.action or "entry"),
            action_score=float(intent.action_score),
            tp_price=intent.metadata.get("tp_price"),
            sl_price=intent.metadata.get("sl_price"),
            risk_budget_pct=float(budget_plan.get("effective_target_risk_pct", 0.0)),
            lifecycle_action="entry",
            metadata=dict(intent.metadata or {}),
        ),
        budget_plan,
    )


def evaluate_risk_decision(
    *,
    policy_intent: PolicyIntent,
    market_state: MarketState,
    portfolio_state: PortfolioState,
    config: RiskKernelConfig | None = None,
) -> RiskDecision:
    cfg = config or RiskKernelConfig()
    trace: list[RiskRuleTrace] = []
    verdict = "allow"
    reason = "approved"
    lifecycle_action: LifecycleAction = "hold"
    close_lots = 0.0
    requested_lifecycle_action = str(policy_intent.metadata.get("lifecycle_action") or policy_intent.action or "").strip().lower()
    has_open_position = bool(policy_intent.metadata.get("has_open_position", False))
    managing_existing_position = bool(has_open_position or requested_lifecycle_action in {"hold", "partial_tp", "exit", "tighten_stop", "modify_sl"})
    rollout_mode = str(cfg.rollout_mode or "").strip().lower()
    rollout_configured = bool(rollout_mode == "canary")
    rollout_pair_allowlisted = bool(cfg.rollout_pair_allowlisted)
    rollout_budget_scale = _rollout_budget_scale(cfg) if rollout_configured else 1.0
    effective_gross_exposure_limit = _effective_positive_limit(cfg.max_gross_exposure, cfg.rollout_max_gross_exposure if rollout_pair_allowlisted else 0.0)
    effective_net_exposure_limit = _effective_positive_limit(cfg.max_net_exposure, cfg.rollout_max_net_exposure if rollout_pair_allowlisted else 0.0)
    effective_total_positions = _effective_positive_int_limit(cfg.max_total_positions, cfg.rollout_max_total_positions if rollout_pair_allowlisted else 0)
    effective_pair_positions = _effective_positive_int_limit(cfg.max_pair_positions, cfg.rollout_max_pair_positions if rollout_pair_allowlisted else 0)
    rollout_budget_plan = _entry_budget_plan(intent=policy_intent, portfolio=portfolio_state, config=cfg)
    rollout_reduced_budget = bool(rollout_configured and rollout_pair_allowlisted and rollout_budget_plan.get("reduced_budget", False))
    rollout_breach = False
    rollout_breach_reason = ""

    def _rollout_metadata(
        *,
        final_lots: float = 0.0,
        effective_target_risk_pct: float | None = None,
        raw_lots_effective: float | None = None,
        reduced_budget: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "configured": bool(rollout_configured),
            "active": bool(rollout_configured and rollout_pair_allowlisted),
            "mode": rollout_mode,
            "pair_allowlisted": bool(rollout_pair_allowlisted),
            "budget_scale": float(rollout_budget_scale if rollout_configured and rollout_pair_allowlisted else 1.0),
            "source": str(policy_intent.metadata.get("rollout_source") or ""),
            "requested_lots": float(rollout_budget_plan.get("requested_lots", 0.0)),
            "requested_target_risk_pct": float(rollout_budget_plan.get("requested_target_risk_pct", 0.0)),
            "effective_target_risk_pct": float(
                rollout_budget_plan.get("effective_target_risk_pct", 0.0)
                if effective_target_risk_pct is None
                else effective_target_risk_pct
            ),
            "raw_lots_requested": float(rollout_budget_plan.get("raw_lots_requested", 0.0)),
            "raw_lots_effective": float(
                rollout_budget_plan.get("raw_lots_effective", 0.0)
                if raw_lots_effective is None
                else raw_lots_effective
            ),
            "final_lots": float(final_lots),
            "reduced_budget": bool(rollout_reduced_budget if reduced_budget is None else reduced_budget),
            "breach": bool(rollout_breach),
            "breach_reason": str(rollout_breach_reason),
            "effective_max_total_positions": int(effective_total_positions),
            "effective_max_pair_positions": int(effective_pair_positions),
            "effective_max_gross_exposure": float(effective_gross_exposure_limit),
            "effective_max_net_exposure": float(effective_net_exposure_limit),
        }

    # 1. Data freshness
    freshness_ok = bool(market_state.data_fresh)
    if market_state.freshness_limit_secs is not None and market_state.freshness_secs is not None:
        freshness_ok = freshness_ok and float(market_state.freshness_secs) <= float(market_state.freshness_limit_secs)
    if cfg.freshness_limit_secs > 0.0 and market_state.freshness_secs is not None:
        freshness_ok = freshness_ok and float(market_state.freshness_secs) <= float(cfg.freshness_limit_secs)
    if managing_existing_position:
        trace.append(
            _rule_trace(
                "data_freshness",
                "allow",
                "bypass_existing_position",
                details={"freshness_secs": market_state.freshness_secs, "limit_secs": market_state.freshness_limit_secs or cfg.freshness_limit_secs},
            )
        )
    else:
        trace.append(_rule_trace("data_freshness", _verdict_for_rule("data_freshness", freshness_ok, fail_verdict=cfg.freshness_fail_verdict), reason if not freshness_ok else "fresh", score=None if market_state.freshness_secs is None else float(market_state.freshness_secs), details={"freshness_secs": market_state.freshness_secs, "limit_secs": market_state.freshness_limit_secs or cfg.freshness_limit_secs}))
    if (not managing_existing_position) and (not freshness_ok):
        verdict = cfg.freshness_fail_verdict
        reason = "data_stale"
    if (not managing_existing_position) and (not freshness_ok):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "data_freshness", "rollout": _rollout_metadata()},
        )

    # 2. Marketability
    policy_allowed = bool(policy_intent.metadata.get("policy_allowed", True))
    policy_block_reason = str(policy_intent.metadata.get("policy_block_reason") or policy_intent.metadata.get("rejection_reason") or "").strip()
    marketable = bool(market_state.marketable and market_state.market_open and policy_allowed)
    if (not managing_existing_position) and (not marketable):
        verdict = cfg.marketability_fail_verdict
        reason = policy_block_reason or "market_not_marketable"
    trace.append(_rule_trace("marketability", "allow" if managing_existing_position else _verdict_for_rule("marketability", marketable, fail_verdict=cfg.marketability_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not marketable else "marketable"), details={"market_open": bool(market_state.market_open), "marketable": bool(market_state.marketable), "policy_allowed": bool(policy_allowed), "policy_block_reason": str(policy_block_reason)}))
    if (not managing_existing_position) and (not marketable):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "marketability", "rollout": _rollout_metadata()},
        )

    # 3. Spread / session
    session_limit = _session_spread_limit(cfg, market_state.session_bucket)
    effective_spread_limit = float(session_limit if session_limit > 0.0 else market_state.allowed_spread_bps)
    spread_ok = True if effective_spread_limit <= 0.0 else float(market_state.spread_bps) <= float(effective_spread_limit)
    if (not managing_existing_position) and (not spread_ok):
        verdict = cfg.spread_fail_verdict
        reason = "spread_too_wide"
    trace.append(_rule_trace("spread_session", "allow" if managing_existing_position else _verdict_for_rule("spread_session", spread_ok, fail_verdict=cfg.spread_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not spread_ok else "spread_ok"), details={"spread_bps": float(market_state.spread_bps), "session_bucket": market_state.session_bucket, "limit_bps": float(effective_spread_limit)}))
    if (not managing_existing_position) and (not spread_ok):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "spread_session", "rollout": _rollout_metadata()},
        )

    # 4. Exposure
    exposure_ok = True
    if effective_gross_exposure_limit > 0.0:
        exposure_ok = exposure_ok and float(portfolio_state.gross_exposure) <= float(effective_gross_exposure_limit)
    if effective_net_exposure_limit > 0.0:
        exposure_ok = exposure_ok and abs(float(portfolio_state.net_exposure)) <= float(effective_net_exposure_limit)
    if (not managing_existing_position) and (not exposure_ok):
        verdict = cfg.exposure_fail_verdict
        rollout_constrained = bool(
            rollout_pair_allowlisted
            and (
                (cfg.rollout_max_gross_exposure > 0.0 and ((cfg.max_gross_exposure <= 0.0) or effective_gross_exposure_limit < float(cfg.max_gross_exposure)))
                or (cfg.rollout_max_net_exposure > 0.0 and ((cfg.max_net_exposure <= 0.0) or effective_net_exposure_limit < float(cfg.max_net_exposure)))
            )
        )
        reason = "rollout_exposure_limit" if rollout_constrained else "exposure_limit"
        rollout_breach = rollout_constrained
        rollout_breach_reason = reason if rollout_constrained else rollout_breach_reason
    trace.append(_rule_trace("exposure", "allow" if managing_existing_position else _verdict_for_rule("exposure", exposure_ok, fail_verdict=cfg.exposure_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not exposure_ok else "exposure_ok"), details={"gross_exposure": float(portfolio_state.gross_exposure), "net_exposure": float(portfolio_state.net_exposure), "gross_limit": float(effective_gross_exposure_limit), "net_limit": float(effective_net_exposure_limit), "base_gross_limit": float(cfg.max_gross_exposure), "base_net_limit": float(cfg.max_net_exposure), "rollout_gross_limit": float(cfg.rollout_max_gross_exposure), "rollout_net_limit": float(cfg.rollout_max_net_exposure)}))
    if (not managing_existing_position) and (not exposure_ok):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "exposure", "rollout": _rollout_metadata()},
        )

    # 5. Position caps
    caps_ok = True
    if effective_total_positions > 0:
        caps_ok = caps_ok and int(portfolio_state.open_position_count) < int(effective_total_positions)
    if effective_pair_positions > 0:
        caps_ok = caps_ok and int(portfolio_state.pair_position_count) < int(effective_pair_positions)
    if (not managing_existing_position) and (not caps_ok):
        verdict = "block"
        rollout_constrained = bool(
            rollout_pair_allowlisted
            and (
                (effective_total_positions > 0 and effective_total_positions != int(cfg.max_total_positions or 0))
                or (effective_pair_positions > 0 and effective_pair_positions != int(cfg.max_pair_positions or 0))
            )
        )
        reason = "rollout_position_caps" if rollout_constrained else "position_caps"
        rollout_breach = rollout_constrained
        rollout_breach_reason = reason if rollout_constrained else rollout_breach_reason
    trace.append(_rule_trace("position_caps", "allow" if managing_existing_position else _verdict_for_rule("position_caps", caps_ok, fail_verdict="block"), "bypass_existing_position" if managing_existing_position else (reason if not caps_ok else "caps_ok"), details={"open_position_count": int(portfolio_state.open_position_count), "pair_position_count": int(portfolio_state.pair_position_count), "max_total_positions": int(effective_total_positions), "max_pair_positions": int(effective_pair_positions), "base_max_total_positions": int(cfg.max_total_positions), "base_max_pair_positions": int(cfg.max_pair_positions), "rollout_max_total_positions": int(cfg.rollout_max_total_positions), "rollout_max_pair_positions": int(cfg.rollout_max_pair_positions)}))
    if (not managing_existing_position) and (not caps_ok):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "position_caps", "rollout": _rollout_metadata()},
        )

    # 6. Drawdown
    drawdown_ok = True
    if cfg.max_drawdown_pct > 0.0:
        drawdown_ok = float(portfolio_state.drawdown_pct) <= float(cfg.max_drawdown_pct)
    if (not managing_existing_position) and (not drawdown_ok):
        verdict = cfg.drawdown_fail_verdict
        reason = "drawdown_limit"
    trace.append(_rule_trace("drawdown", "allow" if managing_existing_position else _verdict_for_rule("drawdown", drawdown_ok, fail_verdict=cfg.drawdown_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not drawdown_ok else "drawdown_ok"), details={"drawdown_pct": float(portfolio_state.drawdown_pct), "max_drawdown_pct": float(cfg.max_drawdown_pct)}))
    if (not managing_existing_position) and (not drawdown_ok):
        return RiskDecision(
            pair=policy_intent.pair,
            verdict=verdict,
            reason=reason,
            policy_intent=policy_intent,
            market_state=market_state,
            portfolio_state=portfolio_state,
            trace=trace,
            lifecycle_action="hold",
            metadata={"rule": "drawdown", "rollout": _rollout_metadata()},
        )

    # 7. Canary rollout
    if rollout_configured:
        rollout_changed = bool(
            rollout_reduced_budget
            or (effective_total_positions > 0 and effective_total_positions != int(cfg.max_total_positions or 0))
            or (effective_pair_positions > 0 and effective_pair_positions != int(cfg.max_pair_positions or 0))
            or (effective_gross_exposure_limit > 0.0 and effective_gross_exposure_limit != float(cfg.max_gross_exposure or 0.0))
            or (effective_net_exposure_limit > 0.0 and effective_net_exposure_limit != float(cfg.max_net_exposure or 0.0))
        )
        rollout_reason = "canary_budget_reduced" if rollout_changed else "canary_ok"
        rollout_verdict = "reduce" if rollout_changed else "allow"
        if (not managing_existing_position) and (not rollout_pair_allowlisted):
            verdict = "block"
            reason = "rollout_pair_not_allowlisted"
            rollout_breach = True
            rollout_breach_reason = str(reason)
            trace.append(
                _rule_trace(
                    "rollout_canary",
                    "block",
                    reason,
                    changed=True,
                    details={
                        "mode": rollout_mode,
                        "pair_allowlisted": bool(rollout_pair_allowlisted),
                        "budget_scale": float(rollout_budget_scale),
                    },
                )
            )
            return RiskDecision(
                pair=policy_intent.pair,
                verdict=verdict,
                reason=reason,
                policy_intent=policy_intent,
                market_state=market_state,
                portfolio_state=portfolio_state,
                trace=trace,
                lifecycle_action="hold",
                metadata={
                    "rule": "rollout_canary",
                    "rollout": _rollout_metadata(reduced_budget=False),
                },
            )
        if (not managing_existing_position) and rollout_pair_allowlisted and rollout_budget_scale <= 0.0:
            verdict = "block"
            reason = "rollout_budget_zero"
            rollout_breach = True
            rollout_breach_reason = str(reason)
            trace.append(
                _rule_trace(
                    "rollout_canary",
                    "block",
                    reason,
                    changed=True,
                    details={
                        "mode": rollout_mode,
                        "pair_allowlisted": bool(rollout_pair_allowlisted),
                        "budget_scale": float(rollout_budget_scale),
                    },
                )
            )
            return RiskDecision(
                pair=policy_intent.pair,
                verdict=verdict,
                reason=reason,
                policy_intent=policy_intent,
                market_state=market_state,
                portfolio_state=portfolio_state,
                trace=trace,
                lifecycle_action="hold",
                metadata={
                    "rule": "rollout_canary",
                    "rollout": _rollout_metadata(reduced_budget=False),
                },
            )
        trace.append(
            _rule_trace(
                "rollout_canary",
                rollout_verdict if not managing_existing_position else "allow",
                "bypass_existing_position" if managing_existing_position else rollout_reason,
                changed=rollout_changed and (not managing_existing_position),
                details={
                    "mode": rollout_mode,
                    "pair_allowlisted": bool(rollout_pair_allowlisted),
                    "budget_scale": float(rollout_budget_scale),
                    "requested_lots": float(rollout_budget_plan.get("requested_lots", 0.0)),
                    "requested_target_risk_pct": float(rollout_budget_plan.get("requested_target_risk_pct", 0.0)),
                    "effective_target_risk_pct": float(rollout_budget_plan.get("effective_target_risk_pct", 0.0)),
                    "effective_max_total_positions": int(effective_total_positions),
                    "effective_max_pair_positions": int(effective_pair_positions),
                    "effective_max_gross_exposure": float(effective_gross_exposure_limit),
                    "effective_max_net_exposure": float(effective_net_exposure_limit),
                },
            )
        )

    # 8. Lifecycle overrides
    lifecycle_action = "entry"
    if cfg.allow_lifecycle_overrides:
        intent_lifecycle = requested_lifecycle_action
        if intent_lifecycle in {"hold", "partial_tp", "exit", "modify_sl", "tighten_stop"}:
            lifecycle_action = ("tighten_stop" if intent_lifecycle in {"tighten_stop", "modify_sl"} else intent_lifecycle)  # type: ignore[assignment]
        if lifecycle_action == "partial_tp" and float(policy_intent.metadata.get("close_lots", 0.0) or 0.0) <= 0.0:
            lifecycle_action = "hold"
        if lifecycle_action == "exit":
            close_lots = float(policy_intent.metadata.get("close_lots", 0.0) or 0.0)
    trace.append(
        _rule_trace(
            "lifecycle_overrides",
            "allow" if lifecycle_action != "hold" or str(policy_intent.action).strip() else "allow",
            lifecycle_action,
            details={"lifecycle_action": lifecycle_action, "close_lots": float(close_lots)},
        )
    )

    # 9. Final sizing / order instructions
    approved_order, budget_plan = _final_order(
        intent=policy_intent,
        market=market_state,
        portfolio=portfolio_state,
        config=cfg,
        lifecycle_action=lifecycle_action,
        close_lots=close_lots,
    )
    if approved_order is None and lifecycle_action == "hold":
        verdict = "hold"
        reason = "no_order_required"
    elif approved_order is None and lifecycle_action not in {"hold"}:
        verdict = "block"
        reason = "order_build_failed"
    else:
        verdict = "allow"
        reason = "approved"
    trace.append(
        _rule_trace(
            "final_sizing_order",
            "allow" if approved_order is not None else ("hold" if lifecycle_action == "hold" else "block"),
            reason,
            details={"final_lots": float(approved_order.lots) if approved_order is not None else 0.0, "close_lots": float(approved_order.close_lots) if approved_order is not None else float(close_lots), "command": approved_order.command if approved_order is not None else "", "budget_plan": dict(budget_plan or {})},
        )
    )

    if rollout_configured and rollout_pair_allowlisted and lifecycle_action == "entry" and bool(budget_plan.get("reduced_budget", False)):
        rollout_breach = True
        if not rollout_breach_reason:
            rollout_breach_reason = "rollout_budget_reduced"

    return RiskDecision(
        pair=policy_intent.pair,
        verdict=verdict,
        reason=reason,
        policy_intent=policy_intent,
        market_state=market_state,
        portfolio_state=portfolio_state,
        trace=trace,
        approved_order=approved_order,
        final_lots=float(approved_order.lots) if approved_order is not None else 0.0,
        close_lots=float(approved_order.close_lots) if approved_order is not None else float(close_lots),
        lifecycle_action=lifecycle_action,
        metadata={
            "command": approved_order.command if approved_order is not None else "",
            "final_lots": float(approved_order.lots) if approved_order is not None else 0.0,
            "close_lots": float(approved_order.close_lots) if approved_order is not None else float(close_lots),
            "rollout": _rollout_metadata(
                final_lots=float(approved_order.lots) if approved_order is not None else 0.0,
                effective_target_risk_pct=float(
                    budget_plan.get("effective_target_risk_pct", rollout_budget_plan.get("effective_target_risk_pct", 0.0))
                ),
                raw_lots_effective=float(
                    budget_plan.get("raw_lots_effective", rollout_budget_plan.get("raw_lots_effective", 0.0))
                ),
                reduced_budget=bool(budget_plan.get("reduced_budget", rollout_reduced_budget)),
            ),
        },
    )
