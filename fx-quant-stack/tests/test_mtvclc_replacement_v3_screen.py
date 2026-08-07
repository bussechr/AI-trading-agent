from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import asdict
from datetime import UTC, datetime

import pytest
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation as base,
)
from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation_replacement_v3 as screen,
)


def _cost(symbol: str) -> base.MT4CostCalibration:
    pnl_currency = symbol[3:]
    return base.MT4CostCalibration(
        symbol=symbol,
        p90_spread_bps=1.0,
        commission_bps_per_round_trip=0.0,
        financing_bps_per_trade=0.0,
        account_currency="USD",
        pnl_currency=pnl_currency,
        convert_on_close_charge_fraction=(0.0 if pnl_currency == "USD" else 0.005),
        source_sha256=hashlib.sha256(f"cost:{symbol}".encode()).hexdigest(),
    )


def _valid_result() -> dict:
    costs = {symbol: _cost(symbol) for symbol in IG_MT4_SCALP_SYMBOLS}
    symbol = "EURUSD"
    cost = costs[symbol]
    signal_epoch = int(datetime(2026, 1, 2, 12, 0, tzinfo=UTC).timestamp())
    reservation = {
        "config_id": base.CONFIG_ID,
        "symbol": symbol,
        "side": "BUY",
        "signal_index": base.BASELINE_M1_BARS,
        "signal_epoch": signal_epoch,
        "expected_entry_epoch": signal_epoch + 60,
        "entry_day": "2026-01-02",
        "volume_v90": 100.0,
        "signal_tick_volume": 101,
        "bid_body_bps": 2.0,
        "bid_close_location": 0.9,
        "p90_spread_bps": cost.p90_spread_bps,
        "recorded_cost_bps": cost.recorded_cost_bps,
        "convert_on_close_charge_fraction": cost.convert_on_close_charge_fraction,
        "target_bps": base.TARGET_COST_MULTIPLE * cost.recorded_cost_bps,
        "stop_bps": base.STOP_COST_MULTIPLE * cost.recorded_cost_bps,
        "p_star": cost.break_even_win_probability,
        "entry_status": "contemporaneous_entry_quote_missing",
    }
    gross = -reservation["stop_bps"]
    conversion = abs(gross) * cost.convert_on_close_charge_fraction
    outcome = {
        "config_id": base.CONFIG_ID,
        "symbol": symbol,
        "side": "BUY",
        "signal_epoch": signal_epoch,
        "entry_day": "2026-01-02",
        "entry_epoch": None,
        "exit_epoch": None,
        "entry_price": None,
        "exit_price": None,
        "exit_reason": "ENTRY_QUOTE_MISSING",
        "full_target_hit_first": False,
        "gross_quote_bps": gross,
        "recorded_cost_bps": cost.recorded_cost_bps,
        "currency_conversion_debit_bps": conversion,
        "net_bps": gross - cost.recorded_cost_bps - conversion,
    }
    cost_rows = {symbol: asdict(value) for symbol, value in costs.items()}
    ready = {symbol: True for symbol in IG_MT4_SCALP_SYMBOLS}
    cells = screen.recompute_cells_from_ledgers(
        reservation_ledger=[reservation],
        outcome_ledger=[outcome],
        costs=cost_rows,
        source_ready_by_symbol=ready,
    )
    assert cells is not None
    return {
        "schema_version": screen.SCREEN_RESULT_SCHEMA,
        "strategy_id": base.STRATEGY_ID,
        "strategy_version": base.STRATEGY_VERSION,
        "config_ids": [base.CONFIG_ID],
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "source_contract_id": base.SOURCE_CONTRACT_ID,
        "activity_metric_id": base.ACTIVITY_METRIC_ID,
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_786,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_830,
        },
        "source_scope_ready": True,
        "source_ready_by_symbol": ready,
        "source_sha256_by_symbol": {
            symbol: hashlib.sha256(f"source:{symbol}".encode()).hexdigest()
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        "source_errors": [],
        "costs": cost_rows,
        "cells": cells,
        "reservation_ledger": [reservation],
        "outcome_ledger": [outcome],
        "all_cells_pass_fixed_screen": False,
        "attempt_manifest": screen.attempt_manifest(),
        "research_only": True,
        "success_claim_authorized": False,
        "holdout_access_authorized": False,
        "promotion_authorized": False,
        "activation_authorized": False,
        "registry_write_authorized": False,
        "runtime_authorized": False,
        "order_authorized": False,
    }


