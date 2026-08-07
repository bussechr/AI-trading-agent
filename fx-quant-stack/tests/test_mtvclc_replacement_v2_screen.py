from __future__ import annotations

import hashlib

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation as base,
)
from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation_replacement_v2 as replacement,
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


def _screen(module):  # type: ignore[no-untyped-def]
    return module.screen_universe(
        bars_by_symbol={symbol: [] for symbol in IG_MT4_SCALP_SYMBOLS},
        quotes_by_symbol={symbol: [] for symbol in IG_MT4_SCALP_SYMBOLS},
        costs_by_symbol={symbol: _cost(symbol) for symbol in IG_MT4_SCALP_SYMBOLS},
        source_sha256_by_symbol={
            symbol: hashlib.sha256(f"source:{symbol}".encode()).hexdigest()
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        source_contract_id=base.SOURCE_CONTRACT_ID,
    )


def test_third_attempt_changes_only_family_accounting_and_intervals() -> None:
    original = _screen(base)
    result = _screen(replacement)

    assert replacement.validate_result_bundle(result) is True
    assert base.validate_result_bundle(result) is False
    assert result["attempt_accounting"] == {
        "prior_attempted_cells_lower_bound": 4_786,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_830,
    }
    manifest = result["attempt_manifest"]
    assert manifest["descriptive_df99_bonferroni_abs_t_threshold"] == (
        4.648050309953223
    )
    assert manifest["win_probability_familywise_attempted_cells"] == 4_830
    assert manifest["win_probability_alpha_allocation"] == "one_sided_0.05_over_4830"
    for field in set(original) - {"attempt_accounting", "attempt_manifest", "cells"}:
        assert result[field] == original[field]


def test_third_attempt_validator_rejects_stale_4786_family() -> None:
    result = _screen(replacement)
    stale = dict(result)
    stale["attempt_accounting"] = {
        "prior_attempted_cells_lower_bound": 4_742,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_786,
    }

    assert replacement.validate_result_bundle(stale) is False


def test_third_attempt_wilson_spends_alpha_over_all_4830_cells() -> None:
    base_family = base._wilson_one_sided_lower(30, 30)
    third_family = replacement._wilson_one_sided_lower(30, 30)

    assert third_family < base_family
    assert replacement._wilson_one_sided_lower(0, 0) == 0.0
