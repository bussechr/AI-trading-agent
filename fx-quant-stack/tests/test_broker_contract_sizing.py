from __future__ import annotations

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_CRYPTO_CFD_SYMBOLS,
    IG_MT4_SCALP_CATALOG,
    IG_MT4_SCALP_SYMBOLS,
)
from fxstack.risk.sizing import (
    BrokerContractSpec,
    lots_for_broker_contract,
)


QUOTE_RATES = {
    "EURUSD": 1.10,
    "GBPUSD": 1.25,
    "AUDUSD": 0.65,
    "NZDUSD": 0.60,
    "USDJPY": 150.0,
    "USDCAD": 1.35,
    "USDCHF": 0.90,
}


def _contract(symbol: str, **overrides) -> BrokerContractSpec:
    crypto = symbol in IG_MT4_CRYPTO_CFD_SYMBOLS
    point = 0.01 if crypto else (0.001 if symbol.endswith("JPY") else 0.00001)
    values = {
        "symbol": symbol,
        "broker_symbol": f"{symbol}.IG",
        "lot_size": 1.0 if crypto else 100_000.0,
        "min_lot": 0.01,
        "lot_step": 0.01,
        "max_lot": 100.0,
        "point": point,
        "digits": 2 if crypto else (3 if symbol.endswith("JPY") else 5),
        "tick_size": point,
        "tick_value": 1.0,
        "stop_level_points": 10.0,
        "freeze_level_points": 0.0,
        "trade_allowed": True,
        "margin_required": 100.0,
    }
    values.update(overrides)
    return BrokerContractSpec(**values)


@pytest.mark.parametrize("symbol", IG_MT4_SCALP_SYMBOLS)
def test_every_ig_symbol_sizes_from_its_broker_contract(symbol: str) -> None:
    instrument = IG_MT4_SCALP_CATALOG[symbol]
    if instrument.asset_class == "crypto":
        entry, stop = 1_000.0, 900.0
    elif symbol.endswith("JPY"):
        entry, stop = 150.0, 149.50
    else:
        entry, stop = 1.10, 1.095

    result = lots_for_broker_contract(
        pair=symbol,
        equity=10_000.0,
        available_margin=9_000.0,
        risk_fraction=0.005,
        entry_price=entry,
        stop_price=stop,
        contract=_contract(symbol),
        rates=QUOTE_RATES,
        margin_utilization_cap=0.25,
    )

    assert result.ok, (symbol, result)
    assert result.lots >= 0.01
    assert result.money_at_risk <= 50.0 + 1e-6
    assert result.effective_risk_fraction <= 0.005 + 1e-9


def test_crypto_uses_one_unit_contract_not_fx_assumption() -> None:
    result = lots_for_broker_contract(
        pair="BTCUSD",
        equity=10_000.0,
        available_margin=9_000.0,
        risk_fraction=0.005,
        entry_price=1_000.0,
        stop_price=900.0,
        contract=_contract("BTCUSD"),
        rates=QUOTE_RATES,
    )
    assert result.ok
    assert result.value_per_price_unit == pytest.approx(1.0)
    assert result.lots == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"broker_symbol": ""}, "broker_contract_symbol_unresolved"),
        ({"lot_size": 0.0}, "broker_contract_lot_size_invalid"),
        ({"min_lot": 0.0}, "broker_contract_min_lot_invalid"),
        ({"lot_step": 0.0}, "broker_contract_lot_step_invalid"),
        ({"max_lot": 0.0}, "broker_contract_max_lot_invalid"),
        ({"point": 0.0}, "broker_contract_point_invalid"),
        ({"digits": 0}, "broker_contract_digits_invalid"),
        ({"point": 0.001}, "broker_contract_point_digits_mismatch"),
        ({"tick_size": 0.0}, "broker_contract_tick_size_invalid"),
        ({"tick_size": 0.000015}, "broker_contract_tick_size_point_mismatch"),
        ({"tick_value": 0.0}, "broker_contract_tick_value_invalid"),
        ({"stop_level_points": -1.0}, "broker_contract_stop_level_invalid"),
        ({"freeze_level_points": -1.0}, "broker_contract_freeze_level_invalid"),
        ({"margin_required": 0.0}, "broker_contract_margin_invalid"),
        ({"trade_allowed": False}, "broker_contract_trade_not_allowed"),
    ],
)
def test_malformed_broker_contract_fails_closed(overrides, reason: str) -> None:
    result = lots_for_broker_contract(
        pair="EURUSD",
        equity=10_000.0,
        available_margin=9_000.0,
        risk_fraction=0.005,
        entry_price=1.10,
        stop_price=1.095,
        contract=_contract("EURUSD", **overrides),
        rates=QUOTE_RATES,
    )
    assert not result.ok
    assert result.reason == reason


def test_broker_minimum_stop_is_a_geometry_veto() -> None:
    result = lots_for_broker_contract(
        pair="EURUSD",
        equity=10_000.0,
        available_margin=9_000.0,
        risk_fraction=0.005,
        entry_price=1.10000,
        stop_price=1.09995,
        contract=_contract("EURUSD", stop_level_points=10.0),
        rates=QUOTE_RATES,
    )
    assert not result.ok
    assert result.reason == "stop_below_broker_minimum"


def test_margin_cap_rounds_down_with_broker_quantum() -> None:
    result = lots_for_broker_contract(
        pair="EURUSD",
        equity=10_000.0,
        available_margin=1_000.0,
        risk_fraction=0.02,
        entry_price=1.1000,
        stop_price=1.0990,
        contract=_contract("EURUSD", margin_required=1_000.0),
        rates=QUOTE_RATES,
        margin_utilization_cap=0.01,
    )
    assert result.ok
    assert result.margin_capped is True
    assert result.lots == pytest.approx(0.1)
    assert result.margin_required == pytest.approx(100.0)


def test_missing_cross_conversion_refuses_instead_of_guessing() -> None:
    result = lots_for_broker_contract(
        pair="EURJPY",
        equity=10_000.0,
        available_margin=9_000.0,
        risk_fraction=0.005,
        entry_price=160.0,
        stop_price=159.5,
        contract=_contract("EURJPY"),
        rates={"EURUSD": 1.10},
    )
    assert not result.ok
    assert result.reason == "conversion_unresolvable"


def test_mapping_parser_preserves_broker_symbol_and_geometry() -> None:
    spec = BrokerContractSpec.from_mapping(
        symbol="ETHUSD",
        payload={
            "broker_symbol": "ETHUSD.d",
            "lot_size": 1,
            "min_lot": 0.1,
            "lot_step": 0.1,
            "max_lot": 20,
            "point": 0.01,
            "digits": 2,
            "tick_size": 0.01,
            "tick_value": 1.0,
            "stop_level_points": 25,
            "freeze_level_points": 0,
            "trade_allowed": True,
            "margin_required": 75,
        },
    )
    assert spec.validation_error(expected_symbol="ETHUSD") == ""
    assert spec.broker_symbol == "ETHUSD.d"
    assert spec.lot_size == pytest.approx(1.0)
    assert spec.tick_size == pytest.approx(0.01)
    assert spec.trade_allowed is True
