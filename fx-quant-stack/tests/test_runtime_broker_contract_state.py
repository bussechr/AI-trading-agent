from __future__ import annotations

from datetime import UTC, datetime

import pytest

import fxstack.runtime.broker_contract_state as broker_contract_state
from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_CRYPTO_CFD_SYMBOLS,
    IG_MT4_SCALP_SYMBOLS,
)
from fxstack.runtime.broker_contract_state import (
    BROKER_CONTRACT_STATE_SCHEMA,
    MT4_MARKET_ENTRY_MAX_SLIPPAGE_POINTS,
    PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION,
    broker_contract_binding_sha256,
    broker_contract_command_binding_error,
    broker_contract_market_entry_fields,
    broker_contract_order_cash_risk_error,
    broker_contract_order_geometry_error,
    broker_contract_sizing_metadata,
    project_ig_mt4_authority_contract_universe,
    project_ig_mt4_contract_universe,
    project_ig_mt4_selected_contract_universe,
)
from fxstack.runtime.market_source_identity import build_authenticated_market_source


NOW = 1_800_000_000.0
PRODUCER_IDENTITY = "ig-mt4-production-ea"
PRODUCER_INSTANCE_ID = "ig-mt4-terminal-instance-1"
TERMINAL_LEASE_SCOPE = "ig-mt4-terminal-scope"
CREDENTIAL_GENERATION_ID = "ig-mt4-generation-1"


