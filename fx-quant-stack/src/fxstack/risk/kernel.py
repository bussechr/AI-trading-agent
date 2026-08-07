from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
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
from fxstack.risk.constants import (
    ROLLOUT_BUDGET_THROTTLED_MODES,
    ROLLOUT_EXECUTION_MODES,
)
from fxstack.risk.sizing import (
    STANDARD_LOT_UNITS,
    BrokerContractSpec,
    lots_for_broker_contract,
    lots_for_risk,
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
    require_entry_protection: bool = False
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


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _round_finite_lots(
    value: float, *, min_lot: float, lot_step: float, max_lot: float
) -> float:
    lots = max(0.0, value)
    if lots <= 0.0:
        return 0.0
    min_lot = max(0.0, min_lot)
    step = max(1e-9, lot_step)
    tolerance = max(1e-9, step / 10.0)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
    lots = math.floor((lots + tolerance) / step) * step
    if min_lot > 0.0 and lots + tolerance < min_lot:
        lots = min_lot
    max_lot = max(0.0, max_lot)
    if max_lot > 0.0:
        lots = min(max_lot, lots)
    if min_lot > 0.0 and lots + tolerance < min_lot:
        return 0.0
    return round(lots, 8)


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
        details=dict(details or {}),
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


def _broker_contract_sizing_required(intent: PolicyIntent) -> bool:
    return bool(intent.metadata.get("broker_contract_required", False))


def _broker_contract_entry_budget_plan(
    *,
    intent: PolicyIntent,
    portfolio: PortfolioState,
    config: RiskKernelConfig,
) -> dict[str, Any]:
    """Size an opted-in entry only from attested broker contract evidence."""

    metadata = dict(intent.metadata or {})
    target_raw = metadata.get("target_risk_pct", 0.0)
    requested_lots_raw = metadata.get(
        "requested_lots", metadata.get("planned_entry_lots", 0.0)
    )
    requested_target_risk_pct = _safe_float(target_raw, 0.0)
    ignored_requested_lots = _safe_float(requested_lots_raw, 0.0)
    budget_scale = _rollout_budget_scale(config)
    rollout_active = bool(
        str(config.rollout_mode or "").strip().lower() == "canary"
        and config.rollout_pair_allowlisted
    )
    effective_target_risk_pct = (
        float(requested_target_risk_pct)
        * float(budget_scale if rollout_active else 1.0)
        if requested_target_risk_pct > 0.0
        else 0.0
    )
    risk_fraction_for_sizing = (
        float(effective_target_risk_pct)
        if rollout_active
        else float(requested_target_risk_pct)
    )

    margin_key = "broker_contract_margin_utilization_cap"
    margin_cap_raw = metadata.get(margin_key, 0.25)
    margin_cap = _safe_float(
        margin_cap_raw,
        0.0 if margin_key in metadata else 0.25,
    )
    available_margin = _safe_float(
        metadata.get("broker_contract_available_margin"), 0.0
    )
    account_currency = str(
        metadata.get("broker_contract_account_currency") or ""
    ).strip().upper()
    operator_max_lots = _safe_float(config.max_lots, 0.0)

    numeric_errors: list[str] = []
    for name, value in {
        "target_risk_pct": target_raw,
        "action_score": intent.action_score,
        "expected_edge_bps": intent.expected_edge_bps,
        "confidence": intent.confidence,
        "max_lots": config.max_lots,
    }.items():
        if value is not None and not _is_finite(value):
            numeric_errors.append(f"nonfinite:{name}")
    if requested_target_risk_pct < 0.0:
        numeric_errors.append("out_of_range:target_risk_pct")
    for name, value in {
        "action_score": intent.action_score,
        "confidence": intent.confidence,
    }.items():
        if _is_finite(value) and not 0.0 <= float(value) <= 1.0:
            numeric_errors.append(f"out_of_range:{name}")
    if _is_finite(config.max_lots) and float(config.max_lots) < 0.0:
        numeric_errors.append("out_of_range:max_lots")
    for price_name in ("tp_price", "sl_price"):
        price = metadata.get(price_name)
        if price is not None and (not _is_finite(price) or float(price) <= 0.0):
            numeric_errors.append(f"invalid:{price_name}")
        if bool(config.require_entry_protection) and price is None:
            numeric_errors.append(f"missing:{price_name}")
    numeric_errors = sorted(set(numeric_errors))

    diagnostics: dict[str, Any] = {
        "required": True,
        "status": "refused",
        "reason": "",
        "symbol": str(intent.pair or "").strip().upper(),
        "broker_symbol": "",
        "account_currency": account_currency,
        "available_margin": float(available_margin),
        "margin_utilization_cap": float(margin_cap),
        "requested_risk_fraction": float(requested_target_risk_pct),
        "budgeted_risk_fraction": float(risk_fraction_for_sizing),
        "effective_risk_fraction": 0.0,
        "lots": 0.0,
        "money_at_risk": 0.0,
        "value_per_price_unit": 0.0,
        "margin_required": 0.0,
        "margin_capped": False,
        "broker_lot_size": 0.0,
        "broker_min_lot": 0.0,
        "broker_lot_step": 0.0,
        "broker_max_lot": 0.0,
        "broker_point": 0.0,
        "broker_stop_level_points": 0.0,
        "broker_margin_required_per_lot": 0.0,
        "operator_max_lots": float(operator_max_lots),
        "effective_max_lot": 0.0,
        "ignored_requested_lots": float(ignored_requested_lots),
        "state_errors": [],
    }
    base_plan: dict[str, Any] = {
        "source": "broker_contract_target_risk_pct",
        "sizing_source": "",
        "budget_scale": float(budget_scale if rollout_active else 1.0),
        "requested_target_risk_pct": float(requested_target_risk_pct),
        "effective_target_risk_pct": float(effective_target_risk_pct),
        "requested_lots": float(ignored_requested_lots),
        "raw_lots_requested": float(ignored_requested_lots),
        "raw_lots_effective": 0.0,
        "final_lots": 0.0,
        "reduced_budget": bool(
            rollout_active
            and requested_target_risk_pct > 0.0
            and float(budget_scale) < 1.0
        ),
        "rejection_reason": "",
        "risk_sizing_refusal": "",
        "numeric_inputs_valid": not numeric_errors,
        "numeric_input_errors": numeric_errors,
        "broker_contract_sizing": diagnostics,
    }

    def _refusal(reason: str) -> dict[str, Any]:
        refusal_reason = str(reason or "broker_contract_unsizeable")
        diagnostics["status"] = "refused"
        diagnostics["reason"] = refusal_reason
        return {
            **base_plan,
            "rejection_reason": refusal_reason,
            "risk_sizing_refusal": refusal_reason,
            "broker_contract_sizing": dict(diagnostics),
        }

    raw_state_errors = metadata.get("broker_contract_errors", [])
    if raw_state_errors is None:
        state_errors: list[str] = []
    elif isinstance(raw_state_errors, (list, tuple)):
        state_errors = [
            str(item).strip() for item in raw_state_errors if str(item).strip()
        ]
    else:
        return _refusal("broker_contract_errors_malformed")
    diagnostics["state_errors"] = state_errors
    if state_errors:
        return _refusal(state_errors[0])

    if "broker_contract_spec" not in metadata or metadata.get(
        "broker_contract_spec"
    ) is None:
        return _refusal("broker_contract_spec_missing")
    raw_contract = metadata.get("broker_contract_spec")
    if isinstance(raw_contract, BrokerContractSpec):
        contract = raw_contract
    elif isinstance(raw_contract, Mapping):
        try:
            contract = BrokerContractSpec.from_mapping(
                symbol=str(intent.pair), payload=raw_contract
            )
        except (TypeError, ValueError, OverflowError):
            return _refusal("broker_contract_spec_malformed")
    else:
        return _refusal("broker_contract_spec_malformed")

    diagnostics.update(
        {
            "broker_symbol": str(contract.broker_symbol),
            "broker_lot_size": float(contract.lot_size),
            "broker_min_lot": float(contract.min_lot),
            "broker_lot_step": float(contract.lot_step),
            "broker_max_lot": float(contract.max_lot),
            "broker_point": float(contract.point),
            "broker_stop_level_points": float(contract.stop_level_points),
            "broker_margin_required_per_lot": float(contract.margin_required),
            "effective_max_lot": float(contract.max_lot),
        }
    )
    contract_error = contract.validation_error(expected_symbol=intent.pair)
    if contract_error:
        return _refusal(contract_error)

    effective_contract = contract
    if operator_max_lots > 0.0 and operator_max_lots < contract.max_lot:
        stepped_operator_max = (
            math.floor(
                (operator_max_lots + contract.lot_step * 1e-9)
                / contract.lot_step
            )
            * contract.lot_step
        )
        if stepped_operator_max + contract.lot_step * 1e-9 < contract.min_lot:
            diagnostics["effective_max_lot"] = float(stepped_operator_max)
            return _refusal("broker_contract_operator_max_below_min_lot")
        effective_contract = replace(
            contract,
            max_lot=round(float(stepped_operator_max), 8),
        )
        diagnostics["effective_max_lot"] = float(effective_contract.max_lot)

    if numeric_errors:
        return _refusal("invalid_order_numeric_contract")
    if len(account_currency) != 3 or not account_currency.isalpha():
        return _refusal("broker_contract_account_currency_unattested")
    if rollout_active and risk_fraction_for_sizing <= 0.0:
        return _refusal("rollout_budget_scale_zero")

    raw_quote_rates = metadata.get("quote_rates", {})
    quote_rates = dict(raw_quote_rates) if isinstance(raw_quote_rates, Mapping) else {}
    sized = lots_for_broker_contract(
        pair=str(intent.pair),
        equity=_safe_float(portfolio.equity, 0.0),
        available_margin=available_margin,
        risk_fraction=risk_fraction_for_sizing,
        entry_price=_safe_float(metadata.get("entry_price"), 0.0),
        stop_price=_safe_float(metadata.get("sl_price"), 0.0),
        contract=effective_contract,
        rates=quote_rates,
        account_currency=account_currency,
        margin_utilization_cap=margin_cap,
    )
    diagnostics.update(
        {
            "requested_risk_fraction": float(sized.requested_risk_fraction),
            "effective_risk_fraction": float(sized.effective_risk_fraction),
            "lots": float(sized.lots),
            "money_at_risk": float(sized.money_at_risk),
            "value_per_price_unit": float(sized.value_per_price_unit),
            "margin_required": float(sized.margin_required),
            "margin_capped": bool(sized.margin_capped),
        }
    )
    if not sized.ok:
        return _refusal(sized.reason)

    diagnostics["status"] = "approved"
    diagnostics["reason"] = ""
    return {
        **base_plan,
        "sizing_source": "broker_contract",
        "raw_lots_effective": float(sized.lots),
        "final_lots": float(sized.lots),
        "rejection_reason": "",
        "risk_sizing_refusal": "",
        "broker_contract_sizing": dict(diagnostics),
    }


def _entry_budget_plan(*, intent: PolicyIntent, portfolio: PortfolioState, config: RiskKernelConfig) -> dict[str, Any]:
    if _broker_contract_sizing_required(intent):
        return _broker_contract_entry_budget_plan(
            intent=intent,
            portfolio=portfolio,
            config=config,
        )

    target_raw = intent.metadata.get("target_risk_pct", 0.0)
    lots_raw = intent.metadata.get("requested_lots", intent.metadata.get("planned_entry_lots", 0.0))
    requested_lots_number = _finite_number(lots_raw)
    requested_target_number = _finite_number(target_raw)
    action_score = _finite_number(intent.action_score)
    expected_edge_bps = _finite_number(intent.expected_edge_bps)
    confidence = _finite_number(intent.confidence)
    min_lots = _finite_number(config.min_lots)
    lot_step = _finite_number(config.lot_step)
    max_lots = _finite_number(config.max_lots)
    numeric_errors: list[str] = []
    for name, value, number in (
        ("requested_lots", lots_raw, requested_lots_number),
        ("target_risk_pct", target_raw, requested_target_number),
        ("action_score", intent.action_score, action_score),
        ("expected_edge_bps", intent.expected_edge_bps, expected_edge_bps),
        ("confidence", intent.confidence, confidence),
        ("min_lots", config.min_lots, min_lots),
        ("lot_step", config.lot_step, lot_step),
        ("max_lots", config.max_lots, max_lots),
    ):
        if value is not None and number is None:
            numeric_errors.append(f"nonfinite:{name}")
    requested_lots = 0.0 if requested_lots_number is None else requested_lots_number
    requested_target_risk_pct = (
        0.0 if requested_target_number is None else requested_target_number
    )
    for name, number in (
        ("requested_lots", requested_lots_number),
        ("target_risk_pct", requested_target_number),
    ):
        if number is not None and number < 0.0:
            numeric_errors.append(f"out_of_range:{name}")
    for name, number in (("action_score", action_score), ("confidence", confidence)):
        if number is not None and not 0.0 <= number <= 1.0:
            numeric_errors.append(f"out_of_range:{name}")
    if min_lots is not None and min_lots < 0.0:
        numeric_errors.append("out_of_range:min_lots")
    if lot_step is not None and lot_step <= 0.0:
        numeric_errors.append("out_of_range:lot_step")
    if max_lots is not None and max_lots < 0.0:
        numeric_errors.append("out_of_range:max_lots")
    tp_price_raw = intent.metadata.get("tp_price")
    sl_price_raw = intent.metadata.get("sl_price")
    tp_price = _finite_number(tp_price_raw)
    sl_price = _finite_number(sl_price_raw)
    for price_name, price, price_number in (
        ("tp_price", tp_price_raw, tp_price),
        ("sl_price", sl_price_raw, sl_price),
    ):
        if price is not None and (price_number is None or price_number <= 0.0):
            numeric_errors.append(f"invalid:{price_name}")
        if bool(config.require_entry_protection) and price is None:
            numeric_errors.append(f"missing:{price_name}")
    numeric_errors = sorted(set(numeric_errors))
    budget_scale = _rollout_budget_scale(config)
    rollout_active = bool(str(config.rollout_mode or "").strip().lower() == "canary" and config.rollout_pair_allowlisted)
    source = "target_risk_pct" if requested_target_risk_pct > 0.0 else "requested_lots"
    raw_lots_requested = requested_lots
    effective_target_risk_pct = (
        requested_target_risk_pct * (budget_scale if rollout_active else 1.0)
        if requested_target_risk_pct > 0.0
        else 0.0
    )
    raw_lots_effective = requested_lots * (budget_scale if rollout_active else 1.0)
    final_lots = _round_finite_lots(
        raw_lots_effective,
        min_lot=0.0 if min_lots is None else min_lots,
        lot_step=0.01 if lot_step is None else lot_step,
        max_lot=0.0 if max_lots is None else max_lots,
    )
    # Risk-based sizing, natively. Previously a caller that supplied
    # ``target_risk_pct`` without also supplying lots was rejected outright with
    # ``target_risk_pct_requires_custom_order_builder``, and the only escape --
    # ``config.order_builder`` -- was declared and never assigned. So the one
    # code path architected for risk-based sizing was unreachable, every order
    # carried ``risk_budget_pct = 0.0``, and lots came from ``equity * 1e-5``
    # with no knowledge of the stop. That made stop width scale money-at-risk
    # linearly, which is why the bracket geometry could not safely be changed.
    #
    # ``config.order_builder`` is the wrong hook for this: the kernel consults it
    # for EVERY lifecycle action (hold/exit/tighten_stop/partial_tp), so an
    # entry-only builder there would break position exits. Sizing therefore lives
    # here, in the entry branch, where it cannot affect lifecycle handling.
    sizing_source = ""
    risk_sizing_refusal = ""
    if not numeric_errors and requested_target_risk_pct > 0.0 and requested_lots <= 0.0:
        stop_distance = abs(_finite_number(intent.metadata.get("stop_distance")) or 0.0)
        if stop_distance <= 0.0:
            entry_px = _finite_number(intent.metadata.get("entry_price")) or 0.0
            sl_px = sl_price or 0.0
            if entry_px > 0.0 and sl_px > 0.0:
                stop_distance = abs(entry_px - sl_px)
        equity = _finite_number(portfolio.equity) or 0.0
        # Explicit selection, never a falsy-or: an active canary rollout with
        # budget_scale exactly 0.0 must refuse, not silently restore the FULL
        # unscaled fraction (`0.0 or requested` did exactly that).
        risk_fraction_for_sizing = (
            effective_target_risk_pct if rollout_active else requested_target_risk_pct
        )
        if rollout_active and risk_fraction_for_sizing <= 0.0:
            risk_sizing_refusal = "rollout_budget_scale_zero"
        elif stop_distance > 0.0 and equity > 0.0:
            value_per_price_unit = _finite_number(
                intent.metadata.get("value_per_price_unit")
            )
            sized = lots_for_risk(
                equity=equity,
                risk_fraction=risk_fraction_for_sizing,
                stop_distance_price=stop_distance,
                value_per_price_unit=(
                    STANDARD_LOT_UNITS
                    if value_per_price_unit is None
                    else value_per_price_unit
                ),
                min_lots=0.01 if min_lots is None else min_lots,
                lot_step=0.01 if lot_step is None else lot_step,
                max_lots=0.0 if max_lots is None else max_lots,
            )
            if sized.lots > 0.0:
                raw_lots_effective = float(sized.lots)
                final_lots = float(sized.lots)
                sizing_source = "target_risk_pct_native"
            else:
                risk_sizing_refusal = str(sized.reason or "")

    rejection_reason = "invalid_order_numeric_contract" if numeric_errors else ""
    if not rejection_reason and requested_target_risk_pct > 0.0 and requested_lots <= 0.0 and not sizing_source:
        if risk_sizing_refusal == "rollout_budget_scale_zero":
            rejection_reason = "rollout_budget_scale_zero"
        elif risk_sizing_refusal.startswith("risk_budget_below_min_lot"):
            # The stop and equity WERE derivable; the composed risk budget
            # (base x Kelly x drawdown x portfolio scale) simply rounds below
            # the broker's minimum lot. Distinct from the missing-input case so
            # telemetry can tell "entry inexpressible at this lot quantum" apart
            # from "no signal" and from "cannot state risk" -- the audit found
            # these were conflated and invisible in cycle telemetry.
            rejection_reason = "entry_risk_budget_below_min_lot_quantum"
        else:
            # Still unsizeable: no stop distance or no equity means risk cannot
            # be stated, and sizing without a stated risk is the defect being
            # removed.
            rejection_reason = "target_risk_pct_unsizeable_missing_stop_or_equity"
    elif (
        not rejection_reason
        and requested_lots > 0.0
        and rollout_active
        and budget_scale <= 0.0
    ):
        # Legacy lot path under a zero rollout scale: the scaled request is 0
        # lots, which previously fell through with NO rejection (a 0-lot
        # "approval"). Same silent-zero class, named explicitly.
        rejection_reason = "rollout_budget_scale_zero"
    elif not rejection_reason and raw_lots_effective > 0.0 and final_lots <= 0.0:
        rejection_reason = "requested_lots_below_min_lot"
    return {
        "source": str(source),
        "sizing_source": str(sizing_source),
        "budget_scale": budget_scale if rollout_active else 1.0,
        "requested_target_risk_pct": requested_target_risk_pct,
        "effective_target_risk_pct": effective_target_risk_pct,
        "requested_lots": requested_lots,
        "raw_lots_requested": raw_lots_requested,
        "raw_lots_effective": raw_lots_effective,
        "final_lots": final_lots,
        "reduced_budget": bool(rollout_active and raw_lots_effective + 1e-12 < raw_lots_requested),
        "rejection_reason": str(rejection_reason),
        "risk_sizing_refusal": str(risk_sizing_refusal),
        "numeric_inputs_valid": not numeric_errors,
        "numeric_input_errors": numeric_errors,
    }


def _approved_order_numeric_errors(order: ApprovedOrderIntent) -> list[str]:
    errors: list[str] = []
    lots = _finite_number(order.lots)
    close_lots = _finite_number(order.close_lots)
    action_score = _finite_number(order.action_score)
    risk_budget_pct = _finite_number(order.risk_budget_pct)
    for name, value, number in (
        ("lots", order.lots, lots),
        ("close_lots", order.close_lots, close_lots),
        ("action_score", order.action_score, action_score),
        ("risk_budget_pct", order.risk_budget_pct, risk_budget_pct),
    ):
        if number is None:
            errors.append(f"nonfinite:{name}")
    for name, number in (
        ("lots", lots),
        ("close_lots", close_lots),
        ("risk_budget_pct", risk_budget_pct),
    ):
        if number is not None and number < 0.0:
            errors.append(f"out_of_range:{name}")
    if action_score is not None and not 0.0 <= action_score <= 1.0:
        errors.append("out_of_range:action_score")
    for name, value in (("tp_price", order.tp_price), ("sl_price", order.sl_price)):
        number = _finite_number(value)
        if value is not None and (number is None or number <= 0.0):
            errors.append(f"invalid:{name}")
    command = str(order.command or "").strip().upper()
    if command in {"BUY", "SELL", "CLOSE_PARTIAL"} and lots is not None and lots <= 0.0:
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
    entry_budget_plan: dict[str, Any] | None = None,
) -> tuple[ApprovedOrderIntent | None, dict[str, Any]]:
    builder = config.order_builder
    required_broker_entry = bool(
        lifecycle_action == "entry" and _broker_contract_sizing_required(intent)
    )
    if builder is not None and not required_broker_entry:
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

    budget_plan = (
        entry_budget_plan
        if entry_budget_plan is not None
        else _entry_budget_plan(intent=intent, portfolio=portfolio, config=config)
    )
    rejection_reason = str(budget_plan.get("rejection_reason") or "")
    if rejection_reason:
        return None, budget_plan
    final_lots = float(budget_plan.get("final_lots", 0.0))
    if final_lots <= 0.0:
        return None, budget_plan
    order_metadata = dict(intent.metadata or {})
    broker_sizing = budget_plan.get("broker_contract_sizing")
    if isinstance(broker_sizing, Mapping):
        order_metadata["broker_contract_sizing"] = dict(broker_sizing)
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
            metadata=order_metadata,
        ),
        budget_plan,
    )


