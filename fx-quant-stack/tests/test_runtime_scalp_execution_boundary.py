from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_CRYPTO_CFD_SYMBOLS,
    IG_MT4_SCALP_SYMBOLS,
)
from fxstack.providers.execution.mt4 import command_to_wire_line
from fxstack.risk.sizing import BrokerContractSpec
from fxstack.runtime.broker_contract_state import (
    BrokerContractUniverse,
    broker_contract_sizing_metadata,
)
from fxstack.runtime.dto import ExecutionCommand
from fxstack.runtime.scalp_execution_boundary import (
    PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS,
    SCALP_BROKER_ENTRY_COST_MODEL_SPREAD_ONLY,
    SCALP_BROKER_ENTRY_PLAN_SCHEMA,
    ScalpBrokerEntryCostModel,
    build_scalp_broker_entry_plan as _build_scalp_broker_entry_plan,
    production_scalp_market_entry_envelope_error,
)
from fxstack.runtime.scalp_execution_authority import (
    ScalpAuthorityExpectation,
    build_active_authority,
    command_binding_fields,
)
from fxstack.strategy.mtvclc import (
    FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


PLAN_NOW = 2_000_000_000


def build_scalp_broker_entry_plan(**kwargs: Any):
    return _build_scalp_broker_entry_plan(
        execution_type="market",
        pending_orders_forbidden=True,
        entry_deadline_epoch=PLAN_NOW + 5,
        as_of_epoch=PLAN_NOW,
        **kwargs,
    )


def _contract(symbol: str, **overrides: object) -> BrokerContractSpec:
    crypto = symbol in IG_MT4_CRYPTO_CFD_SYMBOLS
    point = 0.01 if crypto else (0.001 if symbol.endswith("JPY") else 0.00001)
    values: dict[str, object] = {
        "symbol": symbol,
        "broker_symbol": f"{symbol}.IG",
        "lot_size": 1.0 if crypto else 100_000.0,
        "min_lot": 0.01,
        "lot_step": 0.01,
        "max_lot": 100.0,
        "point": point,
        "stop_level_points": 10.0,
        "margin_required": 100.0,
        "digits": 2 if crypto else (3 if symbol.endswith("JPY") else 5),
        "tick_size": point,
        "tick_value": 1.0,
        "freeze_level_points": 0.0,
        "trade_allowed": True,
    }
    values.update(overrides)
    return BrokerContractSpec(**values)  # type: ignore[arg-type]


def _geometry(symbol: str) -> tuple[float, float, float]:
    if symbol in IG_MT4_CRYPTO_CFD_SYMBOLS:
        return 1_000.0, 10.0, 20.0
    if symbol.endswith("JPY"):
        return 150.0, 0.15, 0.30
    return 1.10, 0.001, 0.002


def _production_command(
    side: str = "BUY",
    *,
    cost_model: ScalpBrokerEntryCostModel | None = None,
) -> ExecutionCommand:
    contract = _contract("EURUSD")
    universe = BrokerContractUniverse(
        contracts={"EURUSD": contract},
        account_currency="USD",
        available_margin=9_000.0,
        observed_at=1_900_000_000.0,
        age_secs=0.0,
    )
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side=side,
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=contract,
        cost_model=cost_model,
    )
    assert result.plan is not None
    payload = broker_contract_sizing_metadata(
        universe,
        symbol="EURUSD",
        margin_utilization_cap=0.25,
    )
    payload.update(result.plan.command_fields())
    authority = build_active_authority(
        ScalpAuthorityExpectation(
            generation_id="mtvclc-generation-1",
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256="1" * 64,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="2" * 64,
            runtime_release_signing_key_id="3" * 64,
            research_evidence_sha256="4" * 64,
            research_evidence_signing_key_id="5" * 64,
            registry_generation_id="mtvclc-generation-1",
            registry_revision=11,
            registry_sha256="6" * 64,
            qualification_surface_sha256="7" * 64,
            cost_mapping_sha256="8" * 64,
            execution_contract_sha256="9" * 64,
            validation_expires_at_epoch=2_100_000_000.0,
            runtime_boot_id="execution-boundary-boot",
            authority_revision=7,
        ),
        activated_at=1_900_000_000.0,
    )
    payload.update(command_binding_fields(authority))
    payload.update(
        {
            "strategy_lane": "production_scalper",
            "intent": "production_scalper_entry",
            "expected_account_mode": "demo",
            "expected_account_scope": "ig-demo-scope",
            "lots": 0.1,
            "sl_price": result.plan.sl_price,
            "tp_price": result.plan.tp_price,
        }
    )
    return ExecutionCommand(
        command_id="scalp-market-entry-1",
        session_id="session-1",
        proto="v3.0.0",
        cmd=side,
        symbol="EURUSD",
        lots=0.1,
        sl_price=result.plan.sl_price,
        tp_price=result.plan.tp_price,
        magic=246_810,
        owner_token="fxs-market-entry-1",
        ownership_contract="ticket_owner_v1",
        intent="PRODUCTION_SCALPER_ENTRY",
        payload=payload,
    )


