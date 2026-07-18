from __future__ import annotations

from dataclasses import dataclass, field
import math
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
    return max(lower, min(upper, _safe_float(value, lower)))


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        out = float(default)
    if math.isfinite(out):
        return out
    fallback = float(default)
    return fallback if math.isfinite(fallback) else 0.0


def _is_finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _round_lots(value: float, *, min_lot: float, lot_step: float, max_lot: float) -> float:
    lots = max(0.0, _safe_float(value, 0.0))
    if lots <= 0.0:
        return 0.0
    min_lot = max(0.0, _safe_float(min_lot, 0.0))
    step = max(1e-9, _safe_float(lot_step, 0.01))
    tolerance = max(1e-9, step / 10.0)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
    lots = math.floor((lots + tolerance) / step) * step
    if min_lot > 0.0 and lots + tolerance < min_lot:
        lots = float(min_lot)
    max_lot = max(0.0, _safe_float(max_lot, 0.0))
    if max_lot > 0.0:
        lots = min(max_lot, lots)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
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
        score=None if score is None or not _is_finite(score) else float(score),
        changed_decision=bool(changed),
        details=_json_safe(dict(details or {})),
    )


def _session_spread_limit(config: RiskKernelConfig, session_bucket: str) -> float:
    bucket = str(session_bucket or "").strip().lower()
    if bucket and bucket in config.session_spread_overrides:
        return _safe_float(config.session_spread_overrides[bucket], 0.0)
    return _safe_float(config.max_spread_bps, 0.0)


def _effective_positive_limit(base_value: float, override_value: float) -> float:
    base = max(0.0, _safe_float(base_value, 0.0))
    override = max(0.0, _safe_float(override_value, 0.0))
    if override <= 0.0:
        return base
    if base <= 0.0:
        return override
    return min(base, override)


def _effective_positive_int_limit(base_value: int, override_value: int) -> int:
    base = max(0, int(_safe_float(base_value, 0.0)))
    override = max(0, int(_safe_float(override_value, 0.0)))
    if override <= 0:
        return base
    if base <= 0:
        return override
    return min(base, override)


def _rollout_budget_scale(config: RiskKernelConfig) -> float:
    if str(config.rollout_mode or "").strip().lower() != "canary":
        return 1.0
    return _clamp(config.rollout_budget_scale, 0.0, 1.0)


def _normalize_exposure_unit(value: Any) -> str:
    unit = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "lot": "lot_units",
        "lots": "lot_units",
        "contracts": "lot_units",
        "contract_units": "lot_units",
        "units": "lot_units",
        "notional": "notional_units",
        "quote_notional": "notional_units",
        "exposure_notional": "notional_units",
        "gross_notional": "notional_units",
    }
    return aliases.get(unit, unit)


def _portfolio_exposure_state(portfolio: PortfolioState) -> tuple[float, float, str, bool, bool, str]:
    metadata = dict(getattr(portfolio, "metadata", {}) or {})
    portfolio_book = dict(metadata.get("portfolio_book") or {})
    portfolio_telemetry = dict(metadata.get("portfolio_telemetry") or {})
    gross_lot_exposure = portfolio_book.get(
        "gross_lot_exposure",
        portfolio_telemetry.get("gross_lot_exposure", metadata.get("gross_lot_exposure")),
    )
    net_lot_exposure = portfolio_book.get(
        "net_lot_exposure",
        portfolio_telemetry.get("net_lot_exposure", metadata.get("net_lot_exposure")),
    )
    if gross_lot_exposure is not None or net_lot_exposure is not None:
        values_finite = bool(
            (gross_lot_exposure is None or _is_finite(gross_lot_exposure))
            and (net_lot_exposure is None or _is_finite(net_lot_exposure))
        )
        return (
            float(_safe_float(gross_lot_exposure, 0.0)),
            float(_safe_float(net_lot_exposure, 0.0)),
            "lot_units",
            True,
            values_finite,
            "lot_metadata",
        )
    exposure_unit = _normalize_exposure_unit(
        portfolio_book.get(
            "exposure_unit",
            portfolio_telemetry.get("exposure_unit", metadata.get("exposure_unit", "")),
        )
    )
    exposure_math_safe = exposure_unit in {"", "lot_units"}
    values_finite = bool(_is_finite(portfolio.gross_exposure) and _is_finite(portfolio.net_exposure))
    return (
        _safe_float(portfolio.gross_exposure, 0.0),
        _safe_float(portfolio.net_exposure, 0.0),
        str("lot_units" if exposure_math_safe else exposure_unit or "portfolio_state"),
        bool(exposure_math_safe),
        values_finite,
        "portfolio_state",
    )