def _market_source(
    producer_instance_id: str = PRODUCER_INSTANCE_ID,
):
    source = build_authenticated_market_source(
        broker_account_scope="ig-demo-scope",
        broker_venue_id="ig_mt4",
        producer_identity=PRODUCER_IDENTITY,
        producer_instance_id=producer_instance_id,
        terminal_lease_scope=TERMINAL_LEASE_SCOPE,
        credential_generation_id=CREDENTIAL_GENERATION_ID,
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert source is not None
    return source


def _spec(symbol: str) -> dict[str, object]:
    crypto = symbol in IG_MT4_CRYPTO_CFD_SYMBOLS
    point = 0.01 if crypto else (0.001 if symbol.endswith("JPY") else 0.00001)
    return {
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
        "margin_required": 50.0 if crypto else 1_000.0,
        "trade_allowed": True,
    }


def _state() -> dict[str, object]:
    market_source = _market_source()
    return {
        "broker_venue_id": "ig_mt4",
        "broker_account_scope": "ig-demo-scope",
        "broker_account_currency": "USD",
        "bridge_producer_identity": PRODUCER_IDENTITY,
        "bridge_producer_instance_id": PRODUCER_INSTANCE_ID,
        "bridge_terminal_lease_scope": TERMINAL_LEASE_SCOPE,
        "bridge_credential_generation_id": CREDENTIAL_GENERATION_ID,
        "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
        "bridge_consumer_lease": {
            "schema_version": "fxstack_bridge_consumer_lease_v1",
            "consumer_identity": PRODUCER_IDENTITY,
            "producer_instance_id": PRODUCER_INSTANCE_ID,
            "terminal_lease_scope": TERMINAL_LEASE_SCOPE,
            "credential_generation_id": CREDENTIAL_GENERATION_ID,
            "bridge_protocol_version": BRIDGE_PROTOCOL_VERSION,
            "expires_at": NOW + 120.0,
        },
        "bridge_market_source": market_source.to_fields(),
        "freemargin": 9_000.0,
        "symbol_specs_ts": datetime.fromtimestamp(NOW - 2.0, tz=UTC).isoformat(),
        "symbol_specs": {symbol: _spec(symbol) for symbol in IG_MT4_SCALP_SYMBOLS},
        "symbol_specs_market_source": market_source.to_fields(),
        "symbol_specs_market_source_id": market_source.source_id,
    }


def _cash_risk_payload(
    *,
    universe,
    symbol: str,
    entry_price: float,
    stop_price: float,
    lots: float,
    value_per_price_unit: float,
    equity: float = 10_000.0,
) -> dict[str, object]:
    contract = universe.contract_for(symbol)
    assert contract is not None
    money_at_risk = (
        float(lots)
        * abs(float(entry_price) - float(stop_price))
        * float(value_per_price_unit)
    )
    risk_fraction = money_at_risk / float(equity)
    return {
        **broker_contract_sizing_metadata(
            universe,
            symbol=symbol,
            margin_utilization_cap=0.25,
        ),
        "lots": float(lots),
        "entry_price": float(entry_price),
        "sl_price": float(stop_price),
        "broker_contract_sizing": {
            "required": True,
            "status": "approved",
            "symbol": symbol,
            "broker_symbol": contract.broker_symbol,
            "account_currency": universe.account_currency,
            "lots": float(lots),
            "money_at_risk": float(money_at_risk),
            "value_per_price_unit": float(value_per_price_unit),
            "requested_risk_fraction": float(risk_fraction),
            "budgeted_risk_fraction": float(risk_fraction),
            "effective_risk_fraction": float(risk_fraction),
        },
    }


def test_projects_exact_fresh_22_symbol_contract_snapshot() -> None:
    universe = project_ig_mt4_contract_universe(_state(), now_ts=NOW, max_age_secs=30.0)

    assert universe.ok
    assert tuple(universe.contracts) == IG_MT4_SCALP_SYMBOLS
    assert universe.account_currency == "USD"
    assert universe.contract_for("BTCUSD").lot_size == pytest.approx(1.0)
    assert universe.contract_for("EURUSD").lot_size == pytest.approx(100_000.0)

    metadata = broker_contract_sizing_metadata(
        universe,
        symbol="BTCUSD",
        margin_utilization_cap=0.25,
        quote_rates={"USDJPY": 145.0},
    )
    assert metadata["broker_contract_required"] is True
    assert metadata["broker_contract_state_schema"] == BROKER_CONTRACT_STATE_SCHEMA
    assert metadata["broker_contract_spec"]["lot_size"] == pytest.approx(1.0)
    assert metadata["broker_contract_available_margin"] == pytest.approx(9_000.0)
    assert metadata["quote_rates"] == {"USDJPY": 145.0}
    assert len(metadata["expected_broker_contract_binding_sha256"]) == 64
    assert metadata["expected_broker_contract_broker_symbol"] == "BTCUSD.IG"
    assert metadata["expected_broker_contract_tick_size"] == pytest.approx(0.01)
    assert metadata["expected_broker_contract_digits"] == 2
    assert metadata["expected_broker_contract_trade_allowed"] is True
    assert (
        broker_contract_command_binding_error(
            metadata,
            universe=universe,
            symbol="BTCUSD",
        )
        == ""
    )

    command = {
        **metadata,
        "lots": 0.1,
        "sl_price": 99.0,
    }
    assert (
        broker_contract_order_geometry_error(
            command,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
        )
        == ""
    )


def test_contract_projection_reuses_exact_rows_and_detects_same_timestamp_drift() -> (
    None
):
    broker_contract_state._cached_broker_contract_spec.cache_clear()
    first = project_ig_mt4_contract_universe(
        _state(),
        now_ts=NOW,
        max_age_secs=30.0,
    )
    first_cache = broker_contract_state._cached_broker_contract_spec.cache_info()
    second = project_ig_mt4_contract_universe(
        _state(),
        now_ts=NOW,
        max_age_secs=30.0,
    )
    second_cache = broker_contract_state._cached_broker_contract_spec.cache_info()
    drifted_state = _state()
    drifted_state["symbol_specs"]["EURUSD"]["margin_required"] = 1_250.0
    drifted = project_ig_mt4_contract_universe(
        drifted_state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    drifted_cache = broker_contract_state._cached_broker_contract_spec.cache_info()

    assert first.ok and second.ok and drifted.ok
    assert first_cache.misses == len(IG_MT4_SCALP_SYMBOLS)
    assert second_cache.misses == first_cache.misses
    assert second_cache.hits == first_cache.hits + len(IG_MT4_SCALP_SYMBOLS)
    assert drifted_cache.misses == second_cache.misses + 1
    assert drifted.contract_for("EURUSD").margin_required == pytest.approx(1_250.0)


def test_contract_projection_custom_mapping_bypasses_row_cache() -> None:
    class CustomSpec(dict[str, object]):
        pass

    broker_contract_state._cached_broker_contract_spec.cache_clear()
    state = _state()
    state["symbol_specs"]["EURUSD"] = CustomSpec(
        state["symbol_specs"]["EURUSD"]
    )
    first = project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    first_cache = broker_contract_state._cached_broker_contract_spec.cache_info()
    state["symbol_specs"]["EURUSD"]["margin_required"] = 1_500.0
    second = project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    second_cache = broker_contract_state._cached_broker_contract_spec.cache_info()

    assert first.ok and second.ok
    assert first_cache.misses == len(IG_MT4_SCALP_SYMBOLS) - 1
    assert second_cache.misses == first_cache.misses
    assert second.contract_for("EURUSD").margin_required == pytest.approx(1_500.0)


def test_authority_projection_keeps_tradeability_symbol_scoped() -> None:
    state = _state()
    state["symbol_specs"]["BTCUSD"]["trade_allowed"] = False

    strict = project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    authority = project_ig_mt4_authority_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    selected_btc = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("BTCUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )
    selected_fx = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    assert strict.errors == ("broker_contract_trade_not_allowed:BTCUSD",)
    assert authority.ok
    assert len(authority.contracts) == len(IG_MT4_SCALP_SYMBOLS)
    assert authority.contract_for("BTCUSD").trade_allowed is False
    assert selected_btc.errors == ("broker_contract_trade_not_allowed:BTCUSD",)
    assert selected_fx.ok

    malformed = _state()
    malformed["symbol_specs"]["BTCUSD"].update(
        trade_allowed=False,
        lot_size=0.0,
    )
    malformed_authority = project_ig_mt4_authority_contract_universe(
        malformed,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    assert malformed_authority.errors == (
        "broker_contract_lot_size_invalid:BTCUSD",
    )


def test_command_binding_tracks_execution_geometry_not_volatile_tick_value() -> None:
    baseline = _state()
    first = project_ig_mt4_contract_universe(
        baseline,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    first_hash = broker_contract_binding_sha256(first, symbol="BTCUSD")

    valuation_move = _state()
    valuation_move["symbol_specs"]["BTCUSD"]["tick_value"] = 1.25
    second = project_ig_mt4_contract_universe(
        valuation_move,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    assert broker_contract_binding_sha256(second, symbol="BTCUSD") == first_hash

    geometry_move = _state()
    geometry_move["symbol_specs"]["BTCUSD"]["tick_size"] = 0.02
    third = project_ig_mt4_contract_universe(
        geometry_move,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    assert broker_contract_binding_sha256(third, symbol="BTCUSD") != first_hash

def test_command_binding_and_order_geometry_fail_closed() -> None:
    universe = project_ig_mt4_contract_universe(_state(), now_ts=NOW, max_age_secs=30.0)
    metadata = broker_contract_sizing_metadata(
        universe,
        symbol="BTCUSD",
        margin_utilization_cap=0.25,
    )

    drifted = {**metadata, "expected_broker_contract_broker_symbol": "BTCUSD.BAD"}
    assert (
        broker_contract_command_binding_error(
            drifted,
            universe=universe,
            symbol="BTCUSD",
        )
        == "expected_broker_contract_broker_symbol_changed"
    )

    bad_step = {**metadata, "lots": 0.015, "sl_price": 99.0}
    assert (
        broker_contract_order_geometry_error(
            bad_step,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
        )
        == "broker_contract_order_lot_step_mismatch"
    )

    bad_stop = {**metadata, "lots": 0.1, "sl_price": 99.95}
    assert (
        broker_contract_order_geometry_error(
            bad_stop,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
        )
        == "broker_contract_order_stop_below_minimum"
    )


def test_cash_risk_recheck_accepts_unchanged_quote_and_lot_reduction() -> None:
    universe = project_ig_mt4_contract_universe(_state(), now_ts=NOW, max_age_secs=30.0)
    payload = _cash_risk_payload(
        universe=universe,
        symbol="BTCUSD",
        entry_price=100.0,
        stop_price=99.0,
        lots=0.1,
        value_per_price_unit=1.0,
    )

    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == ""
    )
    reduced = {**payload, "lots": 0.05}
    assert (
        broker_contract_order_cash_risk_error(
            reduced,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == ""
    )


def test_cash_risk_recheck_blocks_quote_equity_and_proof_drift() -> None:
    universe = project_ig_mt4_contract_universe(_state(), now_ts=NOW, max_age_secs=30.0)
    payload = _cash_risk_payload(
        universe=universe,
        symbol="BTCUSD",
        entry_price=100.0,
        stop_price=99.0,
        lots=0.1,
        value_per_price_unit=1.0,
    )

    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="BTCUSD",
            entry_price=101.0,
            equity=10_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_exceeded"
    )
    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=5_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_exceeded"
    )
    assert (
        broker_contract_order_cash_risk_error(
            {
                key: value
                for key, value in payload.items()
                if key != "broker_contract_sizing"
            },
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_proof_missing"
    )
    malformed = {
        **payload,
        "broker_contract_sizing": {
            **dict(payload["broker_contract_sizing"]),
            "money_at_risk": 1_000.0,
        },
    }
    assert (
        broker_contract_order_cash_risk_error(
            malformed,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_proof_mismatch"
    )


def test_cash_risk_recheck_enforces_equity_relative_production_scalp_hard_cap() -> None:
    universe = project_ig_mt4_contract_universe(
        _state(), now_ts=NOW, max_age_secs=30.0
    )
    viable = _cash_risk_payload(
        universe=universe,
        symbol="BTCUSD",
        entry_price=100.0,
        stop_price=90.0,
        lots=0.2,
        value_per_price_unit=1.0,
    )

    assert PRODUCTION_SCALP_MAX_CASH_RISK_FRACTION == pytest.approx(0.005)
    assert (
        broker_contract_order_cash_risk_error(
            viable,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == ""
    )
    oversized = _cash_risk_payload(
        universe=universe,
        symbol="BTCUSD",
        entry_price=100.0,
        stop_price=40.0,
        lots=1.0,
        value_per_price_unit=1.0,
    )
    assert (
        broker_contract_order_cash_risk_error(
            oversized,
            universe=universe,
            symbol="BTCUSD",
            entry_price=100.0,
            equity=10_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_hard_cap_exceeded"
    )


def test_cash_risk_recheck_revalues_current_cross_currency_rate() -> None:
    universe = project_ig_mt4_contract_universe(_state(), now_ts=NOW, max_age_secs=30.0)
    payload = _cash_risk_payload(
        universe=universe,
        symbol="EURJPY",
        entry_price=160.0,
        stop_price=159.9,
        lots=0.01,
        value_per_price_unit=1_000.0,
    )

    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="EURJPY",
            entry_price=160.0,
            equity=10_000.0,
            quote_rates={"USDJPY": 100.0},
        )
        == ""
    )
    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="EURJPY",
            entry_price=160.0,
            equity=10_000.0,
            quote_rates={"USDJPY": 50.0},
        )
        == "broker_contract_order_cash_risk_exceeded"
    )
    assert (
        broker_contract_order_cash_risk_error(
            payload,
            universe=universe,
            symbol="EURJPY",
            entry_price=160.0,
            equity=10_000.0,
            quote_rates={},
        )
        == "broker_contract_order_cash_risk_conversion_unresolvable"
    )


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (
            lambda state: state.update(broker_venue_id=""),
            "broker_contract_ig_mt4_venue_unattested",
        ),
        (
            lambda state: state.update(broker_account_currency=""),
            "broker_contract_account_currency_unattested",
        ),
        (
            lambda state: state.update(freemargin=0.0),
            "broker_contract_free_margin_unattested",
        ),
        (
            lambda state: state.update(symbol_specs_ts=""),
            "broker_contract_specs_timestamp_missing",
        ),
        (
            lambda state: state.update(
                symbol_specs_ts=datetime.fromtimestamp(NOW - 60.0, tz=UTC).isoformat()
            ),
            "broker_contract_specs_stale",
        ),
    ],
)
def test_refuses_unattested_or_stale_state(mutation, expected_error: str) -> None:
    state = _state()
    mutation(state)
    universe = project_ig_mt4_contract_universe(state, now_ts=NOW, max_age_secs=30.0)

    assert not universe.ok
    assert expected_error in universe.errors


def test_one_missing_or_malformed_symbol_invalidates_whole_scope() -> None:
    missing = _state()
    missing["symbol_specs"].pop("NZDJPY")
    missing_universe = project_ig_mt4_contract_universe(
        missing, now_ts=NOW, max_age_secs=30.0
    )
    assert "broker_contract_spec_missing:NZDJPY" in missing_universe.errors

    malformed = _state()
    malformed["symbol_specs"]["BTCUSD"]["lot_size"] = 0.0
    malformed_universe = project_ig_mt4_contract_universe(
        malformed, now_ts=NOW, max_age_secs=30.0
    )
    assert "broker_contract_lot_size_invalid:BTCUSD" in malformed_universe.errors


def test_incomplete_required_scope_is_never_a_valid_scalper_snapshot() -> None:
    universe = project_ig_mt4_contract_universe(
        _state(),
        now_ts=NOW,
        max_age_secs=30.0,
        required_symbols=IG_MT4_SCALP_SYMBOLS[:-1],
    )

    assert not universe.ok
    assert "broker_contract_symbol_scope_incomplete" in universe.errors


def test_selected_contract_scope_isolates_an_unselected_malformed_symbol() -> None:
    state = _state()
    state["symbol_specs"]["NZDJPY"]["lot_size"] = 0.0

    aggregate = project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    selected = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )
    malformed = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("NZDJPY",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    assert "broker_contract_lot_size_invalid:NZDJPY" in aggregate.errors
    assert selected.ok, selected.errors
    assert tuple(selected.contracts) == ("EURUSD",)
    assert malformed.ok is False
    assert "broker_contract_lot_size_invalid:NZDJPY" in malformed.errors


@pytest.mark.parametrize(
    ("side", "expected_quote", "expected_worst"),
    [
        ("BUY", 1.10000, 1.10020),
        ("SELL", 1.09990, 1.09970),
    ],
)
def test_market_entry_fields_bind_side_quote_and_adverse_fill(
    side: str,
    expected_quote: float,
    expected_worst: float,
) -> None:
    universe = project_ig_mt4_selected_contract_universe(
        _state(),
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    fields, error = broker_contract_market_entry_fields(
        universe,
        symbol="EURUSD",
        side=side,
        bid=1.09990,
        ask=1.10000,
    )

    assert error == ""
    assert fields["execution_type"] == "market"
    assert fields["pending_orders_forbidden"] is True
    assert fields["entry_quote_price"] == pytest.approx(expected_quote)
    assert fields["entry_price"] == pytest.approx(expected_worst)
    assert fields["worst_fill_price"] == pytest.approx(expected_worst)
    assert (
        fields["max_slippage_points"]
        == MT4_MARKET_ENTRY_MAX_SLIPPAGE_POINTS
    )
    assert fields["expected_broker_contract_symbol"] == "EURUSD"
    assert fields["expected_broker_contract_broker_symbol"] == "EURUSD.IG"
    assert fields["expected_broker_contract_trade_allowed"] is True


@pytest.mark.parametrize(
    ("side", "bid", "ask", "expected_error"),
    [
        ("HOLD", 1.09990, 1.10000, "broker_contract_market_entry_side_invalid"),
        (
            "BUY",
            1.09990,
            1.100001,
            "broker_contract_market_entry_quote_off_grid",
        ),
        (
            "SELL",
            1.10000,
            1.09990,
            "broker_contract_market_entry_quote_invalid",
        ),
    ],
)
def test_market_entry_fields_refuse_invalid_side_or_quote(
    side: str,
    bid: float,
    ask: float,
    expected_error: str,
) -> None:
    universe = project_ig_mt4_selected_contract_universe(
        _state(),
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    fields, error = broker_contract_market_entry_fields(
        universe,
        symbol="EURUSD",
        side=side,
        bid=bid,
        ask=ask,
    )

    assert fields == {}
    assert error == expected_error


@pytest.mark.parametrize(
    ("selected_symbols", "expected_error"),
    [
        ((), "broker_contract_selected_scope_invalid"),
        (
            ("EURUSD", "EURUSD"),
            "broker_contract_selected_scope_invalid",
        ),
        (
            ("XAUUSD",),
            "broker_contract_selected_symbol_unsupported:XAUUSD",
        ),
    ],
)
def test_selected_contract_scope_rejects_invalid_catalog_selections(
    selected_symbols: tuple[str, ...],
    expected_error: str,
) -> None:
    universe = project_ig_mt4_selected_contract_universe(
        _state(),
        selected_symbols=selected_symbols,
        now_ts=NOW,
        max_age_secs=30.0,
    )

    assert universe.ok is False
    assert expected_error in universe.errors


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        (
            lambda state: state.update(symbol_specs_market_source_id="0" * 64),
            "broker_contract_specs_market_source_id_mismatch",
        ),
        (
            lambda state: state["symbol_specs_market_source"].update(
                producer_instance_id="foreign-terminal"
            ),
            "broker_contract_specs_market_source_invalid:market_source_id_invalid",
        ),
        (
            lambda state: state.update(bridge_market_source={}),
            "broker_contract_bridge_market_source_invalid:market_source_unauthenticated",
        ),
    ],
)
def test_contract_projection_globally_rejects_market_source_drift(
    mutation,
    expected_error: str,
) -> None:
    state = _state()
    mutation(state)

    aggregate = project_ig_mt4_contract_universe(
        state,
        now_ts=NOW,
        max_age_secs=30.0,
    )
    selected = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    assert expected_error in aggregate.errors
    assert expected_error in selected.errors


def test_contract_projection_rejects_valid_but_foreign_specs_source() -> None:
    state = _state()
    foreign_source = _market_source("foreign-terminal-instance")
    state["symbol_specs_market_source"] = foreign_source.to_fields()
    state["symbol_specs_market_source_id"] = foreign_source.source_id

    selected = project_ig_mt4_selected_contract_universe(
        state,
        selected_symbols=("EURUSD",),
        now_ts=NOW,
        max_age_secs=30.0,
    )

    assert "broker_contract_specs_market_source_mismatch" in selected.errors
    assert "broker_contract_specs_market_source_id_mismatch" in selected.errors