@pytest.mark.parametrize("symbol", IG_MT4_SCALP_SYMBOLS)
@pytest.mark.parametrize("side", ("BUY", "SELL"))
def test_exact_22_market_entries_are_bounded_on_the_broker_grid(
    symbol: str,
    side: str,
) -> None:
    quote, stop_distance, target_distance = _geometry(symbol)
    contract = _contract(symbol)
    result = build_scalp_broker_entry_plan(
        symbol=symbol,
        side=side,
        quote_entry_price=quote,
        reference_mid=quote,
        stop_distance_price=stop_distance,
        target_distance_price=target_distance,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=contract,
    )

    assert result.accepted, (symbol, side, result)
    assert result.plan is not None
    plan = result.plan
    assert plan.schema_version == SCALP_BROKER_ENTRY_PLAN_SCHEMA
    assert plan.max_slippage_points == PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS
    assert plan.effective_slippage_points <= plan.max_slippage_points + 1e-7
    for price in (plan.worst_fill_price, plan.sl_price, plan.tp_price):
        assert math.isclose(
            price / contract.tick_size,
            round(price / contract.tick_size),
            rel_tol=0.0,
            abs_tol=1e-7,
        )
    if side == "BUY":
        assert quote <= plan.worst_fill_price <= quote + 20 * contract.point
        assert plan.sl_price < plan.worst_fill_price < plan.tp_price
        # A fill better than the bound has less stop risk and more reward.
        assert quote - plan.sl_price <= plan.stop_distance_price + 1e-12
        assert plan.tp_price - quote >= plan.target_distance_price - 1e-12
    else:
        assert quote - 20 * contract.point <= plan.worst_fill_price <= quote
        assert plan.tp_price < plan.worst_fill_price < plan.sl_price
        assert plan.sl_price - quote <= plan.stop_distance_price + 1e-12
        assert quote - plan.tp_price >= plan.target_distance_price - 1e-12


@pytest.mark.parametrize(
    ("side", "expected_bound", "expected_effective_points"),
    (
        ("BUY", 1.1002, 20.0),
        ("SELL", 1.0998, 20.0),
    ),
)
def test_tick_grid_rounding_keeps_slippage_bounded_and_protection_outward(
    side: str,
    expected_bound: float,
    expected_effective_points: float,
) -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side=side,
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001003,
        target_distance_price=0.002007,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD", tick_size=0.00005),
        max_slippage_points=PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS,
    )

    assert result.accepted
    assert result.plan is not None
    assert result.plan.worst_fill_price == pytest.approx(expected_bound)
    assert result.plan.effective_slippage_points == pytest.approx(
        expected_effective_points
    )
    assert result.plan.stop_distance_price >= 0.001003 - 1e-12
    assert result.plan.target_distance_price >= 0.002007 - 1e-12


def test_command_fields_size_cash_risk_from_worst_fill_not_current_quote() -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD"),
    )
    assert result.plan is not None

    fields = result.plan.command_fields()
    assert fields["execution_type"] == "market"
    assert fields["pending_orders_forbidden"] is True
    assert fields["entry_deadline_epoch"] == PLAN_NOW + 5
    assert fields["entry_price"] == result.plan.worst_fill_price
    assert fields["worst_fill_price"] == result.plan.worst_fill_price
    assert fields["entry_quote_price"] == pytest.approx(1.1)
    assert fields["max_slippage_points"] == 20
    assert fields["broker_entry_plan"]["broker_symbol"] == "EURUSD.IG"