def _entry_budget_plan(*, intent: PolicyIntent, portfolio: PortfolioState, config: RiskKernelConfig) -> dict[str, Any]:
    target_raw = intent.metadata.get("target_risk_pct", 0.0)
    lots_raw = intent.metadata.get("requested_lots", intent.metadata.get("planned_entry_lots", 0.0))
    requested_target_risk_pct = _safe_float(target_raw, 0.0)
    requested_lots = _safe_float(lots_raw, 0.0)
    numeric_errors: list[str] = []
    for name, value in {
        "requested_lots": lots_raw,
        "target_risk_pct": target_raw,
        "action_score": intent.action_score,
        "expected_edge_bps": intent.expected_edge_bps,
        "confidence": intent.confidence,
        "min_lots": config.min_lots,
        "lot_step": config.lot_step,
        "max_lots": config.max_lots,
    }.items():
        if value is not None and not _is_finite(value):
            numeric_errors.append(f"nonfinite:{name}")
    for name, value in {"requested_lots": requested_lots, "target_risk_pct": requested_target_risk_pct}.items():
        if value < 0.0:
            numeric_errors.append(f"out_of_range:{name}")
    for name, value in {"action_score": intent.action_score, "confidence": intent.confidence}.items():
        if _is_finite(value) and not 0.0 <= float(value) <= 1.0:
            numeric_errors.append(f"out_of_range:{name}")
    if _is_finite(config.min_lots) and float(config.min_lots) < 0.0:
        numeric_errors.append("out_of_range:min_lots")
    if _is_finite(config.lot_step) and float(config.lot_step) <= 0.0:
        numeric_errors.append("out_of_range:lot_step")
    if _is_finite(config.max_lots) and float(config.max_lots) < 0.0:
        numeric_errors.append("out_of_range:max_lots")
    for price_name in ("tp_price", "sl_price"):
        price = intent.metadata.get(price_name)
        if price is not None and (not _is_finite(price) or float(price) <= 0.0):
            numeric_errors.append(f"invalid:{price_name}")
    numeric_errors = sorted(set(numeric_errors))
    budget_scale = _rollout_budget_scale(config)
    rollout_active = bool(str(config.rollout_mode or "").strip().lower() == "canary" and config.rollout_pair_allowlisted)
    source = "target_risk_pct" if requested_target_risk_pct > 0.0 else "requested_lots"
    raw_lots_requested = float(requested_lots)
    effective_target_risk_pct = float(requested_target_risk_pct) * float(budget_scale if rollout_active else 1.0) if requested_target_risk_pct > 0.0 else 0.0
    raw_lots_effective = float(requested_lots) * float(budget_scale if rollout_active else 1.0)
    final_lots = _round_lots(raw_lots_effective, min_lot=config.min_lots, lot_step=config.lot_step, max_lot=config.max_lots)
    rejection_reason = "invalid_order_numeric_contract" if numeric_errors else ""
    if not rejection_reason and requested_target_risk_pct > 0.0 and requested_lots <= 0.0:
        rejection_reason = "target_risk_pct_requires_custom_order_builder"
    elif not rejection_reason and raw_lots_effective > 0.0 and final_lots <= 0.0:
        rejection_reason = "requested_lots_below_min_lot"
    return {
        "source": str(source),
        "budget_scale": float(budget_scale if rollout_active else 1.0),
        "requested_target_risk_pct": float(requested_target_risk_pct),
        "effective_target_risk_pct": float(effective_target_risk_pct),
        "requested_lots": float(requested_lots),
        "raw_lots_requested": float(raw_lots_requested),
        "raw_lots_effective": float(raw_lots_effective),
        "final_lots": float(final_lots),
        "reduced_budget": bool(rollout_active and raw_lots_effective + 1e-12 < raw_lots_requested),
        "rejection_reason": str(rejection_reason),
        "numeric_inputs_valid": not numeric_errors,
        "numeric_input_errors": numeric_errors,
    }


