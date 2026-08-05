"""Fail-closed projection of live IG MT4 contract truth for order sizing.

The MT4 bridge publishes ``MarketInfo`` rows into runtime state.  This module
is the production-owned boundary that turns those untyped rows into validated
``BrokerContractSpec`` objects.  It deliberately refuses missing, stale, or
cross-venue state: the mixed FX/crypto universe cannot safely share the legacy
100,000-unit FX assumption.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import math
from typing import Any, Iterable, Mapping

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.risk.sizing import BrokerContractSpec, account_value_per_price_unit
from fxstack.runtime.market_source_identity import (
    authenticated_market_source_from_row,
    current_authenticated_market_source,
)


BROKER_CONTRACT_STATE_SCHEMA = "fxstack_ig_mt4_contract_state_v1"
ACCOUNT_CONVERSION_RATE_SCHEMA = "fxstack_account_conversion_rates_v1"
# A production-scalper command may spend at most two minutes between the most
# recent full MarketInfo snapshot and enqueue/poll.  Individual deployments may
# use a tighter runner cadence; the durable queue boundary never accepts a
# looser one.
MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS = 120.0
# Hard equity-relative loss ceiling for every production-scalper entry. It is
# intentionally code-owned rather than an operator knob: risk sizing, queue
# enqueue, and broker poll all recheck the same 0.5% ceiling from current
# account equity and quote truth. Unlike a fixed account-currency ceiling, this
# remains meaningful across demo and real accounts of different sizes.
PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION = 0.005


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _parse_timestamp(value: Any) -> float:
    numeric = _finite(value)
    if numeric > 0.0:
        return numeric
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _normalized_symbols(values: Iterable[Any]) -> tuple[str, ...]:
    return tuple(str(value or "").strip().upper() for value in values)


@dataclass(frozen=True, slots=True)
class BrokerContractUniverse:
    """One coherent, fresh broker snapshot for the complete strategy scope."""

    contracts: Mapping[str, BrokerContractSpec]
    account_currency: str
    available_margin: float
    observed_at: float
    age_secs: float
    errors: tuple[str, ...] = ()
    schema_version: str = BROKER_CONTRACT_STATE_SCHEMA
    venue_id: str = IG_MT4_VENUE_ID

    @property
    def ok(self) -> bool:
        return not self.errors

    def contract_for(self, symbol: str) -> BrokerContractSpec | None:
        return self.contracts.get(str(symbol or "").strip().upper())


@dataclass(frozen=True, slots=True)
class AccountConversionRateProjection:
    """Fresh, side-aware quote-to-account conversion coverage.

    ``rates`` deliberately retains the scalar mapping consumed by the canonical
    sizing kernel.  An observed ``<account><quote>`` rate is projected at ask
    because quote-to-account conversion divides by that rate; an observed
    ``<quote><account>`` rate is projected at bid because conversion multiplies
    by it.  A USD-triangulated path is emitted as a synthetic
    ``<quote><account>`` factor so the same sizing primitive remains the sole
    cash-risk owner.
    """

    account_currency: str
    rates: Mapping[str, float]
    coverage: Mapping[str, Mapping[str, Any]]
    errors: tuple[str, ...] = ()
    schema_version: str = ACCOUNT_CONVERSION_RATE_SCHEMA

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": str(self.schema_version),
            "account_currency": str(self.account_currency),
            "rates": {str(key): float(value) for key, value in self.rates.items()},
            "coverage": {
                str(key): dict(value) for key, value in self.coverage.items()
            },
            "errors": list(self.errors),
            "ok": bool(self.ok),
        }


def account_conversion_tick_symbols(
    account_currency: Any,
    *,
    required_symbols: Iterable[Any] = IG_MT4_SCALP_SYMBOLS,
) -> tuple[str, ...]:
    """Return only conversion-only symbols the MQL producer may add.

    This mirrors ``EffectiveMarketDataSymbols``: once either orientation is
    already part of the exact strategy scope, neither orientation is an
    allowable extra.  Where neither is present, both are allowable because the
    broker resolver deterministically chooses the first resolvable direction.
    """

    account = str(account_currency or "").strip().upper()
    if len(account) != 3 or not account.isalpha():
        return ()
    strategy_symbols = {
        str(item or "").strip().upper()
        for item in required_symbols
        if str(item or "").strip()
    }
    out: list[str] = []
    quote_currencies = tuple(
        dict.fromkeys(
            symbol[3:6]
            for symbol in strategy_symbols
            if len(symbol) >= 6 and len(symbol[3:6]) == 3
        )
    )
    for quote in sorted(quote_currencies):
        if quote == account:
            continue
        direct = f"{account}{quote}"
        inverse = f"{quote}{account}"
        if direct in strategy_symbols or inverse in strategy_symbols:
            continue
        out.extend((direct, inverse))
    return tuple(sorted(dict.fromkeys(out)))


def account_conversion_source_symbols(
    *,
    account_currency: Any,
    quote_currency: Any,
) -> tuple[str, ...]:
    """Symbols that can prove one direct or USD-triangulated conversion."""

    account = str(account_currency or "").strip().upper()
    quote = str(quote_currency or "").strip().upper()
    if (
        len(account) != 3
        or not account.isalpha()
        or len(quote) != 3
        or not quote.isalpha()
        or quote == account
    ):
        return ()
    out = [f"{account}{quote}", f"{quote}{account}"]
    if account != "USD" and quote != "USD":
        out.extend(
            (
                f"USD{quote}",
                f"{quote}USD",
                f"{account}USD",
                f"USD{account}",
            )
        )
    return tuple(dict.fromkeys(out))


def project_account_conversion_rates(
    ticks: Mapping[str, Any] | None,
    *,
    account_currency: Any,
    required_symbols: Iterable[Any] = IG_MT4_SCALP_SYMBOLS,
    require_market_event_fresh: bool,
) -> AccountConversionRateProjection:
    """Project complete quote-currency coverage from scoped broker ticks.

    Only direct/inverse conversion or a two-leg path through USD is accepted.
    The caller owns the symbol scope; this function owns positivity, side
    selection, optional broker-event freshness, and complete quote coverage.
    """

    account = str(account_currency or "").strip().upper()
    if len(account) != 3 or not account.isalpha():
        return AccountConversionRateProjection(
            account_currency=account,
            rates={},
            coverage={},
            errors=("scalp_account_conversion_account_currency_invalid",),
        )

    normalized_ticks: dict[str, Mapping[str, Any]] = {}
    for raw_symbol, raw_tick in dict(ticks or {}).items():
        symbol = str(raw_symbol or "").strip().upper()
        if symbol and isinstance(raw_tick, Mapping):
            normalized_ticks[symbol] = raw_tick

    usable: dict[str, tuple[float, float]] = {}
    status: dict[str, str] = {}
    event_reasons: dict[str, str] = {}
    for symbol, raw_tick in normalized_ticks.items():
        bid = _finite(raw_tick.get("bid"))
        ask = _finite(raw_tick.get("ask"))
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            status[symbol] = "invalid_quote"
            continue
        if require_market_event_fresh and raw_tick.get("market_event_fresh") is not True:
            status[symbol] = "market_event_not_fresh"
            event_reasons[symbol] = str(
                raw_tick.get("market_event_reason") or "market_event_not_fresh"
            )
            continue
        usable[symbol] = (float(bid), float(ask))
        status[symbol] = "fresh"

    def _leg(source: str, destination: str) -> dict[str, Any] | None:
        if source == destination:
            return {
                "factor": 1.0,
                "symbol": "",
                "operation": "same_currency",
                "selected_rate": 1.0,
            }
        divide_symbol = f"{destination}{source}"
        divide_quote = usable.get(divide_symbol)
        if divide_quote is not None:
            selected = float(divide_quote[1])
            return {
                "factor": float(1.0 / selected),
                "symbol": divide_symbol,
                "operation": "divide_ask",
                "selected_rate": selected,
            }
        multiply_symbol = f"{source}{destination}"
        multiply_quote = usable.get(multiply_symbol)
        if multiply_quote is not None:
            selected = float(multiply_quote[0])
            return {
                "factor": selected,
                "symbol": multiply_symbol,
                "operation": "multiply_bid",
                "selected_rate": selected,
            }
        return None

    required = tuple(str(item or "").strip().upper() for item in required_symbols)
    quote_currencies = tuple(
        dict.fromkeys(
            symbol[3:6]
            for symbol in required
            if len(symbol) >= 6 and len(symbol[3:6]) == 3
        )
    )
    rates: dict[str, float] = {}
    coverage: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for quote in quote_currencies:
        if quote == account:
            coverage[quote] = {
                "covered": True,
                "method": "same_currency",
                "factor": 1.0,
                "source_symbols": [],
                "operations": [],
                "reason": "",
            }
            continue

        direct_leg = _leg(quote, account)
        if direct_leg is not None:
            source_symbol = str(direct_leg["symbol"])
            rates[source_symbol] = float(direct_leg["selected_rate"])
            coverage[quote] = {
                "covered": True,
                "method": str(direct_leg["operation"]),
                "factor": float(direct_leg["factor"]),
                "source_symbols": [source_symbol],
                "operations": [str(direct_leg["operation"])],
                "reason": "",
            }
            continue

        first_leg = _leg(quote, "USD") if quote != "USD" else None
        second_leg = _leg("USD", account) if account != "USD" else None
        if first_leg is not None and second_leg is not None:
            factor = float(first_leg["factor"] * second_leg["factor"])
            synthetic_symbol = f"{quote}{account}"
            if factor > 0.0 and math.isfinite(factor):
                rates[synthetic_symbol] = factor
                coverage[quote] = {
                    "covered": True,
                    "method": "usd_triangulated",
                    "factor": factor,
                    "synthetic_rate_symbol": synthetic_symbol,
                    "source_symbols": [
                        str(first_leg["symbol"]),
                        str(second_leg["symbol"]),
                    ],
                    "operations": [
                        str(first_leg["operation"]),
                        str(second_leg["operation"]),
                    ],
                    "reason": "",
                }
                continue

        candidates = account_conversion_source_symbols(
            account_currency=account,
            quote_currency=quote,
        )
        candidate_status = {
            symbol: status.get(symbol, "missing") for symbol in candidates
        }
        candidate_event_reasons = {
            symbol: event_reasons[symbol]
            for symbol in candidates
            if symbol in event_reasons
        }
        if "invalid_quote" in candidate_status.values():
            reason = f"scalp_account_conversion_tick_invalid:{quote}"
        elif "market_event_not_fresh" in candidate_status.values():
            reason = f"scalp_account_conversion_tick_not_fresh:{quote}"
        else:
            reason = f"scalp_account_conversion_tick_missing:{quote}"
        errors.append(reason)
        coverage[quote] = {
            "covered": False,
            "method": "unresolved",
            "factor": 0.0,
            "source_symbols": [],
            "operations": [],
            "reason": reason,
            "candidate_status": candidate_status,
            "candidate_event_reasons": candidate_event_reasons,
        }

    return AccountConversionRateProjection(
        account_currency=account,
        rates=rates,
        coverage=coverage,
        errors=tuple(dict.fromkeys(errors)),
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )


def broker_contract_binding_sha256(
    universe: BrokerContractUniverse,
    *,
    symbol: str,
) -> str:
    """Bind an order to exact venue/account-currency/contract geometry."""

    pair = str(symbol or "").strip().upper()
    contract = universe.contract_for(pair)
    if contract is None:
        return ""
    # Tick value is a price-dependent valuation observation rather than stable
    # execution geometry. It must be positive in the typed snapshot, but is not
    # part of the command identity: otherwise an ordinary quote move could
    # invalidate a queued order even though lot, price grid, stops, and margin
    # contract are unchanged.
    execution_geometry = {
        "broker_symbol": str(contract.broker_symbol),
        "lot_size": float(contract.lot_size),
        "min_lot": float(contract.min_lot),
        "lot_step": float(contract.lot_step),
        "max_lot": float(contract.max_lot),
        "point": float(contract.point),
        "digits": int(contract.digits),
        "tick_size": float(contract.tick_size),
        "stop_level_points": float(contract.stop_level_points),
        "freeze_level_points": float(contract.freeze_level_points),
        "margin_required": float(contract.margin_required),
        "trade_allowed": bool(contract.trade_allowed),
    }
    payload = {
        "schema_version": BROKER_CONTRACT_STATE_SCHEMA,
        "venue_id": str(universe.venue_id),
        "account_currency": str(universe.account_currency),
        "symbol": pair,
        "execution_geometry": execution_geometry,
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def broker_contract_command_fields(
    universe: BrokerContractUniverse,
    *,
    symbol: str,
) -> dict[str, Any]:
    """Fields a broker-sized command must retain through queue delivery."""

    pair = str(symbol or "").strip().upper()
    contract = universe.contract_for(pair)
    if contract is None:
        return {}
    return {
        "expected_broker_contract_state_schema": BROKER_CONTRACT_STATE_SCHEMA,
        "expected_broker_contract_venue_id": str(universe.venue_id),
        "expected_broker_contract_symbol": pair,
        "expected_broker_contract_broker_symbol": str(contract.broker_symbol),
        "expected_broker_contract_account_currency": str(universe.account_currency),
        "expected_broker_contract_binding_sha256": (
            broker_contract_binding_sha256(universe, symbol=pair)
        ),
        "expected_broker_contract_lot_size": float(contract.lot_size),
        "expected_broker_contract_min_lot": float(contract.min_lot),
        "expected_broker_contract_lot_step": float(contract.lot_step),
        "expected_broker_contract_max_lot": float(contract.max_lot),
        "expected_broker_contract_point": float(contract.point),
        "expected_broker_contract_digits": int(contract.digits),
        "expected_broker_contract_tick_size": float(contract.tick_size),
        "expected_broker_contract_stop_level_points": float(
            contract.stop_level_points
        ),
        "expected_broker_contract_freeze_level_points": float(
            contract.freeze_level_points
        ),
        "expected_broker_contract_margin_required": float(
            contract.margin_required
        ),
        "expected_broker_contract_trade_allowed": bool(
            contract.trade_allowed
        ),
    }


def broker_contract_command_binding_error(
    payload: Mapping[str, Any] | None,
    *,
    universe: BrokerContractUniverse,
    symbol: str,
) -> str:
    """Recheck the order's sizing identity against current broker state."""

    raw = dict(payload or {})
    expected = broker_contract_command_fields(universe, symbol=symbol)
    if not expected:
        return "broker_contract_command_spec_missing"
    for field, expected_value in expected.items():
        if field not in raw or raw[field] is None:
            return f"{field}_missing"
        observed_value = raw[field]
        if isinstance(observed_value, str) and not observed_value.strip():
            return f"{field}_missing"
        values_match = False
        if isinstance(expected_value, bool):
            values_match = observed_value is expected_value
        elif isinstance(expected_value, (int, float)) and not isinstance(
            expected_value, bool
        ):
            if not isinstance(observed_value, bool):
                observed_number = _finite(observed_value, default=math.nan)
                values_match = math.isfinite(observed_number) and math.isclose(
                    observed_number,
                    float(expected_value),
                    rel_tol=1e-12,
                    abs_tol=1e-15,
                )
        else:
            values_match = (
                str(observed_value).strip().lower()
                == str(expected_value).strip().lower()
            )
        if not values_match:
            return f"{field}_changed"
    return ""