def test_default_cost_contract_includes_fixed_execution_debit() -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD"),
    )

    assert result.plan is not None
    plan = result.plan
    assert plan.cost_model_id == SCALP_BROKER_ENTRY_COST_MODEL_SPREAD_ONLY
    assert plan.fixed_non_spread_cost_bps == FIXED_ADVERSE_EXECUTION_DEBIT_BPS
    assert plan.current_total_cost_bps == pytest.approx(
        plan.current_spread_bps + FIXED_ADVERSE_EXECUTION_DEBIT_BPS
    )
    assert plan.convert_on_close_charge_fraction == 0.0
    assert plan.live_p_star == pytest.approx(
        (plan.stop_bps + plan.current_total_cost_bps)
        / (plan.target_bps + plan.stop_bps)
    )
    assert plan.conservative_expected_edge_bps == pytest.approx(
        plan.win_probability_lower_bound * plan.target_bps
        - (1.0 - plan.win_probability_lower_bound) * plan.stop_bps
        - plan.current_total_cost_bps
    )


@pytest.mark.parametrize("conversion", (0.0, 0.005))
def test_mtvclc_cost_contract_recomputes_exact_grid_payoff(
    conversion: float,
) -> None:
    lower_bound = 0.65
    model = ScalpBrokerEntryCostModel.mtvclc(
        cost_model_id="frozen-mtvclc-cost-row-2026-08",
        p90_spread_bps=0.9,
        commission_bps_per_round_trip=0.25,
        financing_bps_per_trade=0.15,
        convert_on_close_charge_fraction=conversion,
    )
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.00001,
        target_distance_price=0.001,
        current_spread_bps=0.5,
        win_probability_lower_bound=lower_bound,
        contract=_contract("EURUSD", stop_level_points=10.0),
        cost_model=model,
    )

    assert result.accepted, result.reasons
    assert result.plan is not None
    plan = result.plan
    assert plan.stop_distance_price > 0.00001
    assert plan.target_distance_price >= 0.001 - 1e-12
    assert plan.stop_bps == pytest.approx(
        plan.stop_distance_price / plan.reference_mid * 1e4
    )
    assert plan.target_bps == pytest.approx(
        plan.target_distance_price / plan.reference_mid * 1e4
    )
    fixed_cost = 0.25 + 0.15 + 1.0
    current_total_cost = 0.5 + fixed_cost
    denominator = plan.target_bps * (1.0 - conversion) + plan.stop_bps * (
        1.0 + conversion
    )
    expected_p_star = (
        plan.stop_bps * (1.0 + conversion) + current_total_cost
    ) / denominator
    expected_edge = (
        lower_bound * plan.target_bps * (1.0 - conversion)
        - (1.0 - lower_bound) * plan.stop_bps * (1.0 + conversion)
        - current_total_cost
    )
    assert plan.fixed_non_spread_cost_bps == pytest.approx(fixed_cost)
    assert plan.current_total_cost_bps == pytest.approx(current_total_cost)
    assert plan.live_p_star == pytest.approx(expected_p_star)
    assert plan.conservative_expected_edge_bps == pytest.approx(expected_edge)
    fields = plan.command_fields()
    for field in (
        "reference_mid",
        "win_probability_lower_bound",
        "cost_model_id",
        "current_spread_bps",
        "p90_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "adverse_execution_debit_bps",
        "fixed_non_spread_cost_bps",
        "current_total_cost_bps",
        "convert_on_close_charge_fraction",
        "live_p_star",
        "conservative_expected_edge_bps",
    ):
        assert fields[field] == fields["broker_entry_plan"][field]