def test_v3_recomputes_all_cells_exclusively_from_complete_ledgers() -> None:
    result = _valid_result()

    assert screen.validate_result_bundle(result) is True
    assert result["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    assert list(result["source_ready_by_symbol"]) == list(IG_MT4_SCALP_SYMBOLS)
    assert "XRPUSD" not in result["symbol_scope"]


@pytest.mark.parametrize(
    "mutation",
    (
        "empty",
        "missing",
        "duplicate_reservation",
        "duplicate_outcome",
        "inconsistent_outcome",
        "fabricated_cell",
        "reordered_scope",
    ),
)
def test_v3_rejects_empty_missing_duplicate_inconsistent_or_fabricated_ledgers(
    mutation: str,
) -> None:
    result = deepcopy(_valid_result())
    if mutation == "empty":
        result["reservation_ledger"] = []
        result["outcome_ledger"] = []
    elif mutation == "missing":
        result.pop("outcome_ledger")
    elif mutation == "duplicate_reservation":
        result["reservation_ledger"].append(deepcopy(result["reservation_ledger"][0]))
    elif mutation == "duplicate_outcome":
        result["outcome_ledger"].append(deepcopy(result["outcome_ledger"][0]))
    elif mutation == "inconsistent_outcome":
        result["outcome_ledger"][0]["net_bps"] += 1.0
    elif mutation == "fabricated_cell":
        result["cells"][0]["wins"] = 999
    elif mutation == "reordered_scope":
        result["symbol_scope"] = list(reversed(result["symbol_scope"]))
    else:  # pragma: no cover
        raise AssertionError(mutation)

    assert screen.validate_result_bundle(result) is False


def test_v3_manifest_preserves_immediate_market_pending_forbidden_contract() -> None:
    configuration = screen.attempt_manifest()["configuration"]

    assert configuration["execution_type"] == "market"
    assert configuration["pending_orders_forbidden"] is True
    assert screen.attempt_manifest()["win_probability_alpha_allocation"] == (
        "one_sided_0.05_over_4830"
    )


def test_v3_rejects_self_consistent_but_forged_outcome_gross_return() -> None:
    result = deepcopy(_valid_result())
    outcome = result["outcome_ledger"][0]
    cost = result["costs"][outcome["symbol"]]
    recorded_cost_bps = float(outcome["recorded_cost_bps"])
    outcome["gross_quote_bps"] -= 1.0
    outcome["currency_conversion_debit_bps"] = abs(
        outcome["gross_quote_bps"]
    ) * float(cost["convert_on_close_charge_fraction"])
    outcome["net_bps"] = (
        outcome["gross_quote_bps"]
        - recorded_cost_bps
        - outcome["currency_conversion_debit_bps"]
    )

    assert screen.validate_result_bundle(result) is False


def test_v3_accepts_the_strategy_defined_adverse_quote_gap_shape() -> None:
    result = deepcopy(_valid_result())
    reservation = result["reservation_ledger"][0]
    reservation["entry_status"] = "admitted"
    outcome = result["outcome_ledger"][0]
    outcome.update(
        {
            "entry_epoch": reservation["expected_entry_epoch"],
            "exit_epoch": reservation["expected_entry_epoch"] + 6,
            "entry_price": 1.0,
            "exit_price": None,
            "exit_reason": "QUOTE_GAP_ADVERSE",
        }
    )
    result["cells"] = screen.recompute_cells_from_ledgers(
        reservation_ledger=result["reservation_ledger"],
        outcome_ledger=result["outcome_ledger"],
        costs=result["costs"],
        source_ready_by_symbol=result["source_ready_by_symbol"],
    )

    assert screen.validate_result_bundle(result) is True