def _evaluate_risk_decision(
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
    # Execution permission and budget throttling are separate questions.
    # ``canary`` answers yes to both; ``live`` answers yes only to the first.
    rollout_configured = bool(rollout_mode in ROLLOUT_EXECUTION_MODES)
    rollout_budget_throttled = bool(rollout_mode in ROLLOUT_BUDGET_THROTTLED_MODES)
    rollout_pair_allowlisted = bool(cfg.rollout_pair_allowlisted)
    rollout_budget_scale = _rollout_budget_scale(cfg) if rollout_budget_throttled else 1.0
    effective_gross_exposure_limit = _effective_positive_limit(cfg.max_gross_exposure, cfg.rollout_max_gross_exposure if rollout_pair_allowlisted else 0.0)
    effective_net_exposure_limit = _effective_positive_limit(cfg.max_net_exposure, cfg.rollout_max_net_exposure if rollout_pair_allowlisted else 0.0)
    effective_total_positions = _effective_positive_int_limit(cfg.max_total_positions, cfg.rollout_max_total_positions if rollout_pair_allowlisted else 0)
    effective_pair_positions = _effective_positive_int_limit(cfg.max_pair_positions, cfg.rollout_max_pair_positions if rollout_pair_allowlisted else 0)
    candidate_entry_side = str(policy_intent.side).upper()
    (
        portfolio_gross_exposure,
        portfolio_net_exposure,
        exposure_unit,
        exposure_math_safe,
        exposure_values_finite,
        exposure_source,
    ) = _portfolio_exposure_state(portfolio_state)
    entry_budget_config = cfg
    sensible_lot_cap = 0.0
    sensible_lot_cap_sources: list[str] = []
    if (
        not managing_existing_position
        and _broker_contract_sizing_required(policy_intent)
        and exposure_math_safe
        and exposure_values_finite
        and candidate_entry_side in {"BUY", "SELL"}
    ):
        lot_caps: list[tuple[str, float]] = []
        if _safe_float(cfg.max_lots, 0.0) > 0.0:
            lot_caps.append(("operator", _safe_float(cfg.max_lots, 0.0)))
        if effective_gross_exposure_limit > 0.0:
            lot_caps.append(
                (
                    "gross_exposure",
                    max(
                        0.0,
                        float(effective_gross_exposure_limit)
                        - float(portfolio_gross_exposure),
                    ),
                )
            )
        if effective_net_exposure_limit > 0.0:
            directional_headroom = (
                float(effective_net_exposure_limit)
                - float(portfolio_net_exposure)
                if candidate_entry_side == "BUY"
                else float(effective_net_exposure_limit)
                + float(portfolio_net_exposure)
            )
            lot_caps.append(("net_exposure", max(0.0, directional_headroom)))
        if lot_caps:
            sensible_lot_cap = min(value for _, value in lot_caps)
            sensible_lot_cap_sources = [
                source
                for source, value in lot_caps
                if math.isclose(value, sensible_lot_cap, rel_tol=1e-12, abs_tol=1e-12)
            ]
            # A zero headroom must remain a positive sub-quantum cap here;
            # max_lots=0 means "no operator ceiling" to the sizing adapter.
            entry_budget_config = replace(
                cfg,
                max_lots=max(
                    sensible_lot_cap,
                    min(max(_safe_float(cfg.min_lots, 0.01), 1e-9) / 2.0, 1e-6),
                ),
            )
    rollout_budget_plan = _entry_budget_plan(
        intent=policy_intent,
        portfolio=portfolio_state,
        config=entry_budget_config,
    )
    if sensible_lot_cap_sources:
        rollout_budget_plan = {
            **rollout_budget_plan,
            "sensible_lot_cap": float(sensible_lot_cap),
            "sensible_lot_cap_sources": list(sensible_lot_cap_sources),
        }
    rollout_reduced_budget = bool(rollout_budget_throttled and rollout_pair_allowlisted and rollout_budget_plan.get("reduced_budget", False))
    rollout_breach = False
    rollout_breach_reason = ""
    candidate_entry_lots = float(rollout_budget_plan.get("final_lots", 0.0))
    candidate_entry_signed_lots = candidate_entry_lots if candidate_entry_side == "BUY" else (-candidate_entry_lots if candidate_entry_side == "SELL" else 0.0)

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
            "budget_scale": float(rollout_budget_scale if rollout_budget_throttled and rollout_pair_allowlisted else 1.0),
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

    # 7. Rollout (canary AND live -- both are active rollouts).
    #
    # This block previously ran for canary only, which meant the allowlist below
    # was NOT enforced for pairs in live mode. Widening it tightens that hole.
    if rollout_configured:
        rollout_changed = bool(
            rollout_reduced_budget
            or (effective_total_positions > 0 and effective_total_positions != int(cfg.max_total_positions or 0))
            or (effective_pair_positions > 0 and effective_pair_positions != int(cfg.max_pair_positions or 0))
            or (effective_gross_exposure_limit > 0.0 and effective_gross_exposure_limit != float(cfg.max_gross_exposure or 0.0))
            or (effective_net_exposure_limit > 0.0 and effective_net_exposure_limit != float(cfg.max_net_exposure or 0.0))
        )
        rollout_reason = (
            f"{rollout_mode}_budget_reduced" if rollout_changed else f"{rollout_mode}_ok"
        )
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
        config=entry_budget_config,
        lifecycle_action=lifecycle_action,
        close_lots=close_lots,
        entry_budget_plan=rollout_budget_plan,
    )
    if sensible_lot_cap_sources and lifecycle_action == "entry":
        budget_plan = {
            **dict(budget_plan or {}),
            "sensible_lot_cap": float(sensible_lot_cap),
            "sensible_lot_cap_sources": list(sensible_lot_cap_sources),
        }
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
    sizing_details = {
        key: value
        for key in (
            "source",
            "sizing_source",
            "risk_sizing_refusal",
            "numeric_input_errors",
            "sensible_lot_cap",
            "sensible_lot_cap_sources",
            "post_builder_exposure_checked",
        )
        if (value := budget_plan.get(key))
    }
    if budget_plan.get("numeric_inputs_valid") is False:
        sizing_details["numeric_inputs_valid"] = False
    trace.append(
        _rule_trace(
            "final_sizing_order",
            "allow" if approved_order is not None else ("hold" if lifecycle_action == "hold" else "block"),
            reason,
            details=sizing_details,
        )
    )

    if rollout_budget_throttled and rollout_pair_allowlisted and lifecycle_action == "entry" and bool(budget_plan.get("reduced_budget", False)):
        rollout_breach = True
        if not rollout_breach_reason:
            rollout_breach_reason = "rollout_budget_reduced"

    decision_metadata: dict[str, Any] = {
        "command": approved_order.command if approved_order is not None else "",
        "final_lots": float(approved_order.lots) if approved_order is not None else 0.0,
        "close_lots": float(approved_order.close_lots) if approved_order is not None else float(close_lots),
        "rollout": _rollout_metadata(
            final_lots=float(approved_order.lots) if approved_order is not None else 0.0,
            effective_target_risk_pct=float(
                budget_plan.get(
                    "effective_target_risk_pct",
                    rollout_budget_plan.get("effective_target_risk_pct", 0.0),
                )
            ),
            raw_lots_effective=float(
                budget_plan.get(
                    "raw_lots_effective",
                    rollout_budget_plan.get("raw_lots_effective", 0.0),
                )
            ),
            reduced_budget=bool(
                budget_plan.get("reduced_budget", rollout_reduced_budget)
            ),
        ),
    }
    broker_sizing = budget_plan.get("broker_contract_sizing")
    if isinstance(broker_sizing, Mapping):
        decision_metadata["broker_contract_sizing"] = dict(broker_sizing)

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
        metadata=decision_metadata,
    )


def evaluate_risk_decision(
    *,
    policy_intent: PolicyIntent,
    market_state: MarketState,
    portfolio_state: PortfolioState,
    config: RiskKernelConfig | None = None,
) -> RiskDecision:
    """Evaluate the canonical kernel and mark its owned trace details trusted."""

    decision = _evaluate_risk_decision(
        policy_intent=policy_intent,
        market_state=market_state,
        portfolio_state=portfolio_state,
        config=config,
    )
    decision._trusted_trace_details = True
    return decision