def test_frozen_p90_spread_ceiling_refuses_grid_projection() -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="SELL",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5000001,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD"),
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            p90_spread_bps=0.5,
            commission_bps_per_round_trip=0.25,
            financing_bps_per_trade=0.15,
            convert_on_close_charge_fraction=0.0,
        ),
    )

    assert result.plan is None
    assert result.reasons == ("scalp_broker_entry_spread_ceiling_exceeded",)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    (
        ({"trade_allowed": False}, "broker_contract_trade_not_allowed"),
        ({"tick_size": 0.0}, "broker_contract_tick_size_invalid"),
        ({"digits": 0}, "broker_contract_digits_invalid"),
        ({"digits": 9, "point": 1e-9}, "broker_contract_digits_invalid"),
    ),
)
def test_malformed_or_nontradeable_contract_refuses_only_that_entry(
    overrides: dict[str, object],
    reason: str,
) -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD", **overrides),
    )
    assert not result.accepted
    assert reason in result.reasons


@pytest.mark.parametrize("max_slippage", (-1, 0, 17, 21, 101, 1.5, True))
def test_invalid_slippage_contract_fails_closed(max_slippage: object) -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD"),
        max_slippage_points=max_slippage,  # type: ignore[arg-type]
    )
    assert not result.accepted
    assert "scalp_broker_entry_max_slippage_invalid" in result.reasons


def test_broker_stop_floor_widens_geometry_before_risk_sizing() -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.00005,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD", stop_level_points=10.0),
    )
    assert result.accepted
    assert result.plan is not None
    bid = 1.1 - (1.1 * 0.5 / 1e4)
    assert bid - result.plan.sl_price >= 0.00015 - 1e-12
    assert result.plan.stop_distance_price > 0.00005


def test_sell_protection_is_built_from_live_ask_with_transit_cushion() -> None:
    contract = _contract("EURUSD", stop_level_points=40.0)
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="SELL",
        quote_entry_price=1.15044,
        reference_mid=1.15047,
        stop_distance_price=0.00050,
        target_distance_price=0.00100,
        current_spread_bps=(1.15050 - 1.15044) / 1.15047 * 1e4,
        win_probability_lower_bound=0.80,
        contract=contract,
        current_bid=1.15044,
        current_ask=1.15050,
    )

    assert result.accepted, result.reasons
    assert result.plan is not None
    assert result.plan.sl_price - 1.15050 >= 45 * contract.point - 1e-12
    assert 1.15044 - result.plan.tp_price >= 45 * contract.point - 1e-12


def test_buy_protection_is_built_from_live_bid_with_transit_cushion() -> None:
    contract = _contract("EURUSD", stop_level_points=40.0)
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        quote_entry_price=1.15050,
        reference_mid=1.15047,
        stop_distance_price=0.00050,
        target_distance_price=0.00100,
        current_spread_bps=(1.15050 - 1.15044) / 1.15047 * 1e4,
        win_probability_lower_bound=0.80,
        contract=contract,
        current_bid=1.15044,
        current_ask=1.15050,
    )

    assert result.accepted, result.reasons
    assert result.plan is not None
    assert 1.15044 - result.plan.sl_price >= 45 * contract.point - 1e-12
    assert result.plan.tp_price - 1.15050 >= 45 * contract.point - 1e-12


def test_quantized_payoff_must_remain_alive() -> None:
    result = build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="SELL",
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=3.0,
        win_probability_lower_bound=0.20,
        contract=_contract("EURUSD"),
    )
    assert not result.accepted
    assert result.reasons == ("scalp_broker_entry_cost_dead",)


def test_broker_plan_refuses_at_the_exact_t_plus_five_deadline() -> None:
    result = _build_scalp_broker_entry_plan(
        symbol="EURUSD",
        side="BUY",
        execution_type="market",
        pending_orders_forbidden=True,
        entry_deadline_epoch=PLAN_NOW + 5,
        as_of_epoch=PLAN_NOW + 5,
        quote_entry_price=1.1,
        reference_mid=1.1,
        stop_distance_price=0.001,
        target_distance_price=0.002,
        current_spread_bps=0.5,
        win_probability_lower_bound=0.65,
        contract=_contract("EURUSD"),
    )

    assert result.plan is None
    assert result.reasons == ("scalp_broker_entry_deadline_expired",)