def broker_contract_order_geometry_error(
    payload: Mapping[str, Any] | None,
    *,
    universe: BrokerContractUniverse,
    symbol: str,
    entry_price: float,
    equity: float,
    side: str = "",
    current_bid: float | None = None,
    current_ask: float | None = None,
) -> str:
    """Validate lot quantum, stop floor, and margin immediately before send."""

    binding_error = broker_contract_command_binding_error(
        payload,
        universe=universe,
        symbol=symbol,
    )
    if binding_error:
        return binding_error
    pair = str(symbol or "").strip().upper()
    contract = universe.contract_for(pair)
    if contract is None:
        return "broker_contract_command_spec_missing"
    raw = dict(payload or {})
    lots = _finite(raw.get("lots"))
    if lots <= 0.0:
        return "broker_contract_order_lots_invalid"
    tolerance = max(1e-9, contract.lot_step / 1000.0)
    if lots + tolerance < contract.min_lot:
        return "broker_contract_order_below_min_lot"
    if lots > contract.max_lot + tolerance:
        return "broker_contract_order_above_max_lot"
    steps = lots / contract.lot_step
    if abs(steps - round(steps)) > max(1e-8, tolerance / contract.lot_step):
        return "broker_contract_order_lot_step_mismatch"

    entry = _finite(entry_price)
    stop = _finite(raw.get("sl_price"))
    target = _finite(raw.get("tp_price"))
    if entry <= 0.0 or stop <= 0.0 or math.isclose(entry, stop):
        return "broker_contract_order_stop_invalid"
    broker_floor = contract.point * contract.stop_level_points
    direction = str(side or raw.get("cmd") or "").strip().upper()
    bid = _finite(current_bid)
    ask = _finite(current_ask)
    if direction in {"BUY", "SELL"} and bid > 0.0 and ask >= bid:
        if target <= 0.0 or math.isclose(entry, target):
            return "broker_contract_order_target_invalid"
        if direction == "BUY":
            stop_distance = bid - stop
            target_distance = target - ask
        else:
            stop_distance = stop - ask
            target_distance = bid - target
    else:
        stop_distance = abs(entry - stop)
        target_distance = abs(target - entry) if target > 0.0 else math.inf
    if stop_distance + 1e-12 < broker_floor:
        return "broker_contract_order_stop_below_minimum"
    if target_distance + 1e-12 < broker_floor:
        return "broker_contract_order_target_below_minimum"

    margin_cap = _finite(raw.get("broker_contract_margin_utilization_cap"))
    account_equity = _finite(equity)
    if not 0.0 < margin_cap <= 1.0:
        return "broker_contract_order_margin_cap_invalid"
    if account_equity <= 0.0 or universe.available_margin <= 0.0:
        return "broker_contract_order_margin_unattested"
    margin_budget = min(
        universe.available_margin,
        account_equity * margin_cap,
    )
    if lots * contract.margin_required > margin_budget + 1e-9:
        return "broker_contract_order_margin_exceeded"
    return ""