def _approved_order_numeric_errors(order: ApprovedOrderIntent) -> list[str]:
    errors: list[str] = []
    for name, value in {
        "lots": order.lots,
        "close_lots": order.close_lots,
        "action_score": order.action_score,
        "risk_budget_pct": order.risk_budget_pct,
    }.items():
        if not _is_finite(value):
            errors.append(f"nonfinite:{name}")
    for name, value in {"lots": order.lots, "close_lots": order.close_lots, "risk_budget_pct": order.risk_budget_pct}.items():
        if _is_finite(value) and float(value) < 0.0:
            errors.append(f"out_of_range:{name}")
    if _is_finite(order.action_score) and not 0.0 <= float(order.action_score) <= 1.0:
        errors.append("out_of_range:action_score")
    for name, value in {"tp_price": order.tp_price, "sl_price": order.sl_price}.items():
        if value is not None and (not _is_finite(value) or float(value) <= 0.0):
            errors.append(f"invalid:{name}")
    command = str(order.command or "").strip().upper()
    if command in {"BUY", "SELL", "CLOSE_PARTIAL"} and _is_finite(order.lots) and float(order.lots) <= 0.0:
        errors.append("out_of_range:lots")
    return sorted(set(errors))


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
        sl_raw = intent.metadata.get("sl_price", 0.0)
        sl_price = _safe_float(sl_raw, 0.0)
        if not _is_finite(sl_raw) or sl_price <= 0.0:
            return None, {"rejection_reason": "invalid_sl_price"}
        return (
            ApprovedOrderIntent(
                command="MODIFY_SL",
                symbol=str(intent.pair).upper(),
                lots=0.0,
                close_lots=0.0,
                side=side_up if side_up in {"BUY", "SELL"} else "BUY",
                intent="ADJUST_MODEL",
                action="tighten_stop",
                action_score=_clamp(intent.action_score, 0.0, 1.0),
                sl_price=sl_price,
                lifecycle_action="tighten_stop",
                metadata=dict(intent.metadata or {}),
            ),
            {},
        )
    if lifecycle_action in {"exit", "partial_tp"}:
        close_raw = intent.metadata.get("close_lots", close_lots)
        if lifecycle_action == "partial_tp" and (not _is_finite(close_raw) or float(close_raw) <= 0.0):
            return None, {"rejection_reason": "invalid_close_lots"}
        close_lots = max(0.0, _safe_float(close_raw, 0.0))
        return (
            ApprovedOrderIntent(
                command="CLOSE" if lifecycle_action == "exit" else "CLOSE_PARTIAL",
                symbol=str(intent.pair).upper(),
                lots=0.0 if lifecycle_action == "exit" else float(close_lots),
                close_lots=float(close_lots),
                side=side_up if side_up in {"BUY", "SELL"} else "BUY",
                intent=str(intent.intent or "EXIT_MODEL").upper(),
                action="exit" if lifecycle_action == "exit" else "partial_tp",
                action_score=_clamp(intent.action_score, 0.0, 1.0),
                lifecycle_action=lifecycle_action,
                metadata=dict(intent.metadata or {}),
            ),
            {},
        )
    if side_up not in {"BUY", "SELL"}:
        return None, {}

    budget_plan = _entry_budget_plan(intent=intent, portfolio=portfolio, config=config)
    rejection_reason = str(budget_plan.get("rejection_reason") or "")
    if rejection_reason:
        return None, budget_plan
    final_lots = float(budget_plan.get("final_lots", 0.0))
    if final_lots <= 0.0:
        return None, budget_plan
    return (
        ApprovedOrderIntent(
            command="BUY" if side_up == "BUY" else "SELL",
            symbol=str(intent.pair).upper(),
            lots=float(final_lots),
            close_lots=0.0,
            side=side_up,
            intent=str(intent.intent).upper(),
            action=str(intent.action or "entry"),
            action_score=_clamp(intent.action_score, 0.0, 1.0),
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
    candidate_entry_lots = float(rollout_budget_plan.get("final_lots", 0.0))
    candidate_entry_side = str(policy_intent.side).upper()
    candidate_entry_signed_lots = candidate_entry_lots if candidate_entry_side == "BUY" else (-candidate_entry_lots if candidate_entry_side == "SELL" else 0.0)
    (
        portfolio_gross_exposure,
        portfolio_net_exposure,
        exposure_unit,
        exposure_math_safe,
        exposure_values_finite,
        exposure_source,
    ) = _portfolio_exposure_state(portfolio_state)

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
    freshness_value_valid = bool(
        market_state.freshness_secs is None
        or (_is_finite(market_state.freshness_secs) and float(market_state.freshness_secs) >= 0.0)
    )
    market_freshness_limit_valid = bool(
        market_state.freshness_limit_secs is None
        or (_is_finite(market_state.freshness_limit_secs) and float(market_state.freshness_limit_secs) >= 0.0)
    )
    config_freshness_limit_valid = bool(
        _is_finite(cfg.freshness_limit_secs) and float(cfg.freshness_limit_secs) >= 0.0
    )
    freshness_contract_valid = bool(
        freshness_value_valid and market_freshness_limit_valid and config_freshness_limit_valid
    )
    freshness_ok = bool(market_state.data_fresh and freshness_contract_valid)
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
        freshness_reason = "fresh" if freshness_ok else ("invalid_freshness_contract" if not freshness_contract_valid else "data_stale")
        trace.append(_rule_trace("data_freshness", _verdict_for_rule("data_freshness", freshness_ok, fail_verdict=cfg.freshness_fail_verdict), freshness_reason, score=None if market_state.freshness_secs is None else market_state.freshness_secs, details={"freshness_secs": market_state.freshness_secs, "limit_secs": market_state.freshness_limit_secs or cfg.freshness_limit_secs, "numeric_contract_valid": freshness_contract_valid}))
    if (not managing_existing_position) and (not freshness_ok):
        verdict = cfg.freshness_fail_verdict
        reason = "invalid_freshness_contract" if not freshness_contract_valid else "data_stale"
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
    session_bucket = str(market_state.session_bucket or "").strip().lower()
    raw_session_limit = (
        cfg.session_spread_overrides.get(session_bucket)
        if session_bucket and session_bucket in cfg.session_spread_overrides
        else cfg.max_spread_bps
    )
    spread_contract_valid = bool(
        _is_finite(market_state.spread_bps)
        and float(market_state.spread_bps) >= 0.0
        and _is_finite(raw_session_limit)
        and float(raw_session_limit) >= 0.0
        and _is_finite(market_state.allowed_spread_bps)
        and float(market_state.allowed_spread_bps) >= 0.0
    )
    session_limit = _session_spread_limit(cfg, market_state.session_bucket)
    effective_spread_limit = float(session_limit if session_limit > 0.0 else _safe_float(market_state.allowed_spread_bps, 0.0))
    spread_value = max(0.0, _safe_float(market_state.spread_bps, 0.0))
    spread_ok = bool(spread_contract_valid and (effective_spread_limit <= 0.0 or spread_value <= effective_spread_limit))
    if (not managing_existing_position) and (not spread_ok):
        verdict = cfg.spread_fail_verdict
        reason = "invalid_spread_contract" if not spread_contract_valid else "spread_too_wide"
    trace.append(_rule_trace("spread_session", "allow" if managing_existing_position else _verdict_for_rule("spread_session", spread_ok, fail_verdict=cfg.spread_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not spread_ok else "spread_ok"), details={"spread_bps": spread_value, "session_bucket": market_state.session_bucket, "limit_bps": float(effective_spread_limit), "numeric_contract_valid": spread_contract_valid}))
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
    exposure_limits_valid = bool(
        all(
            _is_finite(value) and float(value) >= 0.0
            for value in (
                cfg.max_gross_exposure,
                cfg.max_net_exposure,
                cfg.rollout_max_gross_exposure,
                cfg.rollout_max_net_exposure,
            )
        )
    )
    if (not managing_existing_position) and not exposure_values_finite:
        projected_gross_exposure = float(portfolio_gross_exposure)
        projected_net_exposure = float(portfolio_net_exposure)
        exposure_ok = False
        verdict = cfg.exposure_fail_verdict
        reason = "invalid_exposure_values"
    elif (not managing_existing_position) and not exposure_limits_valid:
        projected_gross_exposure = float(portfolio_gross_exposure)
        projected_net_exposure = float(portfolio_net_exposure)
        exposure_ok = False
        verdict = cfg.exposure_fail_verdict
        reason = "invalid_exposure_limits"
    elif (
        (not managing_existing_position)
        and float(candidate_entry_lots) > 0.0
        and (not exposure_math_safe)
        and (effective_gross_exposure_limit > 0.0 or effective_net_exposure_limit > 0.0)
    ):
        projected_gross_exposure = float(portfolio_gross_exposure)
        projected_net_exposure = float(portfolio_net_exposure)
        exposure_ok = False
        verdict = cfg.exposure_fail_verdict
        reason = "exposure_unit_mismatch"
    else:
        if effective_gross_exposure_limit > 0.0:
            projected_gross_exposure = float(portfolio_gross_exposure) + float(candidate_entry_lots if not managing_existing_position else 0.0)
            exposure_ok = exposure_ok and projected_gross_exposure <= float(effective_gross_exposure_limit)
        else:
            projected_gross_exposure = float(portfolio_gross_exposure)
        if effective_net_exposure_limit > 0.0:
            projected_net_exposure = float(portfolio_net_exposure) + float(candidate_entry_signed_lots if not managing_existing_position else 0.0)
            exposure_ok = exposure_ok and abs(projected_net_exposure) <= float(effective_net_exposure_limit)
        else:
            projected_net_exposure = float(portfolio_net_exposure)
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
    trace.append(
        _rule_trace(
            "exposure",
            "allow" if managing_existing_position else _verdict_for_rule("exposure", exposure_ok, fail_verdict=cfg.exposure_fail_verdict),
            "bypass_existing_position" if managing_existing_position else (reason if not exposure_ok else "exposure_ok"),
            details={
                "gross_exposure": float(portfolio_gross_exposure),
                "net_exposure": float(portfolio_net_exposure),
                "projected_gross_exposure": float(projected_gross_exposure),
                "projected_net_exposure": float(projected_net_exposure),
                "candidate_entry_lots": float(candidate_entry_lots),
                "exposure_unit": str(exposure_unit),
                "exposure_math_safe": bool(exposure_math_safe),
                "exposure_values_finite": bool(exposure_values_finite),
                "exposure_limits_valid": bool(exposure_limits_valid),
                "exposure_source": str(exposure_source),
                "candidate_entry_side": candidate_entry_side,
                "gross_limit": float(effective_gross_exposure_limit),
                "net_limit": float(effective_net_exposure_limit),
                "base_gross_limit": float(cfg.max_gross_exposure),
                "base_net_limit": float(cfg.max_net_exposure),
                "rollout_gross_limit": float(cfg.rollout_max_gross_exposure),
                "rollout_net_limit": float(cfg.rollout_max_net_exposure),
            },
        )
    )
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
    caps_contract_valid = bool(
        _is_finite(portfolio_state.open_position_count)
        and float(portfolio_state.open_position_count) >= 0.0
        and _is_finite(portfolio_state.pair_position_count)
        and float(portfolio_state.pair_position_count) >= 0.0
        and _is_finite(cfg.max_total_positions)
        and float(cfg.max_total_positions) >= 0.0
        and _is_finite(cfg.max_pair_positions)
        and float(cfg.max_pair_positions) >= 0.0
        and _is_finite(cfg.rollout_max_total_positions)
        and float(cfg.rollout_max_total_positions) >= 0.0
        and _is_finite(cfg.rollout_max_pair_positions)
        and float(cfg.rollout_max_pair_positions) >= 0.0
    )
    caps_ok = bool(caps_contract_valid)
    open_position_count = max(0, int(_safe_float(portfolio_state.open_position_count, 0.0)))
    pair_position_count = max(0, int(_safe_float(portfolio_state.pair_position_count, 0.0)))
    if effective_total_positions > 0:
        caps_ok = caps_ok and open_position_count < int(effective_total_positions)
    if effective_pair_positions > 0:
        caps_ok = caps_ok and pair_position_count < int(effective_pair_positions)
    if (not managing_existing_position) and (not caps_ok):
        verdict = "block"
        rollout_constrained = bool(
            rollout_pair_allowlisted
            and (
                (effective_total_positions > 0 and effective_total_positions != int(cfg.max_total_positions or 0))
                or (effective_pair_positions > 0 and effective_pair_positions != int(cfg.max_pair_positions or 0))
            )
        )
        reason = "invalid_position_counts" if not caps_contract_valid else ("rollout_position_caps" if rollout_constrained else "position_caps")
        rollout_breach = rollout_constrained
        rollout_breach_reason = reason if rollout_constrained else rollout_breach_reason
    trace.append(_rule_trace("position_caps", "allow" if managing_existing_position else _verdict_for_rule("position_caps", caps_ok, fail_verdict="block"), "bypass_existing_position" if managing_existing_position else (reason if not caps_ok else "caps_ok"), details={"open_position_count": open_position_count, "pair_position_count": pair_position_count, "max_total_positions": int(effective_total_positions), "max_pair_positions": int(effective_pair_positions), "base_max_total_positions": max(0, int(_safe_float(cfg.max_total_positions, 0.0))), "base_max_pair_positions": max(0, int(_safe_float(cfg.max_pair_positions, 0.0))), "rollout_max_total_positions": max(0, int(_safe_float(cfg.rollout_max_total_positions, 0.0))), "rollout_max_pair_positions": max(0, int(_safe_float(cfg.rollout_max_pair_positions, 0.0))), "numeric_contract_valid": caps_contract_valid}))
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
    drawdown_contract_valid = bool(
        _is_finite(portfolio_state.drawdown_pct)
        and float(portfolio_state.drawdown_pct) >= 0.0
        and _is_finite(cfg.max_drawdown_pct)
        and float(cfg.max_drawdown_pct) >= 0.0
    )
    drawdown_value = max(0.0, _safe_float(portfolio_state.drawdown_pct, 0.0))
    drawdown_limit = max(0.0, _safe_float(cfg.max_drawdown_pct, 0.0))
    drawdown_ok = bool(drawdown_contract_valid)
    if drawdown_contract_valid and drawdown_limit > 0.0:
        drawdown_ok = drawdown_value <= drawdown_limit
    if (not managing_existing_position) and (not drawdown_ok):
        verdict = cfg.drawdown_fail_verdict
        reason = "invalid_drawdown_contract" if not drawdown_contract_valid else "drawdown_limit"
    trace.append(_rule_trace("drawdown", "allow" if managing_existing_position else _verdict_for_rule("drawdown", drawdown_ok, fail_verdict=cfg.drawdown_fail_verdict), "bypass_existing_position" if managing_existing_position else (reason if not drawdown_ok else "drawdown_ok"), details={"drawdown_pct": drawdown_value, "max_drawdown_pct": drawdown_limit, "numeric_contract_valid": drawdown_contract_valid}))
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
        close_lots_raw = policy_intent.metadata.get("close_lots", 0.0)
        if lifecycle_action == "partial_tp" and _is_finite(close_lots_raw) and float(close_lots_raw) <= 0.0:
            lifecycle_action = "hold"
        if lifecycle_action == "exit":
            close_lots = max(0.0, _safe_float(close_lots_raw, 0.0))
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
    if approved_order is not None:
        order_numeric_errors = _approved_order_numeric_errors(approved_order)
        if order_numeric_errors:
            budget_plan = {
                **dict(budget_plan or {}),
                "rejection_reason": "invalid_approved_order_numeric_contract",
                "numeric_inputs_valid": False,
                "numeric_input_errors": order_numeric_errors,
            }
            approved_order = None
        elif lifecycle_action == "entry":
            final_entry_lots = float(approved_order.lots)
            post_builder_exposure_reason = ""
            if not exposure_values_finite:
                post_builder_exposure_reason = "invalid_exposure_values"
            elif not exposure_math_safe and (effective_gross_exposure_limit > 0.0 or effective_net_exposure_limit > 0.0):
                post_builder_exposure_reason = "exposure_unit_mismatch"
            elif effective_gross_exposure_limit > 0.0 and portfolio_gross_exposure + final_entry_lots > effective_gross_exposure_limit:
                post_builder_exposure_reason = "order_exposure_limit"
            elif effective_net_exposure_limit > 0.0:
                signed_lots = final_entry_lots if str(approved_order.side).upper() == "BUY" else -final_entry_lots
                if abs(portfolio_net_exposure + signed_lots) > effective_net_exposure_limit:
                    post_builder_exposure_reason = "order_exposure_limit"
            if post_builder_exposure_reason:
                budget_plan = {
                    **dict(budget_plan or {}),
                    "rejection_reason": post_builder_exposure_reason,
                    "post_builder_exposure_checked": True,
                }
                approved_order = None
    if approved_order is None and lifecycle_action == "hold":
        verdict = "hold"
        reason = "no_order_required"
    elif approved_order is None and lifecycle_action not in {"hold"}:
        verdict = "block"
        reason = str(budget_plan.get("rejection_reason") or "order_build_failed")
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