def test_production_scalp_wire_is_an_immediate_market_trade_with_full_binding() -> None:
    command = _production_command()
    command = replace(
        command,
        payload={
            **command.payload,
            "expected_strategy_validation_expires_at_epoch": 2_100_000_000.875,
        },
    )
    line = command_to_wire_line(command)

    assert line.startswith("cmd=BUY;symbol=EURUSD")
    assert "execution_type=market" in line
    assert "pending_orders_forbidden=true" in line
    assert f"entry_deadline_epoch={PLAN_NOW + 5}" in line
    assert f"broker_entry_plan_schema={SCALP_BROKER_ENTRY_PLAN_SCHEMA}" in line
    assert "entry_quote_price=1.1" in line
    assert "worst_fill_price=1.1002" in line
    assert "max_slippage_points=20" in line
    assert "protection_cushion_points=5" in line
    assert (
        "expected_strategy_authority_schema="
        "fxstack_production_scalp_authority_v3"
    ) in line
    assert "expected_strategy_admission_mode=signed_validation" in line
    assert "expected_strategy_account_mode=demo" in line
    assert "expected_strategy_generation_id=mtvclc-generation-1" in line
    assert f"expected_strategy_id={MTVCLC_STRATEGY_ID}" in line
    assert f"expected_strategy_version={MTVCLC_STRATEGY_VERSION}" in line
    assert f"expected_strategy_config_id={MTVCLC_CONFIG_ID}" in line
    assert "expected_strategy_registry_generation_id=mtvclc-generation-1" in line
    assert "expected_strategy_registry_revision=11" in line
    assert (
        "expected_strategy_scope_version=fxstack.ig_mt4.scalp_scope.v3"
        in line
    )
    assert "expected_strategy_binding_sha256=" in line
    assert "expected_strategy_validation_expires_at_epoch=2100000000;" in line
    assert "expected_strategy_validation_expires_at_epoch=2100000000.0" not in line
    assert "expected_strategy_authority_revision=7" in line
    for digest_field in (
        "expected_strategy_engine_sha256",
        "expected_strategy_config_sha256",
        "expected_strategy_runtime_release_certificate_sha256",
        "expected_strategy_runtime_release_signing_key_id",
        "expected_strategy_research_evidence_sha256",
        "expected_strategy_research_evidence_signing_key_id",
        "expected_strategy_registry_sha256",
        "expected_strategy_qualification_surface_sha256",
        "expected_strategy_cost_mapping_sha256",
        "expected_strategy_execution_contract_sha256",
    ):
        assert f"{digest_field}=" in line
    assert "intent=PRODUCTION_SCALPER_ENTRY" in line
    assert "expected_broker_contract_broker_symbol=EURUSD.IG" in line
    assert "expected_broker_contract_tick_size=1e-05" in line
    assert "expected_broker_contract_digits=5" in line
    assert "expected_broker_contract_trade_allowed=1" in line
    assert "OP_BUYLIMIT" not in line and "OP_BUYSTOP" not in line