def broker_contract_order_cash_risk_error(
    payload: Mapping[str, Any] | None,
    *,
    universe: BrokerContractUniverse,
    symbol: str,
    entry_price: float,
    equity: float,
    quote_rates: Mapping[str, Any],
) -> str:
    """Revalue one approved order's stop loss at the current broker quote.

    ``broker_contract_sizing`` is the immutable risk-kernel proof carried by the
    canonical command payload.  Recomputing from the latest entry and currency
    conversion rates closes the enqueue-to-poll window in which a still-valid
    lot quantum can exceed either its originally approved cash risk or the
    current account-equity budget.
    """

    pair = str(symbol or "").strip().upper()
    contract = universe.contract_for(pair)
    if contract is None:
        return "broker_contract_order_cash_risk_spec_missing"
    raw = dict(payload or {})
    raw_proof = raw.get("broker_contract_sizing")
    if not isinstance(raw_proof, Mapping):
        return "broker_contract_order_cash_risk_proof_missing"
    proof = dict(raw_proof)
    if (
        proof.get("required") is not True
        or str(proof.get("status") or "").strip().lower() != "approved"
    ):
        return "broker_contract_order_cash_risk_proof_invalid"
    if (
        str(proof.get("symbol") or "").strip().upper() != pair
        or str(proof.get("broker_symbol") or "").strip() != str(contract.broker_symbol)
        or str(proof.get("account_currency") or "").strip().upper()
        != str(universe.account_currency).strip().upper()
    ):
        return "broker_contract_order_cash_risk_proof_mismatch"

    lots = _finite(raw.get("lots"))
    approved_lots = _finite(proof.get("lots"))
    approved_money_at_risk = _finite(proof.get("money_at_risk"))
    approved_vpu = _finite(proof.get("value_per_price_unit"))
    requested_fraction = _finite(proof.get("requested_risk_fraction"))
    budgeted_fraction = _finite(proof.get("budgeted_risk_fraction"))
    effective_fraction = _finite(proof.get("effective_risk_fraction"))
    current_equity = _finite(equity)
    approved_entry = _finite(raw.get("entry_price"))
    current_entry = _finite(entry_price)
    stop = _finite(raw.get("sl_price"))
    numeric_values = (
        lots,
        approved_lots,
        approved_money_at_risk,
        approved_vpu,
        requested_fraction,
        budgeted_fraction,
        effective_fraction,
        current_equity,
        approved_entry,
        current_entry,
        stop,
    )
    if any(value <= 0.0 for value in numeric_values):
        return "broker_contract_order_cash_risk_proof_invalid"
    fraction_tolerance = 1e-12
    if (
        requested_fraction > 1.0
        or budgeted_fraction > requested_fraction + fraction_tolerance
        or effective_fraction > budgeted_fraction + fraction_tolerance
    ):
        return "broker_contract_order_cash_risk_proof_invalid"
    lot_tolerance = max(1e-9, contract.lot_step / 1000.0)
    if lots > approved_lots + lot_tolerance:
        return "broker_contract_order_cash_risk_lots_exceed_approval"
    if math.isclose(approved_entry, stop) or math.isclose(current_entry, stop):
        return "broker_contract_order_cash_risk_stop_invalid"

    proof_cash_risk = approved_lots * abs(approved_entry - stop) * approved_vpu
    proof_tolerance = max(1e-9, approved_money_at_risk * 1e-9)
    if not math.isclose(
        proof_cash_risk,
        approved_money_at_risk,
        rel_tol=1e-9,
        abs_tol=proof_tolerance,
    ):
        return "broker_contract_order_cash_risk_proof_mismatch"
    hard_cash_cap = (
        current_equity * float(PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION)
    )
    hard_cap_tolerance = max(1e-9, hard_cash_cap * 1e-9)
    if approved_money_at_risk > hard_cash_cap + hard_cap_tolerance:
        return "broker_contract_order_cash_risk_hard_cap_exceeded"

    current_vpu = account_value_per_price_unit(
        pair=pair,
        rates=dict(quote_rates or {}),
        account_currency=universe.account_currency,
        contract_units=contract.lot_size,
    )
    if current_vpu <= 0.0:
        return "broker_contract_order_cash_risk_conversion_unresolvable"
    current_cash_risk = lots * abs(current_entry - stop) * current_vpu
    current_fraction_budget = current_equity * budgeted_fraction
    current_cash_cap = min(
        approved_money_at_risk,
        current_fraction_budget,
        hard_cash_cap,
    )
    cash_tolerance = max(1e-9, current_cash_cap * 1e-9)
    if current_cash_risk > current_cash_cap + cash_tolerance:
        return "broker_contract_order_cash_risk_exceeded"
    return ""


