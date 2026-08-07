# AGENT: ROLE: Pure broker-grid and worst-fill boundary for production scalp entries.
# AGENT: ENTRYPOINT: `build_scalp_broker_entry_plan`.
# AGENT: PRIMARY INPUTS: fresh adverse-side quote, qualified payoff, live broker contract.
# AGENT: PRIMARY OUTPUTS: immutable worst-fill/bracket plan or fail-closed reasons.
# AGENT: STATE / SIDE EFFECTS: none; never persists, polls, or executes a trade.
"""Bind a production scalp entry to one conservative MT4 price envelope.

The MT4 BUY or SELL trade executes at a later instant than strategy
qualification.  It therefore cannot be sized from the current quote and then
executed with an unbounded re-price.  This module moves the bracket to the worst
fill the strategy is willing to accept, rounds every price onto the broker's
actual trade-tick grid, and recomputes the payoff before the canonical risk
kernel sees the trade.  Any actual fill within the resulting bound has no more
stop risk and no less target distance than the proof used for sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
import math
from typing import Any, Mapping

from fxstack._serialization import flat_dataclass_dict
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.risk.sizing import BrokerContractSpec
from fxstack.strategy.mtvclc import FIXED_ADVERSE_EXECUTION_DEBIT_BPS


SCALP_BROKER_ENTRY_PLAN_SCHEMA = "fxstack.production_scalp_broker_entry_plan.v2"
SCALP_BROKER_ENTRY_COST_MODEL_MTVCLC = "fxstack.production_scalp_cost.mtvclc.v1"
PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS = 20
PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS = 5
_SIDES = {"BUY", "SELL"}


@dataclass(frozen=True, slots=True)
class ScalpBrokerEntryCostModel:
    """Explicit economics used to re-prove one broker-grid entry.

    Production callers supply the frozen MTVCLC spread ceiling and non-spread
    inputs through :meth:`mtvclc` so the boundary can recompute its exact
    payoff after broker-grid widening. There is no implicit or spread-only
    production cost lane.
    """

    cost_model_id: str
    commission_bps_per_round_trip: float = 0.0
    financing_bps_per_trade: float = 0.0
    adverse_execution_debit_bps: float = FIXED_ADVERSE_EXECUTION_DEBIT_BPS
    convert_on_close_charge_fraction: float = 0.0
    p90_spread_bps: float | None = None

    @classmethod
    def mtvclc(
        cls,
        *,
        p90_spread_bps: float,
        commission_bps_per_round_trip: float,
        financing_bps_per_trade: float,
        convert_on_close_charge_fraction: float,
    ) -> ScalpBrokerEntryCostModel:
        """Build the exact MTVCLC cost contract, including its fixed debit."""

        return cls(
            cost_model_id=SCALP_BROKER_ENTRY_COST_MODEL_MTVCLC,
            p90_spread_bps=p90_spread_bps,
            commission_bps_per_round_trip=commission_bps_per_round_trip,
            financing_bps_per_trade=financing_bps_per_trade,
            adverse_execution_debit_bps=FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
            convert_on_close_charge_fraction=(convert_on_close_charge_fraction),
        )


@dataclass(frozen=True, slots=True)
class ScalpBrokerEntryPlan:
    """One hash-bindable price plan passed to risk, queue, and MT4."""

    execution_type: str
    pending_orders_forbidden: bool
    entry_deadline_epoch: int
    symbol: str
    broker_symbol: str
    side: str
    quote_entry_price: float
    worst_fill_price: float
    sl_price: float
    tp_price: float
    max_slippage_points: int
    effective_slippage_points: float
    protection_cushion_points: int
    point: float
    tick_size: float
    digits: int
    reference_mid: float
    stop_distance_price: float
    target_distance_price: float
    stop_bps: float
    target_bps: float
    win_probability_lower_bound: float
    cost_model_id: str
    current_spread_bps: float
    p90_spread_bps: float | None
    commission_bps_per_round_trip: float
    financing_bps_per_trade: float
    adverse_execution_debit_bps: float
    fixed_non_spread_cost_bps: float
    current_total_cost_bps: float
    convert_on_close_charge_fraction: float
    live_p_star: float
    conservative_expected_edge_bps: float
    reward_risk_ratio: float
    schema_version: str = SCALP_BROKER_ENTRY_PLAN_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return flat_dataclass_dict(self)

    def command_fields(self) -> dict[str, Any]:
        """Return the fields whose values must survive wire serialization."""

        return {
            "broker_entry_plan_schema": str(self.schema_version),
            # BUY/SELL means an immediate OP_BUY/OP_SELL market trade. This is
            # explicit so no downstream adapter can reinterpret the plan as a
            # pending stop or limit trade.
            "execution_type": str(self.execution_type),
            "pending_orders_forbidden": bool(self.pending_orders_forbidden),
            "entry_deadline_epoch": int(self.entry_deadline_epoch),
            # The sizing proof deliberately uses the worst permitted fill.
            "entry_price": float(self.worst_fill_price),
            "entry_quote_price": float(self.quote_entry_price),
            "worst_fill_price": float(self.worst_fill_price),
            "max_slippage_points": int(self.max_slippage_points),
            "effective_slippage_points": float(self.effective_slippage_points),
            "protection_cushion_points": int(self.protection_cushion_points),
            "sl_price": float(self.sl_price),
            "tp_price": float(self.tp_price),
            "reference_mid": float(self.reference_mid),
            "stop_distance_price": float(self.stop_distance_price),
            "target_distance_price": float(self.target_distance_price),
            "stop_bps": float(self.stop_bps),
            "target_bps": float(self.target_bps),
            "win_probability_lower_bound": float(self.win_probability_lower_bound),
            "cost_model_id": str(self.cost_model_id),
            "current_spread_bps": float(self.current_spread_bps),
            "p90_spread_bps": (
                None if self.p90_spread_bps is None else float(self.p90_spread_bps)
            ),
            "commission_bps_per_round_trip": float(self.commission_bps_per_round_trip),
            "financing_bps_per_trade": float(self.financing_bps_per_trade),
            "adverse_execution_debit_bps": float(self.adverse_execution_debit_bps),
            "fixed_non_spread_cost_bps": float(self.fixed_non_spread_cost_bps),
            "current_total_cost_bps": float(self.current_total_cost_bps),
            "convert_on_close_charge_fraction": float(
                self.convert_on_close_charge_fraction
            ),
            "live_p_star": float(self.live_p_star),
            "conservative_expected_edge_bps": float(
                self.conservative_expected_edge_bps
            ),
            "reward_risk_ratio": float(self.reward_risk_ratio),
            "broker_entry_plan": self.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ScalpBrokerEntryPlanResult:
    plan: ScalpBrokerEntryPlan | None
    reasons: tuple[str, ...]

    @property
    def accepted(self) -> bool:
        return self.plan is not None and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": bool(self.accepted),
            "reasons": list(self.reasons),
            "plan": None if self.plan is None else self.plan.to_dict(),
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0.0 else None


def _normalized_cost_model(
    value: ScalpBrokerEntryCostModel,
) -> tuple[ScalpBrokerEntryCostModel | None, str]:
    if not isinstance(value, ScalpBrokerEntryCostModel):
        return None, "scalp_broker_entry_cost_model_invalid"
    model = value
    model_id = str(model.cost_model_id or "").strip()
    if model_id != SCALP_BROKER_ENTRY_COST_MODEL_MTVCLC:
        return None, "scalp_broker_entry_cost_model_invalid"
    commission = _finite(model.commission_bps_per_round_trip)
    financing = _finite(model.financing_bps_per_trade)
    adverse_debit = _finite(model.adverse_execution_debit_bps)
    conversion = _finite(model.convert_on_close_charge_fraction)
    if (
        commission is None
        or commission < 0.0
        or financing is None
        or financing < 0.0
        or adverse_debit is None
        or adverse_debit < 0.0
        or conversion is None
        or not 0.0 <= conversion < 1.0
    ):
        return None, "scalp_broker_entry_cost_model_invalid"
    p90_spread = _positive(model.p90_spread_bps)
    if p90_spread is None:
        return None, "scalp_broker_entry_cost_model_invalid"
    normalized = ScalpBrokerEntryCostModel(
        cost_model_id=model_id,
        commission_bps_per_round_trip=float(commission),
        financing_bps_per_trade=float(financing),
        adverse_execution_debit_bps=float(adverse_debit),
        convert_on_close_charge_fraction=float(conversion),
        p90_spread_bps=p90_spread,
    )
    return normalized, ""


def production_scalp_entry_deadline_epoch(
    payload: Mapping[str, Any] | None,
) -> int | None:
    """Return the strict wire-safe deadline bound to one scalp entry."""

    raw = dict(payload or {})
    value = raw.get("entry_deadline_epoch")
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        not math.isfinite(number)
        or number <= 0.0
        or not number.is_integer()
        or number > 2_147_483_647
    ):
        return None
    return int(number)


def production_scalp_immediate_entry_contract_error(
    payload: Mapping[str, Any] | None,
    *,
    now_epoch: float | None = None,
) -> str:
    """Validate the immutable instant-entry type, pending fence, and deadline."""

    raw = dict(payload or {})
    if str(raw.get("execution_type") or "").strip().lower() != "market":
        return "scalp_market_entry_execution_type_invalid"
    if raw.get("pending_orders_forbidden") is not True:
        return "scalp_market_entry_pending_orders_not_forbidden"
    deadline = production_scalp_entry_deadline_epoch(raw)
    if deadline is None:
        return "scalp_market_entry_deadline_invalid"
    if now_epoch is not None:
        now = _finite(now_epoch)
        if now is None or now <= 0.0:
            return "scalp_market_entry_server_clock_invalid"
        if deadline <= now:
            return "scalp_market_entry_deadline_expired"

    plan = raw.get("broker_entry_plan")
    if isinstance(plan, Mapping):
        if str(plan.get("execution_type") or "").strip().lower() != "market":
            return "scalp_market_entry_plan_execution_type_mismatch"
        if plan.get("pending_orders_forbidden") is not True:
            return "scalp_market_entry_plan_pending_orders_forbidden_mismatch"
        plan_deadline = production_scalp_entry_deadline_epoch(plan)
        if plan_deadline != deadline:
            return "scalp_market_entry_plan_entry_deadline_epoch_mismatch"
    return ""


def _grid_price(
    value: Decimal | float,
    *,
    tick_size: float,
    digits: int,
    toward_positive_infinity: bool,
) -> float | None:
    """Round a positive price to an exact tick without binary-float drift."""

    try:
        raw = Decimal(str(value))
        step = Decimal(str(tick_size))
        quantum = Decimal(1).scaleb(-int(digits))
        if not raw.is_finite() or not step.is_finite() or step <= 0:
            return None
        rounding = ROUND_CEILING if toward_positive_infinity else ROUND_FLOOR
        units = (raw / step).to_integral_value(rounding=rounding)
        rounded = (units * step).quantize(quantum)
    except (InvalidOperation, ValueError, OverflowError):
        return None
    result = float(rounded)
    return result if math.isfinite(result) and result > 0.0 else None


def _refusal(*reasons: str) -> ScalpBrokerEntryPlanResult:
    return ScalpBrokerEntryPlanResult(
        plan=None,
        reasons=tuple(dict.fromkeys(reason for reason in reasons if reason)),
    )


def _payload_number(payload: Mapping[str, Any], field: str) -> float | None:
    return _finite(payload.get(field))


def _on_tick_grid(price: float, *, tick_size: float) -> bool:
    steps = price / tick_size
    return math.isfinite(steps) and math.isclose(
        steps,
        round(steps),
        rel_tol=0.0,
        abs_tol=1e-7,
    )


def production_scalp_market_entry_envelope_error(
    payload: Mapping[str, Any] | None,
    *,
    symbol: str,
    side: str,
    contract: BrokerContractSpec,
    current_bid: float,
    current_ask: float,
    now_epoch: float | None = None,
) -> str:
    """Recheck an approved instant-market envelope at enqueue or poll time."""

    raw = dict(payload or {})
    pair = str(symbol or "").strip().upper()
    direction = str(side or "").strip().upper()
    if pair not in IG_MT4_SCALP_SYMBOLS:
        return "scalp_market_entry_symbol_invalid"
    if direction not in _SIDES:
        return "scalp_market_entry_side_invalid"
    contract_error = contract.validation_error(expected_symbol=pair)
    if contract_error:
        return contract_error
    immediate_contract_error = production_scalp_immediate_entry_contract_error(
        raw,
        now_epoch=now_epoch,
    )
    if immediate_contract_error:
        return immediate_contract_error
    if (
        str(raw.get("broker_entry_plan_schema") or "").strip()
        != SCALP_BROKER_ENTRY_PLAN_SCHEMA
    ):
        return "scalp_market_entry_plan_schema_invalid"

    max_slippage_raw = raw.get("max_slippage_points")
    if isinstance(max_slippage_raw, bool):
        return "scalp_market_entry_max_slippage_invalid"
    try:
        max_slippage_number = float(max_slippage_raw)
    except (TypeError, ValueError, OverflowError):
        return "scalp_market_entry_max_slippage_invalid"
    if (
        not math.isfinite(max_slippage_number)
        or not max_slippage_number.is_integer()
        or int(max_slippage_number) != PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS
    ):
        return "scalp_market_entry_max_slippage_invalid"
    max_slippage = int(max_slippage_number)
    cushion_raw = raw.get("protection_cushion_points")
    if isinstance(cushion_raw, bool):
        return "scalp_market_entry_protection_cushion_invalid"
    try:
        cushion_number = float(cushion_raw)
    except (TypeError, ValueError, OverflowError):
        return "scalp_market_entry_protection_cushion_invalid"
    if (
        not math.isfinite(cushion_number)
        or not cushion_number.is_integer()
        or int(cushion_number) != PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS
    ):
        return "scalp_market_entry_protection_cushion_invalid"

    field_values = {
        field: _payload_number(raw, field)
        for field in (
            "entry_quote_price",
            "entry_price",
            "worst_fill_price",
            "sl_price",
            "tp_price",
        )
    }
    if any(value is None or value <= 0.0 for value in field_values.values()):
        return "scalp_market_entry_price_geometry_invalid"
    quote = float(field_values["entry_quote_price"] or 0.0)
    approved_entry = float(field_values["entry_price"] or 0.0)
    worst_fill = float(field_values["worst_fill_price"] or 0.0)
    stop = float(field_values["sl_price"] or 0.0)
    target = float(field_values["tp_price"] or 0.0)
    bid = _positive(current_bid)
    ask = _positive(current_ask)
    if bid is None or ask is None or ask < bid:
        return "scalp_market_entry_current_quote_invalid"
    tolerance = max(1e-15, contract.point * 1e-7)
    if not math.isclose(
        approved_entry,
        worst_fill,
        rel_tol=0.0,
        abs_tol=tolerance,
    ):
        return "scalp_market_entry_approved_price_mismatch"
    if any(
        not _on_tick_grid(price, tick_size=contract.tick_size)
        for price in (quote, worst_fill, stop, target)
    ):
        return "scalp_market_entry_tick_grid_mismatch"

    allowed_delta = max_slippage * contract.point
    if direction == "BUY":
        if not (
            quote - tolerance <= worst_fill <= quote + allowed_delta + tolerance
            and 0.0 < stop < worst_fill < target
        ):
            return "scalp_market_entry_bound_geometry_invalid"
        if ask > worst_fill + tolerance:
            return "scalp_market_entry_price_beyond_worst_fill"
    else:
        if not (
            quote - allowed_delta - tolerance <= worst_fill <= quote + tolerance
            and 0.0 < target < worst_fill < stop
        ):
            return "scalp_market_entry_bound_geometry_invalid"
        if bid < worst_fill - tolerance:
            return "scalp_market_entry_price_beyond_worst_fill"

    broker_floor = contract.point * (
        contract.stop_level_points + PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS
    )
    if direction == "BUY":
        stop_distance_to_market = bid - stop
        target_distance_to_market = target - ask
    else:
        stop_distance_to_market = stop - ask
        target_distance_to_market = bid - target
    if stop_distance_to_market + tolerance < broker_floor:
        return "scalp_market_entry_stop_below_broker_minimum"
    if target_distance_to_market + tolerance < broker_floor:
        return "scalp_market_entry_target_below_broker_minimum"

    plan = raw.get("broker_entry_plan")
    if not isinstance(plan, Mapping):
        return "scalp_market_entry_plan_missing"
    cost_model_id = str(raw.get("cost_model_id") or "").strip()
    if cost_model_id != SCALP_BROKER_ENTRY_COST_MODEL_MTVCLC:
        return "scalp_market_entry_cost_model_invalid"
    identity = {
        "schema_version": SCALP_BROKER_ENTRY_PLAN_SCHEMA,
        "symbol": pair,
        "broker_symbol": str(contract.broker_symbol),
        "side": direction,
        "cost_model_id": cost_model_id,
    }
    for field, expected in identity.items():
        if str(plan.get(field) or "").strip().lower() != expected.lower():
            return f"scalp_market_entry_plan_{field}_mismatch"

    bound_numeric_fields = (
        "effective_slippage_points",
        "reference_mid",
        "stop_distance_price",
        "target_distance_price",
        "stop_bps",
        "target_bps",
        "win_probability_lower_bound",
        "current_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "adverse_execution_debit_bps",
        "fixed_non_spread_cost_bps",
        "current_total_cost_bps",
        "convert_on_close_charge_fraction",
        "live_p_star",
        "conservative_expected_edge_bps",
        "reward_risk_ratio",
    )
    bound_numbers = {
        field: _payload_number(raw, field) for field in bound_numeric_fields
    }
    if any(value is None for value in bound_numbers.values()):
        return "scalp_market_entry_cost_binding_invalid"
    if "p90_spread_bps" not in raw:
        return "scalp_market_entry_cost_binding_invalid"
    p90_spread = _positive(raw.get("p90_spread_bps"))
    if p90_spread is None:
        return "scalp_market_entry_cost_model_invalid"

    normalized_cost_model, cost_model_error = _normalized_cost_model(
        ScalpBrokerEntryCostModel(
            cost_model_id=cost_model_id,
            commission_bps_per_round_trip=float(
                bound_numbers["commission_bps_per_round_trip"] or 0.0
            ),
            financing_bps_per_trade=float(
                bound_numbers["financing_bps_per_trade"] or 0.0
            ),
            adverse_execution_debit_bps=float(
                bound_numbers["adverse_execution_debit_bps"] or 0.0
            ),
            convert_on_close_charge_fraction=float(
                bound_numbers["convert_on_close_charge_fraction"] or 0.0
            ),
            p90_spread_bps=p90_spread,
        )
    )
    if cost_model_error or normalized_cost_model is None:
        return "scalp_market_entry_cost_model_invalid"

    numeric_identity = {
        "quote_entry_price": quote,
        "worst_fill_price": worst_fill,
        "sl_price": stop,
        "tp_price": target,
        "max_slippage_points": float(max_slippage),
        "protection_cushion_points": float(PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS),
        "point": float(contract.point),
        "tick_size": float(contract.tick_size),
        "digits": float(contract.digits),
        **{
            field: float(value)
            for field, value in bound_numbers.items()
            if value is not None
        },
    }
    for field, expected in numeric_identity.items():
        observed = _finite(plan.get(field))
        if observed is None or not math.isclose(
            observed,
            expected,
            rel_tol=1e-12,
            abs_tol=max(1e-15, contract.point * 1e-7),
        ):
            return f"scalp_market_entry_plan_{field}_mismatch"

    plan_p90 = plan.get("p90_spread_bps")
    if p90_spread is None:
        if plan_p90 is not None:
            return "scalp_market_entry_plan_p90_spread_bps_mismatch"
    else:
        observed_p90 = _finite(plan_p90)
        if observed_p90 is None or not math.isclose(
            observed_p90,
            p90_spread,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            return "scalp_market_entry_plan_p90_spread_bps_mismatch"

    effective_slippage = abs(worst_fill - quote) / contract.point
    final_stop_distance = abs(worst_fill - stop)
    final_target_distance = abs(target - worst_fill)
    reference_mid = float(bound_numbers["reference_mid"] or 0.0)
    lower_bound = float(bound_numbers["win_probability_lower_bound"] or 0.0)
    spread = float(bound_numbers["current_spread_bps"] or 0.0)
    commission = normalized_cost_model.commission_bps_per_round_trip
    financing = normalized_cost_model.financing_bps_per_trade
    adverse_debit = normalized_cost_model.adverse_execution_debit_bps
    conversion = normalized_cost_model.convert_on_close_charge_fraction
    if reference_mid <= 0.0 or not 0.0 <= lower_bound <= 1.0 or spread < 0.0:
        return "scalp_market_entry_cost_binding_invalid"
    if p90_spread is not None and spread > p90_spread + 1e-12:
        return "scalp_market_entry_spread_ceiling_exceeded"

    expected_stop_bps = final_stop_distance / reference_mid * 1e4
    expected_target_bps = final_target_distance / reference_mid * 1e4
    expected_fixed_non_spread = commission + financing + adverse_debit
    expected_total_cost = spread + expected_fixed_non_spread
    expected_denominator = expected_target_bps * (
        1.0 - conversion
    ) + expected_stop_bps * (1.0 + conversion)
    if not math.isfinite(expected_denominator) or expected_denominator <= 0.0:
        return "scalp_market_entry_cost_dead"
    expected_p_star = (
        expected_stop_bps * (1.0 + conversion) + expected_total_cost
    ) / expected_denominator
    expected_edge = (
        lower_bound * expected_target_bps * (1.0 - conversion)
        - (1.0 - lower_bound) * expected_stop_bps * (1.0 + conversion)
        - expected_total_cost
    )
    expected_reward_risk = expected_target_bps / expected_stop_bps
    derived_identity = {
        "effective_slippage_points": effective_slippage,
        "stop_distance_price": final_stop_distance,
        "target_distance_price": final_target_distance,
        "stop_bps": expected_stop_bps,
        "target_bps": expected_target_bps,
        "fixed_non_spread_cost_bps": expected_fixed_non_spread,
        "current_total_cost_bps": expected_total_cost,
        "live_p_star": expected_p_star,
        "conservative_expected_edge_bps": expected_edge,
        "reward_risk_ratio": expected_reward_risk,
    }
    for field, expected in derived_identity.items():
        observed = float(bound_numbers[field] or 0.0)
        if not math.isclose(
            observed,
            expected,
            rel_tol=1e-12,
            abs_tol=max(1e-12, abs(expected) * 1e-12),
        ):
            return f"scalp_market_entry_{field}_inconsistent"
    if (
        not all(
            math.isfinite(value)
            for value in (
                expected_stop_bps,
                expected_target_bps,
                expected_fixed_non_spread,
                expected_total_cost,
                expected_p_star,
                expected_edge,
                expected_reward_risk,
            )
        )
        or expected_stop_bps <= 0.0
        or expected_target_bps <= 0.0
        or not 0.0 < expected_p_star < 1.0
        or lower_bound <= expected_p_star
        or expected_edge <= 0.0
    ):
        return "scalp_market_entry_cost_dead"
    return ""


def build_scalp_broker_entry_plan(
    *,
    symbol: str,
    side: str,
    execution_type: str,
    pending_orders_forbidden: bool,
    entry_deadline_epoch: int,
    as_of_epoch: float,
    quote_entry_price: float,
    reference_mid: float,
    stop_distance_price: float,
    target_distance_price: float,
    current_spread_bps: float,
    win_probability_lower_bound: float,
    contract: BrokerContractSpec,
    cost_model: ScalpBrokerEntryCostModel,
    current_bid: float | None = None,
    current_ask: float | None = None,
    max_slippage_points: int = PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS,
) -> ScalpBrokerEntryPlanResult:
    """Project one entry onto the broker grid at its worst permitted fill.

    BUY bounds are rounded down and SELL bounds up so grid alignment never
    expands the caller's slippage allowance. Protection is built from MT4's
    actual bid/ask inequalities with a small transit cushion and rounded away
    from the market. The widened payoff and cash risk are then recomputed.
    """

    pair = str(symbol or "").strip().upper()
    direction = str(side or "").strip().upper()
    reasons: list[str] = []
    normalized_cost_model, cost_model_error = _normalized_cost_model(cost_model)
    if cost_model_error:
        reasons.append(cost_model_error)
    if pair not in IG_MT4_SCALP_SYMBOLS:
        reasons.append("scalp_broker_entry_symbol_invalid")
    if direction not in _SIDES:
        reasons.append("scalp_broker_entry_side_invalid")
    normalized_execution_type = str(execution_type or "").strip().lower()
    if normalized_execution_type != "market":
        reasons.append("scalp_broker_entry_execution_type_invalid")
    if pending_orders_forbidden is not True:
        reasons.append("scalp_broker_entry_pending_orders_not_forbidden")
    deadline = production_scalp_entry_deadline_epoch(
        {"entry_deadline_epoch": entry_deadline_epoch}
    )
    now = _finite(as_of_epoch)
    if deadline is None:
        reasons.append("scalp_broker_entry_deadline_invalid")
    elif now is None or now <= 0.0:
        reasons.append("scalp_broker_entry_clock_invalid")
    elif deadline <= now:
        reasons.append("scalp_broker_entry_deadline_expired")
    contract_error = contract.validation_error(expected_symbol=pair)
    if contract_error:
        reasons.append(contract_error)
    if isinstance(max_slippage_points, bool) or not isinstance(
        max_slippage_points, int
    ):
        reasons.append("scalp_broker_entry_max_slippage_invalid")
    elif max_slippage_points != PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS:
        reasons.append("scalp_broker_entry_max_slippage_invalid")

    quote = _positive(quote_entry_price)
    ref_mid = _positive(reference_mid)
    stop_distance = _positive(stop_distance_price)
    target_distance = _positive(target_distance_price)
    spread = _finite(current_spread_bps)
    lower_bound = _finite(win_probability_lower_bound)
    if quote is None or ref_mid is None:
        reasons.append("scalp_broker_entry_quote_invalid")
    if stop_distance is None or target_distance is None:
        reasons.append("scalp_broker_entry_distance_invalid")
    if spread is None or spread < 0.0:
        reasons.append("scalp_broker_entry_spread_invalid")
    elif (
        normalized_cost_model is not None
        and normalized_cost_model.p90_spread_bps is not None
        and spread > normalized_cost_model.p90_spread_bps + 1e-12
    ):
        reasons.append("scalp_broker_entry_spread_ceiling_exceeded")
    if lower_bound is None or not 0.0 <= lower_bound <= 1.0:
        reasons.append("scalp_broker_entry_probability_invalid")
    bid = _positive(current_bid)
    ask = _positive(current_ask)
    if (
        bid is None
        and ask is None
        and quote is not None
        and ref_mid is not None
        and spread is not None
    ):
        spread_price = ref_mid * spread / 1e4
        if direction == "BUY":
            ask = quote
            bid = quote - spread_price
        elif direction == "SELL":
            bid = quote
            ask = quote + spread_price
    if bid is None or ask is None or ask < bid:
        reasons.append("scalp_broker_entry_current_quote_invalid")
    if reasons:
        return _refusal(*reasons)

    assert quote is not None
    assert ref_mid is not None
    assert stop_distance is not None
    assert target_distance is not None
    assert spread is not None
    assert lower_bound is not None
    assert normalized_cost_model is not None
    assert bid is not None
    assert ask is not None
    assert deadline is not None
    try:
        quote_decimal = Decimal(str(quote))
        bid_decimal = Decimal(str(bid))
        ask_decimal = Decimal(str(ask))
        stop_distance_decimal = Decimal(str(stop_distance))
        target_distance_decimal = Decimal(str(target_distance))
        point_decimal = Decimal(str(contract.point))
        slip_delta_decimal = Decimal(int(max_slippage_points)) * point_decimal
        protection_floor_decimal = (
            Decimal(str(contract.stop_level_points))
            + Decimal(PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS)
        ) * point_decimal
    except (InvalidOperation, ValueError, OverflowError):
        return _refusal("scalp_broker_entry_decimal_geometry_invalid")
    if direction == "BUY":
        raw_bound_decimal = quote_decimal + slip_delta_decimal
        worst_fill = _grid_price(
            raw_bound_decimal,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=False,
        )
    else:
        raw_bound_decimal = quote_decimal - slip_delta_decimal
        worst_fill = _grid_price(
            raw_bound_decimal,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=True,
        )
    if worst_fill is None:
        return _refusal("scalp_broker_entry_worst_fill_invalid")

    price_tolerance = max(1e-15, contract.point * 1e-7)
    effective_slippage_points = abs(worst_fill - quote) / contract.point
    worst_fill_decimal = Decimal(str(worst_fill))
    if direction == "BUY":
        bound_valid = (
            quote - price_tolerance
            <= worst_fill
            <= float(raw_bound_decimal) + price_tolerance
            and ask <= worst_fill + price_tolerance
        )
        raw_stop = min(
            worst_fill_decimal - stop_distance_decimal,
            bid_decimal - protection_floor_decimal,
        )
        raw_target = max(
            worst_fill_decimal + target_distance_decimal,
            ask_decimal + protection_floor_decimal,
        )
        stop = _grid_price(
            raw_stop,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=False,
        )
        target = _grid_price(
            raw_target,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=True,
        )
    else:
        bound_valid = (
            float(raw_bound_decimal) - price_tolerance
            <= worst_fill
            <= quote + price_tolerance
            and bid >= worst_fill - price_tolerance
        )
        raw_stop = max(
            worst_fill_decimal + stop_distance_decimal,
            ask_decimal + protection_floor_decimal,
        )
        raw_target = min(
            worst_fill_decimal - target_distance_decimal,
            bid_decimal - protection_floor_decimal,
        )
        stop = _grid_price(
            raw_stop,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=True,
        )
        target = _grid_price(
            raw_target,
            tick_size=contract.tick_size,
            digits=contract.digits,
            toward_positive_infinity=False,
        )
    if not bound_valid or effective_slippage_points > float(max_slippage_points) + 1e-7:
        return _refusal("scalp_broker_entry_worst_fill_exceeds_allowance")
    if stop is None or target is None:
        return _refusal("scalp_broker_entry_bracket_grid_invalid")

    if direction == "BUY":
        ordered = 0.0 < stop < worst_fill < target
    else:
        ordered = 0.0 < target < worst_fill < stop
    if not ordered:
        return _refusal("scalp_broker_entry_bracket_direction_invalid")

    final_stop_distance = abs(worst_fill - stop)
    final_target_distance = abs(target - worst_fill)
    broker_floor = contract.point * (
        contract.stop_level_points + PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS
    )
    if direction == "BUY":
        stop_distance_to_market = bid - stop
        target_distance_to_market = target - ask
    else:
        stop_distance_to_market = stop - ask
        target_distance_to_market = bid - target
    if stop_distance_to_market + price_tolerance < broker_floor:
        return _refusal("scalp_broker_entry_stop_below_broker_minimum")
    if target_distance_to_market + price_tolerance < broker_floor:
        return _refusal("scalp_broker_entry_target_below_broker_minimum")

    stop_bps = final_stop_distance / ref_mid * 1e4
    target_bps = final_target_distance / ref_mid * 1e4
    fixed_non_spread_cost = (
        normalized_cost_model.commission_bps_per_round_trip
        + normalized_cost_model.financing_bps_per_trade
        + normalized_cost_model.adverse_execution_debit_bps
    )
    current_total_cost = spread + fixed_non_spread_cost
    conversion = normalized_cost_model.convert_on_close_charge_fraction
    payoff_denominator = target_bps * (1.0 - conversion) + stop_bps * (1.0 + conversion)
    if not math.isfinite(payoff_denominator) or payoff_denominator <= 0.0:
        return _refusal("scalp_broker_entry_cost_dead")
    live_p_star = (
        stop_bps * (1.0 + conversion) + current_total_cost
    ) / payoff_denominator
    expected_edge = (
        lower_bound * target_bps * (1.0 - conversion)
        - (1.0 - lower_bound) * stop_bps * (1.0 + conversion)
        - current_total_cost
    )
    if (
        not all(
            math.isfinite(value)
            for value in (
                stop_bps,
                target_bps,
                fixed_non_spread_cost,
                current_total_cost,
                live_p_star,
                expected_edge,
            )
        )
        or stop_bps <= 0.0
        or target_bps <= 0.0
        or not 0.0 <= live_p_star < 1.0
        or lower_bound <= live_p_star
        or expected_edge <= 0.0
    ):
        return _refusal("scalp_broker_entry_cost_dead")

    plan = ScalpBrokerEntryPlan(
        execution_type="market",
        pending_orders_forbidden=True,
        entry_deadline_epoch=int(deadline),
        symbol=pair,
        broker_symbol=str(contract.broker_symbol),
        side=direction,
        quote_entry_price=quote,
        worst_fill_price=worst_fill,
        sl_price=stop,
        tp_price=target,
        max_slippage_points=int(max_slippage_points),
        effective_slippage_points=float(effective_slippage_points),
        protection_cushion_points=PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS,
        point=float(contract.point),
        tick_size=float(contract.tick_size),
        digits=int(contract.digits),
        reference_mid=float(ref_mid),
        stop_distance_price=float(final_stop_distance),
        target_distance_price=float(final_target_distance),
        stop_bps=float(stop_bps),
        target_bps=float(target_bps),
        win_probability_lower_bound=float(lower_bound),
        cost_model_id=str(normalized_cost_model.cost_model_id),
        current_spread_bps=float(spread),
        p90_spread_bps=(
            None
            if normalized_cost_model.p90_spread_bps is None
            else float(normalized_cost_model.p90_spread_bps)
        ),
        commission_bps_per_round_trip=float(
            normalized_cost_model.commission_bps_per_round_trip
        ),
        financing_bps_per_trade=float(normalized_cost_model.financing_bps_per_trade),
        adverse_execution_debit_bps=float(
            normalized_cost_model.adverse_execution_debit_bps
        ),
        fixed_non_spread_cost_bps=float(fixed_non_spread_cost),
        current_total_cost_bps=float(current_total_cost),
        convert_on_close_charge_fraction=float(conversion),
        live_p_star=float(live_p_star),
        conservative_expected_edge_bps=float(expected_edge),
        reward_risk_ratio=float(target_bps / stop_bps),
    )
    return ScalpBrokerEntryPlanResult(plan=plan, reasons=())


__all__ = [
    "PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS",
    "PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS",
    "SCALP_BROKER_ENTRY_COST_MODEL_MTVCLC",
    "SCALP_BROKER_ENTRY_PLAN_SCHEMA",
    "ScalpBrokerEntryCostModel",
    "ScalpBrokerEntryPlan",
    "ScalpBrokerEntryPlanResult",
    "build_scalp_broker_entry_plan",
    "production_scalp_entry_deadline_epoch",
    "production_scalp_immediate_entry_contract_error",
    "production_scalp_market_entry_envelope_error",
]