@pytest.mark.parametrize("account_mode", ("demo", "real"))
def test_direct_demo_scalp_wire_is_never_serialized(account_mode: str) -> None:
    command = _production_command()
    direct_demo = replace(
        command,
        payload={
            **command.payload,
            "expected_strategy_admission_mode": "direct_demo",
            "expected_strategy_account_mode": account_mode,
            "expected_account_mode": account_mode,
        },
    )

    with pytest.raises(
        ValueError,
        match="expected_strategy_admission_mode must be signed_validation",
    ):
        command_to_wire_line(direct_demo)


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        ({"execution_type": "pending"}, "execution_type=market"),
        (
            {"pending_orders_forbidden": False},
            "pending_orders_forbidden=true",
        ),
        ({"entry_deadline_epoch": None}, "entry_deadline_epoch"),
        ({"worst_fill_price": None}, "worst_fill_price"),
        ({"entry_price": 1.1003}, "entry_price must equal"),
        ({"max_slippage_points": 17}, "max_slippage_points is incompatible"),
        (
            {"expected_strategy_admission_mode": "unsigned"},
            "expected_strategy_admission_mode must be signed_validation",
        ),
        (
            {"expected_strategy_account_mode": "real"},
            "expected_strategy_account_mode is incompatible",
        ),
        (
            {"expected_strategy_authority_schema": "v2"},
            "expected_strategy_authority_schema is incompatible",
        ),
        (
            {"expected_strategy_scope_version": "v2"},
            "expected_strategy_scope_version is incompatible",
        ),
        (
            {"expected_strategy_binding_sha256": ""},
            "expected_strategy_binding_sha256",
        ),
        (
            {"expected_strategy_registry_revision": 0},
            "expected_strategy_registry_revision is outside the allowed range",
        ),
        (
            {"expected_strategy_validation_expires_at_epoch": "not-an-epoch"},
            "expected_strategy_validation_expires_at_epoch must be numeric",
        ),
        (
            {"expected_strategy_validation_expires_at_epoch": float("nan")},
            "expected_strategy_validation_expires_at_epoch must be a finite",
        ),
        (
            {"expected_strategy_validation_expires_at_epoch": float("inf")},
            "expected_strategy_validation_expires_at_epoch must be a finite",
        ),
        (
            {"expected_strategy_validation_expires_at_epoch": 2_147_483_648.0},
            "expected_strategy_validation_expires_at_epoch is outside the allowed range",
        ),
        (
            {"expected_strategy_registry_generation_id": "wrong-generation"},
            "expected_strategy_registry_generation_id mismatch",
        ),
        (
            {"expected_broker_contract_broker_symbol": "EURUSD.BAD"},
            "broker_entry_plan broker_symbol mismatch",
        ),
        (
            {"expected_broker_contract_trade_allowed": False},
            "trade_allowed",
        ),
    ),
)
def test_production_scalp_wire_refuses_dropped_or_mutated_execution_evidence(
    mutation: dict[str, object],
    message: str,
) -> None:
    command = _production_command()
    mutated_payload = {**command.payload, **mutation}
    with pytest.raises(ValueError, match=message):
        command_to_wire_line(replace(command, payload=mutated_payload))


@pytest.mark.parametrize(
    ("command_intent", "payload_updates"),
    (
        ("ENTRY", {}),
        ("PRODUCTION_SCALPER_ENTRY", {"intent": "ENTRY"}),
        ("PRODUCTION_SCALPER_ENTRY", {"strategy_lane": "legacy"}),
    ),
)
def test_production_scalp_wire_refuses_lane_intent_disagreement(
    command_intent: str,
    payload_updates: dict[str, object],
) -> None:
    command = _production_command()
    payload = {**command.payload, **payload_updates}

    with pytest.raises(ValueError, match="lane and intent must agree"):
        command_to_wire_line(replace(command, intent=command_intent, payload=payload))


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    (
        ("BUY", 1.10008, 1.10010),
        ("SELL", 1.09990, 1.09992),
    ),
)
def test_enqueue_poll_envelope_accepts_only_current_price_inside_instant_bound(
    side: str,
    bid: float,
    ask: float,
) -> None:
    command = _production_command(side)
    assert (
        production_scalp_market_entry_envelope_error(
            command.payload,
            symbol="EURUSD",
            side=side,
            contract=_contract("EURUSD"),
            current_bid=bid,
            current_ask=ask,
        )
        == ""
    )


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    (
        ("BUY", 1.10020, 1.10021),
        ("SELL", 1.09979, 1.09980),
    ),
)
def test_enqueue_poll_envelope_refuses_price_beyond_instant_bound(
    side: str,
    bid: float,
    ask: float,
) -> None:
    command = _production_command(side)
    assert (
        production_scalp_market_entry_envelope_error(
            command.payload,
            symbol="EURUSD",
            side=side,
            contract=_contract("EURUSD"),
            current_bid=bid,
            current_ask=ask,
        )
        == "scalp_market_entry_price_beyond_worst_fill"
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        ({"execution_type": "pending"}, "scalp_market_entry_execution_type_invalid"),
        (
            {"pending_orders_forbidden": False},
            "scalp_market_entry_pending_orders_not_forbidden",
        ),
        ({"entry_deadline_epoch": None}, "scalp_market_entry_deadline_invalid"),
        ({"max_slippage_points": 21}, "scalp_market_entry_max_slippage_invalid"),
        ({"worst_fill_price": 1.10021}, "scalp_market_entry_approved_price_mismatch"),
        ({"sl_price": 1.099205}, "scalp_market_entry_tick_grid_mismatch"),
    ),
)
def test_enqueue_poll_envelope_rejects_mutated_plan(
    mutation: dict[str, object],
    reason: str,
) -> None:
    command = _production_command()
    assert (
        production_scalp_market_entry_envelope_error(
            {**command.payload, **mutation},
            symbol="EURUSD",
            side="BUY",
            contract=_contract("EURUSD"),
            current_bid=1.10008,
            current_ask=1.10010,
        )
        == reason
    )