def _project_ig_mt4_contract_scope(
    state: Mapping[str, Any] | None,
    *,
    now_ts: float,
    max_age_secs: float,
    requested: tuple[str, ...],
    scope_errors: Iterable[str] = (),
    future_tolerance_secs: float = 5.0,
    require_trade_allowed: bool = True,
) -> BrokerContractUniverse:
    """Validate common account state and only the requested contract rows."""

    raw_state = dict(state or {})
    now = _finite(now_ts)
    max_age = _finite(max_age_secs)
    future_tolerance = max(0.0, _finite(future_tolerance_secs, 5.0))
    errors: list[str] = []

    if now <= 0.0:
        errors.append("broker_contract_clock_invalid")
    if max_age <= 0.0:
        errors.append("broker_contract_max_age_invalid")
    errors.extend(str(error) for error in scope_errors if str(error))

    venue_id = str(raw_state.get("broker_venue_id") or "").strip().lower()
    if venue_id != IG_MT4_VENUE_ID:
        errors.append("broker_contract_ig_mt4_venue_unattested")

    account_currency = (
        str(raw_state.get("broker_account_currency") or "").strip().upper()
    )
    if len(account_currency) != 3 or not account_currency.isalpha():
        errors.append("broker_contract_account_currency_unattested")

    available_margin = _finite(raw_state.get("freemargin"))
    if available_margin <= 0.0:
        errors.append("broker_contract_free_margin_unattested")

    observed_at = _parse_timestamp(raw_state.get("symbol_specs_ts"))
    age_secs = math.inf
    if observed_at <= 0.0:
        errors.append("broker_contract_specs_timestamp_missing")
    elif now > 0.0:
        age_secs = now - observed_at
        if age_secs < -future_tolerance:
            errors.append("broker_contract_specs_timestamp_future")
        elif max_age > 0.0 and age_secs > max_age:
            errors.append("broker_contract_specs_stale")

    raw_specs = raw_state.get("symbol_specs")
    spec_table = dict(raw_specs) if isinstance(raw_specs, Mapping) else {}
    if not spec_table:
        errors.append("broker_contract_specs_missing")

    current_market_source, _market_source_error = (
        current_authenticated_market_source(
            raw_state,
            now_epoch=now,
            require_active_lease=True,
        )
    )
    if current_market_source is None:
        errors.append("broker_contract_market_source_unattested")
    else:
        canonical_source = current_market_source.to_fields()
        bridge_source_raw = raw_state.get("bridge_market_source")
        bridge_source, bridge_source_error = authenticated_market_source_from_row(
            bridge_source_raw if isinstance(bridge_source_raw, Mapping) else None
        )
        if bridge_source_error:
            errors.append(
                f"broker_contract_bridge_market_source_invalid:{bridge_source_error}"
            )
        elif (
            bridge_source != current_market_source
            or dict(bridge_source_raw) != canonical_source
        ):
            errors.append("broker_contract_bridge_market_source_mismatch")

        specs_source_raw = raw_state.get("symbol_specs_market_source")
        specs_source, specs_source_error = authenticated_market_source_from_row(
            specs_source_raw if isinstance(specs_source_raw, Mapping) else None
        )
        specs_source_id = str(
            raw_state.get("symbol_specs_market_source_id") or ""
        ).strip().lower()
        if specs_source_error:
            errors.append(
                f"broker_contract_specs_market_source_invalid:{specs_source_error}"
            )
        elif (
            specs_source != current_market_source
            or dict(specs_source_raw) != canonical_source
        ):
            errors.append("broker_contract_specs_market_source_mismatch")
        if specs_source_id != current_market_source.source_id:
            errors.append("broker_contract_specs_market_source_id_mismatch")

    contracts: dict[str, BrokerContractSpec] = {}
    normalized_table = {
        str(key or "").strip().upper(): value
        for key, value in spec_table.items()
        if str(key or "").strip()
    }
    for symbol in requested:
        payload = normalized_table.get(symbol)
        if not isinstance(payload, Mapping):
            errors.append(f"broker_contract_spec_missing:{symbol}")
            continue
        contract = BrokerContractSpec.from_mapping(symbol=symbol, payload=payload)
        validation_error = contract.validation_error(expected_symbol=symbol)
        if (
            not require_trade_allowed
            and validation_error == "broker_contract_trade_not_allowed"
        ):
            validation_error = ""
        if validation_error:
            errors.append(f"{validation_error}:{symbol}")
            continue
        contracts[symbol] = contract

    return BrokerContractUniverse(
        contracts=contracts,
        account_currency=account_currency,
        available_margin=available_margin,
        observed_at=observed_at,
        age_secs=age_secs,
        errors=tuple(dict.fromkeys(errors)),
        venue_id=venue_id,
    )


def project_ig_mt4_contract_universe(
    state: Mapping[str, Any] | None,
    *,
    now_ts: float,
    max_age_secs: float,
    required_symbols: Iterable[str] = IG_MT4_SCALP_SYMBOLS,
    future_tolerance_secs: float = 5.0,
) -> BrokerContractUniverse:
    """Validate the exact-22 aggregate snapshot without inventing facts."""

    requested = _normalized_symbols(required_symbols)
    scope_errors: list[str] = []
    if (
        not requested
        or any(not symbol for symbol in requested)
        or len(set(requested)) != len(requested)
    ):
        scope_errors.append("broker_contract_symbol_scope_invalid")
    elif set(requested) != set(IG_MT4_SCALP_SYMBOLS):
        scope_errors.append("broker_contract_symbol_scope_incomplete")
    return _project_ig_mt4_contract_scope(
        state,
        now_ts=now_ts,
        max_age_secs=max_age_secs,
        requested=requested,
        scope_errors=scope_errors,
        future_tolerance_secs=future_tolerance_secs,
    )


def project_ig_mt4_authority_contract_universe(
    state: Mapping[str, Any] | None,
    *,
    now_ts: float,
    max_age_secs: float,
    required_symbols: Iterable[str] = IG_MT4_SCALP_SYMBOLS,
    future_tolerance_secs: float = 5.0,
) -> BrokerContractUniverse:
    """Validate exact-scope contract identity without globalizing tradeability.

    Authority activation needs a complete, fresh, authenticated contract table
    for all 22 strategy symbols.  ``trade_allowed`` is broker-session state,
    however, so one closed instrument must not revoke authority for unrelated
    symbols.  Selected-symbol projections remain strict and still reject that
    instrument before sizing, enqueue, poll, provider dispatch, and EA entry.
    """

    requested = _normalized_symbols(required_symbols)
    scope_errors: list[str] = []
    if (
        not requested
        or any(not symbol for symbol in requested)
        or len(set(requested)) != len(requested)
    ):
        scope_errors.append("broker_contract_symbol_scope_invalid")
    elif set(requested) != set(IG_MT4_SCALP_SYMBOLS):
        scope_errors.append("broker_contract_symbol_scope_incomplete")
    return _project_ig_mt4_contract_scope(
        state,
        now_ts=now_ts,
        max_age_secs=max_age_secs,
        requested=requested,
        scope_errors=scope_errors,
        future_tolerance_secs=future_tolerance_secs,
        require_trade_allowed=False,
    )