@pytest.mark.parametrize(
    "field",
    (
        "reference_mid",
        "win_probability_lower_bound",
        "current_spread_bps",
        "p90_spread_bps",
        "commission_bps_per_round_trip",
        "financing_bps_per_trade",
        "adverse_execution_debit_bps",
        "convert_on_close_charge_fraction",
    ),
)
def test_envelope_rejects_top_level_cost_input_drift(field: str) -> None:
    command = _production_command(
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            cost_model_id="frozen-mtvclc-cost-row-2026-08",
            p90_spread_bps=0.9,
            commission_bps_per_round_trip=0.25,
            financing_bps_per_trade=0.15,
            convert_on_close_charge_fraction=0.005,
        )
    )
    payload = dict(command.payload)
    payload[field] = float(payload[field]) + 0.01

    assert (
        production_scalp_market_entry_envelope_error(
            payload,
            symbol="EURUSD",
            side="BUY",
            contract=_contract("EURUSD"),
            current_bid=1.10008,
            current_ask=1.10010,
        )
        == f"scalp_market_entry_plan_{field}_mismatch"
    )


def test_envelope_rejects_cost_model_id_drift() -> None:
    command = _production_command(
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            cost_model_id="frozen-mtvclc-cost-row-2026-08",
            p90_spread_bps=0.9,
            commission_bps_per_round_trip=0.25,
            financing_bps_per_trade=0.15,
            convert_on_close_charge_fraction=0.005,
        )
    )

    assert (
        production_scalp_market_entry_envelope_error(
            {**command.payload, "cost_model_id": "tampered-cost-row"},
            symbol="EURUSD",
            side="BUY",
            contract=_contract("EURUSD"),
            current_bid=1.10008,
            current_ask=1.10010,
        )
        == "scalp_market_entry_plan_cost_model_id_mismatch"
    )


def test_envelope_rejects_nested_cost_input_drift() -> None:
    command = _production_command(
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            cost_model_id="frozen-mtvclc-cost-row-2026-08",
            p90_spread_bps=0.9,
            commission_bps_per_round_trip=0.25,
            financing_bps_per_trade=0.15,
            convert_on_close_charge_fraction=0.005,
        )
    )
    plan = dict(command.payload["broker_entry_plan"])
    plan["adverse_execution_debit_bps"] = 9.0

    assert (
        production_scalp_market_entry_envelope_error(
            {**command.payload, "broker_entry_plan": plan},
            symbol="EURUSD",
            side="BUY",
            contract=_contract("EURUSD"),
            current_bid=1.10008,
            current_ask=1.10010,
        )
        == "scalp_market_entry_plan_adverse_execution_debit_bps_mismatch"
    )


def test_envelope_recomputes_formula_when_both_cost_copies_are_tampered() -> None:
    command = _production_command(
        cost_model=ScalpBrokerEntryCostModel.mtvclc(
            cost_model_id="frozen-mtvclc-cost-row-2026-08",
            p90_spread_bps=0.9,
            commission_bps_per_round_trip=0.25,
            financing_bps_per_trade=0.15,
            convert_on_close_charge_fraction=0.005,
        )
    )
    plan = dict(command.payload["broker_entry_plan"])
    plan["commission_bps_per_round_trip"] = 0.75
    payload = {
        **command.payload,
        "commission_bps_per_round_trip": 0.75,
        "broker_entry_plan": plan,
    }

    assert (
        production_scalp_market_entry_envelope_error(
            payload,
            symbol="EURUSD",
            side="BUY",
            contract=_contract("EURUSD"),
            current_bid=1.10008,
            current_ask=1.10010,
        )
        == "scalp_market_entry_fixed_non_spread_cost_bps_inconsistent"
    )