def project_ig_mt4_selected_contract_universe(
    state: Mapping[str, Any] | None,
    *,
    selected_symbols: Iterable[str],
    now_ts: float,
    max_age_secs: float,
    future_tolerance_secs: float = 5.0,
) -> BrokerContractUniverse:
    """Validate an explicit non-empty IG MT4 subset for selected risk.

    Account, venue, margin, snapshot-clock, and table provenance remain common
    fail-closed invariants. Missing or malformed rows outside ``selected_symbols``
    are deliberately excluded so they cannot veto an otherwise executable
    strategy symbol.
    """

    requested = _normalized_symbols(selected_symbols)
    scope_errors: list[str] = []
    if (
        not requested
        or any(not symbol for symbol in requested)
        or len(set(requested)) != len(requested)
    ):
        scope_errors.append("broker_contract_selected_scope_invalid")
    else:
        supported = set(IG_MT4_SCALP_SYMBOLS)
        scope_errors.extend(
            f"broker_contract_selected_symbol_unsupported:{symbol}"
            for symbol in requested
            if symbol not in supported
        )
    return _project_ig_mt4_contract_scope(
        state,
        now_ts=now_ts,
        max_age_secs=max_age_secs,
        requested=requested,
        scope_errors=scope_errors,
        future_tolerance_secs=future_tolerance_secs,
    )


def broker_contract_sizing_metadata(
    universe: BrokerContractUniverse,
    *,
    symbol: str,
    margin_utilization_cap: float,
    quote_rates: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the typed risk-kernel handoff for one catalog symbol.

    The required marker is included even on refusal.  Downstream code must not
    interpret an empty metadata mapping as permission to use legacy sizing.
    """

    pair = str(symbol or "").strip().upper()
    metadata: dict[str, Any] = {
        "broker_contract_required": True,
        "broker_contract_state_schema": BROKER_CONTRACT_STATE_SCHEMA,
        "broker_contract_venue_id": str(universe.venue_id),
        "broker_contract_symbol": pair,
        "broker_contract_account_currency": str(universe.account_currency),
        "broker_contract_available_margin": float(universe.available_margin),
        "broker_contract_margin_utilization_cap": float(
            _finite(margin_utilization_cap)
        ),
        "broker_contract_observed_at": float(universe.observed_at),
        "broker_contract_age_secs": (
            None if not math.isfinite(universe.age_secs) else float(universe.age_secs)
        ),
        "broker_contract_errors": list(universe.errors),
        "quote_rates": {
            str(pair or "").strip().upper(): float(_finite(rate))
            for pair, rate in dict(quote_rates or {}).items()
            if str(pair or "").strip() and _finite(rate) > 0.0
        },
    }
    contract = universe.contract_for(pair)
    if contract is not None:
        metadata["broker_contract_spec"] = asdict(contract)
        metadata.update(broker_contract_command_fields(universe, symbol=pair))
    return metadata


__all__ = [
    "ACCOUNT_CONVERSION_RATE_SCHEMA",
    "BROKER_CONTRACT_STATE_SCHEMA",
    "MAX_PRODUCTION_SCALP_BROKER_CONTRACT_AGE_SECS",
    "PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION",
    "AccountConversionRateProjection",
    "BrokerContractUniverse",
    "account_conversion_source_symbols",
    "account_conversion_tick_symbols",
    "broker_contract_binding_sha256",
    "broker_contract_command_binding_error",
    "broker_contract_command_fields",
    "broker_contract_order_cash_risk_error",
    "broker_contract_order_geometry_error",
    "broker_contract_sizing_metadata",
    "project_account_conversion_rates",
    "project_ig_mt4_authority_contract_universe",
    "project_ig_mt4_contract_universe",
    "project_ig_mt4_selected_contract_universe",
]
