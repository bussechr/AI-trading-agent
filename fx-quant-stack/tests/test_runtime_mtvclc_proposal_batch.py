from __future__ import annotations

import ast
from collections import OrderedDict
from dataclasses import asdict, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import pytest

from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS, IG_MT4_VENUE_ID
from fxstack.runtime.market_source_identity import build_authenticated_market_source
import fxstack.runtime.mtvclc_proposal_batch as batch_module
import fxstack.strategy.mtvclc as mtvclc_module
from fxstack.runtime.mtvclc_proposal_batch import (
    MTVCLC_RUNTIME_PROFILE_ID,
    evaluate_mtvclc_profile_batch,
)
from fxstack.strategy.mtvclc import (
    MT4_BID_PRICE_BASIS,
    MT4_IVOLUME_SOURCE,
    MTVCLC_V1_SYMBOLS,
    MTVCLCCostCalibration,
)


CURRENT_MINUTE = 1_800_000_000
COMMON_CLOSED_MINUTE = CURRENT_MINUTE - 60
AS_OF_EPOCH = CURRENT_MINUTE + 2.75
TRANSPORT_RECEIPT = CURRENT_MINUTE + 2.4
BAR_RECEIPT = CURRENT_MINUTE + 2.0


@lru_cache(maxsize=1)
def _source():
    source = build_authenticated_market_source(
        broker_account_scope="ig-demo-scope",
        broker_venue_id=IG_MT4_VENUE_ID,
        producer_identity="ig-mt4-bridge-ea",
        producer_instance_id="mt4-terminal-instance-a",
        terminal_lease_scope="ig-demo-terminal",
        credential_generation_id="generation-1",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert source is not None
    return source


def _state() -> dict[str, Any]:
    source = _source()
    return {
        "broker_account_scope": source.broker_account_scope,
        "broker_account_scope_schema": "fxstack_mt4_account_scope_djb2_xor32_v1",
        "broker_account_scope_version": 1,
        "broker_venue_id": source.broker_venue_id,
        "broker_server": "IG-DEMO",
        "broker_company": "IG Europe GmbH",
        "bridge_producer_identity": source.producer_identity,
        "bridge_producer_instance_id": source.producer_instance_id,
        "bridge_terminal_lease_scope": source.terminal_lease_scope,
        "bridge_credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
        "bridge_consumer_lease": {
            "consumer_identity": source.producer_identity,
            "producer_instance_id": source.producer_instance_id,
            "terminal_lease_scope": source.terminal_lease_scope,
            "credential_generation_id": source.credential_generation_id,
            "bridge_protocol_version": source.bridge_protocol_version,
            "expires_at": AS_OF_EPOCH + 120.0,
        },
    }


def _bar(
    *,
    symbol: str,
    minute_epoch: int,
    signal: bool,
) -> dict[str, Any]:
    source_fields = _source().to_fields()
    if signal:
        bid_open = 1.10000
        bid_high = 1.10220
        bid_low = 1.09980
        bid_close = 1.10200
        volume = 200
    else:
        bid_open = 1.10000
        bid_high = 1.10010
        bid_low = 1.09990
        bid_close = 1.10000
        volume = 100
    return {
        "time": minute_epoch,
        "bid_open": bid_open,
        "bid_high": bid_high,
        "bid_low": bid_low,
        "bid_close": bid_close,
        # Deliberately unrelated compatibility mid values prove that MTVCLC
        # does not normalize these into its bid-price strategy input.
        "open": bid_open + 0.01,
        "high": bid_high + 0.01,
        "low": bid_low + 0.01,
        "close": bid_close + 0.01,
        "volume": volume,
        "volume_source": MT4_IVOLUME_SOURCE,
        "price_basis": MT4_BID_PRICE_BASIS,
        "provider": "mt4_bridge",
        "canonical_symbol": symbol,
        "pair": symbol,
        "venue": IG_MT4_VENUE_ID,
        "source_timeframe": "M1",
        "received_at_epoch": BAR_RECEIPT,
        "quality_flags": [],
        **source_fields,
    }


def _bars(symbol: str) -> list[dict[str, Any]]:
    first = COMMON_CLOSED_MINUTE - 240 * 60
    rows = [
        _bar(
            symbol=symbol,
            minute_epoch=first + index * 60,
            signal=index == 240,
        )
        for index in range(241)
    ]
    # The merged endpoint normally includes the current tick-derived bucket.
    # It must be filtered by time before direct-MT4 provenance is assessed.
    rows.append(
        {
            **_bar(symbol=symbol, minute_epoch=CURRENT_MINUTE, signal=False),
            "price_basis": "bridge_tick_mid_ohlc_v1",
            "volume_source": "bridge_market_event_count_v1",
        }
    )
    return rows


def _quote(symbol: str) -> dict[str, Any]:
    source_fields = _source().to_fields()
    mid = 1.10200
    spread_bps = 0.5
    half_spread = mid * spread_bps / 1e4 / 2.0
    return {
        "provider": "mt4_bridge",
        "instrument": {
            "canonical_symbol": symbol,
            "pair": symbol,
            "venue": IG_MT4_VENUE_ID,
        },
        "bid": mid - half_spread,
        "ask": mid + half_spread,
        "quality_flags": [],
        "transport_fresh": True,
        "received_at_epoch": TRANSPORT_RECEIPT,
        # This older broker-event receipt must not replace the transport clock.
        "market_event_received_at_epoch": CURRENT_MINUTE + 1.0,
        "market_event_sequence": 7,
        "source_event_baseline_initialized": True,
        "source_event_token": "1800000002",
        **source_fields,
    }


def _cost(symbol: str) -> MTVCLCCostCalibration:
    return MTVCLCCostCalibration(
        symbol=symbol,
        calibration_id="sealed-ig-mt4-cost-schedule-v1",
        source_sha256="a" * 64,
        p90_spread_bps=2.0,
        commission_bps_per_round_trip=0.1,
        financing_bps_per_trade=0.1,
        account_currency="EUR",
        pnl_currency="EUR",
        convert_on_close_charge_fraction=0.0,
    )


def _inputs() -> tuple[
    dict[str, list[dict[str, Any]]],
    dict[str, dict[str, Any]],
    dict[str, MTVCLCCostCalibration],
]:
    return (
        {symbol: _bars(symbol) for symbol in MTVCLC_V1_SYMBOLS},
        {symbol: _quote(symbol) for symbol in MTVCLC_V1_SYMBOLS},
        {symbol: _cost(symbol) for symbol in MTVCLC_V1_SYMBOLS},
    )


def _evaluate(
    *,
    profile: str = MTVCLC_RUNTIME_PROFILE_ID,
    bars: dict[str, list[dict[str, Any]]] | None = None,
    quotes: dict[str, dict[str, Any]] | None = None,
    costs: dict[str, MTVCLCCostCalibration] | None = None,
    as_of_epoch: float = AS_OF_EPOCH,
):
    if bars is None or quotes is None or costs is None:
        default_bars, default_quotes, default_costs = _inputs()
        bars = bars if bars is not None else default_bars
        quotes = quotes if quotes is not None else default_quotes
        costs = costs if costs is not None else default_costs
    return evaluate_mtvclc_profile_batch(
        strategy_profile=profile,
        raw_bars_by_symbol=bars,
        raw_quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        market_source_state=_state(),
        as_of_epoch=as_of_epoch,
    )


def test_adapter_scalar_fast_paths_preserve_numeric_and_flag_edges() -> None:
    assert batch_module._finite_float(1.25) == 1.25
    assert batch_module._finite_float(7) == 7.0
    assert batch_module._finite_float(10**400) is None
    assert batch_module._finite_float(float("nan")) is None
    assert batch_module._parse_epoch(CURRENT_MINUTE) == CURRENT_MINUTE
    assert batch_module._parse_epoch(True) is None
    assert batch_module._quality_flags({"quality_flags": []}) == ()
    assert batch_module._quality_flags({"quality_flags": ()}) == ()
    assert batch_module._quality_flags({"quality_flags": ["stale"]}) == ("stale",)


def test_bar_projection_cache_is_exact_and_mutation_safe() -> None:
    batch_module._source_projection.cache_clear()
    source, reasons = batch_module._project_source_identity(
        _state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert source is not None
    assert reasons == ()
    repeated_source, repeated_reasons = batch_module._project_source_identity(
        _state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert repeated_reasons == ()
    assert repeated_source is source

    changed_state = _state()
    changed_state["broker_company"] = "Different Broker Company"
    changed_source, changed_reasons = batch_module._project_source_identity(
        changed_state,
        as_of_epoch=AS_OF_EPOCH,
    )
    assert changed_reasons == ()
    assert changed_source is not None
    assert changed_source is not source
    assert changed_source != source

    row = _bar(
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        signal=True,
    )
    batch_module._bar_from_projection.cache_clear()

    first = batch_module._bar_from_row(
        row=row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    second = batch_module._bar_from_row(
        row=row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )

    assert first == second
    assert batch_module._bar_from_projection.cache_info().hits == 1

    row["bid_open"] = None
    invalid_bar, invalid_reasons = batch_module._bar_from_row(
        row=row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    assert invalid_bar is None
    assert "bar_bid_prices_invalid" in invalid_reasons

    row["bid_open"] = 1.1
    row["quality_flags"] = [{"custom": "unhashable"}]
    fallback_bar, fallback_reasons = batch_module._bar_from_row(
        row=row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    assert fallback_bar is None
    assert "bar_quality_flags_present" in fallback_reasons

    external_row = _bar(
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        signal=True,
    )
    cached_row = batch_module.cache_mtvclc_bar_row(external_row)
    assert cached_row == external_row
    assert cached_row is not external_row
    with pytest.raises(TypeError, match="immutable"):
        cached_row["bid_open"] = None

    cached_rows = batch_module.cache_mtvclc_bar_rows([cached_row])
    assert cached_rows == [cached_row]
    with pytest.raises(TypeError, match="immutable"):
        cached_rows.append(cached_row)
    aggregate_key = batch_module._prepared_bar_cache_key(
        symbol="EURUSD",
        raw_rows=cached_rows,
        current_minute_epoch=CURRENT_MINUTE,
        common_closed_minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    assert aggregate_key is not None
    assert aggregate_key[-1] is cached_rows.projection_key
    assert hash(aggregate_key) == hash(aggregate_key)

    batch_module._bar_from_projection.cache_clear()
    cached_first = batch_module._bar_from_row(
        row=cached_row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    cached_second = batch_module._bar_from_row(
        row=cached_row,
        symbol="EURUSD",
        minute_epoch=COMMON_CLOSED_MINUTE,
        source=source,
    )
    assert cached_first == cached_second
    assert batch_module._bar_from_projection.cache_info().hits == 1


def test_prepared_bar_cache_is_runtime_owned_and_exactly_invalidated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, reasons = batch_module._project_source_identity(
        _state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert source is not None
    assert reasons == ()
    rows = [batch_module.cache_mtvclc_bar_row(row) for row in _bars("EURUSD")]
    original_bar_from_row = batch_module._bar_from_row
    bar_calls = 0

    def counted_bar_from_row(**kwargs: Any):
        nonlocal bar_calls
        bar_calls += 1
        return original_bar_from_row(**kwargs)

    monkeypatch.setattr(batch_module, "_bar_from_row", counted_bar_from_row)
    batch_module._clear_prepared_bar_cache()
    prepare_kwargs = {
        "symbol": "EURUSD",
        "as_of_epoch": AS_OF_EPOCH,
        "current_minute_epoch": CURRENT_MINUTE,
        "common_closed_minute_epoch": COMMON_CLOSED_MINUTE,
        "source": source,
    }
    first = batch_module._prepare_bars(raw_rows=rows, **prepare_kwargs)
    required_bars = batch_module.REQUIRED_COMPLETED_M1_BARS
    assert bar_calls == required_bars

    repeated = batch_module._prepare_bars(raw_rows=rows, **prepare_kwargs)
    assert repeated == first
    assert repeated[0] is first[0]
    assert bar_calls == required_bars

    reordered_rows = list(rows)
    reordered_rows[0], reordered_rows[1] = reordered_rows[1], reordered_rows[0]
    reordered = batch_module._prepare_bars(
        raw_rows=reordered_rows,
        **prepare_kwargs,
    )
    assert reordered == first
    assert bar_calls == 2 * required_bars

    changed_rows = list(rows)
    changed_signal = dict(changed_rows[-2])
    changed_signal["bid_close"] = float(changed_signal["bid_close"]) - 0.0001
    changed_rows[-2] = batch_module.cache_mtvclc_bar_row(changed_signal)
    batch_module._prepare_bars(raw_rows=changed_rows, **prepare_kwargs)
    assert bar_calls == 3 * required_bars

    changed_state = _state()
    changed_state["broker_company"] = "Changed Broker"
    changed_source, changed_reasons = batch_module._project_source_identity(
        changed_state,
        as_of_epoch=AS_OF_EPOCH,
    )
    assert changed_source is not None
    assert changed_reasons == ()
    batch_module._prepare_bars(
        raw_rows=rows,
        **{**prepare_kwargs, "source": changed_source},
    )
    assert bar_calls == 4 * required_bars

    mutable_rows = _bars("EURUSD")
    batch_module._prepare_bars(raw_rows=mutable_rows, **prepare_kwargs)
    batch_module._prepare_bars(raw_rows=mutable_rows, **prepare_kwargs)
    assert bar_calls == 6 * required_bars


def test_prepared_bar_cache_never_reuses_a_future_receipt_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, reasons = batch_module._project_source_identity(
        _state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert source is not None
    assert reasons == ()
    rows = _bars("EURUSD")
    rows[-2]["received_at_epoch"] = AS_OF_EPOCH + 1.0
    cached_rows = [batch_module.cache_mtvclc_bar_row(row) for row in rows]
    original_bar_from_row = batch_module._bar_from_row
    bar_calls = 0

    def counted_bar_from_row(**kwargs: Any):
        nonlocal bar_calls
        bar_calls += 1
        return original_bar_from_row(**kwargs)

    monkeypatch.setattr(batch_module, "_bar_from_row", counted_bar_from_row)
    batch_module._clear_prepared_bar_cache()
    kwargs = {
        "symbol": "EURUSD",
        "raw_rows": cached_rows,
        "as_of_epoch": AS_OF_EPOCH,
        "current_minute_epoch": CURRENT_MINUTE,
        "common_closed_minute_epoch": COMMON_CLOSED_MINUTE,
        "source": source,
    }
    first = batch_module._prepare_bars(**kwargs)
    second = batch_module._prepare_bars(**kwargs)
    assert "signal_bar_received_after_evaluation_time" in first[1]
    assert second == first
    assert bar_calls == 0
    assert batch_module._PREPARED_BAR_CACHE == {}


def test_adapter_preserves_direct_bid_ivolume_source_transport_clock_and_costs() -> (
    None
):
    result = _evaluate()
    source = _source()

    assert result.diagnostics.accepted is True
    assert result.diagnostics.reasons == ()
    assert result.diagnostics.expected_symbols == IG_MT4_SCALP_SYMBOLS
    assert result.diagnostics.observed_bar_symbols == MTVCLC_V1_SYMBOLS
    assert result.diagnostics.market_source_id == source.source_id
    assert result.diagnostics.producer_instance_id == source.producer_instance_id
    assert result.to_dict() == asdict(result)
    assert result.diagnostics.to_dict() == asdict(result.diagnostics)
    cycle_summary = result.diagnostics.to_cycle_summary()
    assert cycle_summary["symbol_diagnostic_count"] == 22
    assert cycle_summary["structural_ready_count"] == 22
    assert cycle_summary["evaluation_allowed_count"] == 22
    assert "symbol_diagnostics" not in cycle_summary
    sparse_diagnostic = replace(
        result.diagnostics.symbol_diagnostics[0],
        evaluation_side=None,
        evaluation_signal_epoch=None,
        evaluation_reasons=(),
    ).to_decision_dict()
    assert "evaluation_side" not in sparse_diagnostic
    assert "evaluation_signal_epoch" not in sparse_diagnostic
    assert "evaluation_reasons" not in sparse_diagnostic
    assert sparse_diagnostic["evaluation_allowed"] is True
    assert sparse_diagnostic["selected_history_count"] == 241
    refused_diagnostic = replace(
        result.diagnostics.symbol_diagnostics[0],
        evaluation_allowed=False,
        evaluation_reasons=("no_signal",),
    ).to_decision_dict()
    assert refused_diagnostic["evaluation_allowed"] is False
    assert refused_diagnostic["evaluation_reasons"] == ("no_signal",)
    assert len(result.proposals) == 22
    assert {proposal.symbol for proposal in result.proposals} == set(MTVCLC_V1_SYMBOLS)
    assert all(proposal.execution_type == "market" for proposal in result.proposals)
    assert all(proposal.pending_orders_forbidden for proposal in result.proposals)
    assert all(proposal.immediate_market_trade for proposal in result.proposals)
    assert all(
        proposal.entry_epoch == CURRENT_MINUTE + 2 for proposal in result.proposals
    )
    assert all(
        proposal.bar_source_id == source.source_id for proposal in result.proposals
    )
    assert all(
        proposal.quote_source_id == source.source_id for proposal in result.proposals
    )
    assert all(proposal.cost_calibration_id for proposal in result.proposals)
    assert all(proposal.cost_calibration_row_sha256 for proposal in result.proposals)
    assert all(not proposal.evidence_qualified for proposal in result.proposals)
    assert all(not proposal.release_authorized for proposal in result.proposals)
    assert all(not proposal.broker_trade_authorized for proposal in result.proposals)
    for diagnostic in result.diagnostics.symbol_diagnostics:
        assert diagnostic.structural_ready is True
        assert diagnostic.selected_history_count == 241
        assert diagnostic.filtered_current_bar_count == 1
        assert diagnostic.quote_transport_received_at_epochs == (TRANSPORT_RECEIPT,)
        assert diagnostic.evaluation_allowed is True
        assert diagnostic.evaluation_side == "BUY"
        assert diagnostic.evaluation_volume_v90 == pytest.approx(100.0)
        assert diagnostic.evaluation_signal_tick_volume == 200
        assert diagnostic.evaluation_activity_ratio == pytest.approx(2.0)
        assert diagnostic.evaluation_bid_close_location == pytest.approx(11.0 / 12.0)
        assert diagnostic.evaluation_live_spread_bps == pytest.approx(0.5)


def test_adapter_quote_handoff_avoids_duplicate_typed_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    validation_calls = 0
    original = mtvclc_module._quote_validation_reasons

    def counted(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mtvclc_module, "_quote_validation_reasons", counted)

    result = _evaluate()

    assert result.diagnostics.accepted is True
    assert validation_calls == 0


def test_ready_symbol_diagnostics_are_constructed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = batch_module.MTVCLCSymbolProposalDiagnostic
    construction_calls = 0

    def counted(*args, **kwargs):
        nonlocal construction_calls
        construction_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(batch_module, "MTVCLCSymbolProposalDiagnostic", counted)

    result = _evaluate()

    assert result.diagnostics.accepted is True
    assert construction_calls == len(MTVCLC_V1_SYMBOLS)


def test_adapter_quote_handoff_keeps_each_cycle_quote_live() -> None:
    bars, quotes, costs = _inputs()
    first = _evaluate(bars=bars, quotes=quotes, costs=costs)
    moved_quotes = dict(quotes)
    moved_eurusd = dict(quotes["EURUSD"])
    moved_eurusd["bid"] = float(moved_eurusd["bid"]) + 0.001
    moved_eurusd["ask"] = float(moved_eurusd["ask"]) + 0.001
    moved_quotes["EURUSD"] = moved_eurusd

    second = _evaluate(bars=bars, quotes=moved_quotes, costs=costs)
    first_eurusd = next(item for item in first.proposals if item.symbol == "EURUSD")
    second_eurusd = next(item for item in second.proposals if item.symbol == "EURUSD")

    assert second_eurusd.entry_price != first_eurusd.entry_price


@pytest.mark.parametrize(
    "changes",
    (
        {},
        {"bid": None},
        {"received_at_epoch": AS_OF_EPOCH + 1.0},
        {"market_event_sequence": 0},
        {"source_event_token": "invalid"},
        {
            "received_at_epoch": 0.5,
            "market_event_received_at_epoch": None,
            "market_event_sequence": 0,
        },
    ),
)
def test_single_quote_fast_path_matches_general_singleton(
    changes: dict[str, Any],
) -> None:
    source, source_reasons = batch_module._project_source_identity(
        _state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert source is not None
    assert source_reasons == ()
    row = {**_quote("EURUSD"), **changes}

    fast = batch_module._prepare_quotes(
        symbol="EURUSD",
        raw_rows=row,
        as_of_epoch=AS_OF_EPOCH,
        source=source,
    )
    general = batch_module._prepare_quotes(
        symbol="EURUSD",
        raw_rows=(row,),
        as_of_epoch=AS_OF_EPOCH,
        source=source,
    )

    assert fast == general


def test_adapter_uses_last_observed_bars_across_no_tick_minutes() -> None:
    bars, quotes, costs = _inputs()
    for symbol in MTVCLC_V1_SYMBOLS:
        for index in range(120):
            bars[symbol][index]["time"] -= 60

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert result.diagnostics.accepted is True
    assert len(result.proposals) == len(MTVCLC_V1_SYMBOLS)
    assert all(
        diagnostic.structural_ready and diagnostic.selected_history_count == 241
        for diagnostic in result.diagnostics.symbol_diagnostics
    )


def test_strategy_abstention_retains_causal_near_signal_measurements() -> None:
    bars, quotes, costs = _inputs()
    bars["EURUSD"][-2]["volume"] = 50

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_ready is True
    assert diagnostic.evaluation_allowed is False
    assert diagnostic.evaluation_reasons == ("tick_volume_not_strictly_above_v90",)
    assert diagnostic.evaluation_volume_v90 == pytest.approx(100.0)
    assert diagnostic.evaluation_signal_tick_volume == 50
    assert diagnostic.evaluation_activity_ratio == pytest.approx(0.5)
    assert diagnostic.evaluation_bid_body_bps is None
    assert diagnostic.evaluation_live_spread_bps is None
    assert "EURUSD" not in {proposal.symbol for proposal in result.proposals}


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    (
        ("provider", "other", "bar_provider_not_mt4_bridge"),
        ("canonical_symbol", "USDJPY", "bar_symbol_mismatch"),
        ("venue", "other", "bar_venue_mismatch"),
        ("source_timeframe", "M5", "bar_timeframe_mismatch"),
        (
            "volume_source",
            "bridge_market_event_count_v1",
            "bar_volume_source_not_direct_mt4_ivolume",
        ),
        (
            "price_basis",
            "bridge_tick_mid_ohlc_v1",
            "bar_price_basis_not_direct_mt4_bid_ohlc",
        ),
        ("quality_flags", ["stale"], "bar_quality_flags_present"),
        ("bid_open", None, "bar_bid_prices_invalid"),
        ("bid_high", 0.5, "bar_bid_geometry_invalid"),
        ("volume", 200.0, "bar_ivolume_not_strict_integer"),
    ),
)
def test_one_fallback_or_coerced_direct_bar_abstains_without_blocking_other_pairs(
    field: str,
    value: Any,
    expected_reason: str,
) -> None:
    bars, quotes, costs = _inputs()
    bars["EURUSD"][-2][field] = value

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert result.diagnostics.accepted is True
    assert len(result.proposals) == 21
    assert "EURUSD" not in {proposal.symbol for proposal in result.proposals}
    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_ready is False
    assert expected_reason in diagnostic.structural_reasons
    assert diagnostic.evaluation_allowed is None


def test_quote_must_match_the_exact_authenticated_terminal_producer() -> None:
    bars, quotes, costs = _inputs()
    quotes["EURUSD"]["producer_instance_id"] = "cloned-terminal"

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert result.diagnostics.accepted is True
    assert len(result.proposals) == 21
    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_ready is False
    assert any(
        reason.startswith("quote_market_source_")
        for reason in diagnostic.structural_reasons
    )


def test_signal_bar_first_receipt_is_required_and_late_pair_abstains() -> None:
    bars, quotes, costs = _inputs()
    bars["EURUSD"][-2].pop("received_at_epoch")

    missing = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert len(missing.proposals) == 21
    diagnostic = next(
        item
        for item in missing.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_reasons == ("signal_bar_receipt_invalid",)

    bars, quotes, costs = _inputs()
    bars["EURUSD"][-2]["received_at_epoch"] = CURRENT_MINUTE + 5.05
    quotes["EURUSD"]["received_at_epoch"] = CURRENT_MINUTE + 4.9

    late = _evaluate(
        bars=bars,
        quotes=quotes,
        costs=costs,
        as_of_epoch=CURRENT_MINUTE + 5.0,
    )

    assert len(late.proposals) == 21
    diagnostic = next(
        item for item in late.diagnostics.symbol_diagnostics if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_reasons == (
        "signal_bar_received_after_evaluation_time",
    )


def test_signal_bar_cannot_be_received_before_its_close() -> None:
    bars, quotes, costs = _inputs()
    bars["EURUSD"][-2]["received_at_epoch"] = CURRENT_MINUTE - 0.000001

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert len(result.proposals) == 21
    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_reasons == ("signal_bar_received_before_close",)


def test_delayed_observable_bar_is_evaluated_without_global_wall_clock_refusal() -> (
    None
):
    bars, quotes, costs = _inputs()
    for symbol in MTVCLC_V1_SYMBOLS:
        bars[symbol][-2]["received_at_epoch"] = CURRENT_MINUTE + 5.0
        quotes[symbol]["received_at_epoch"] = CURRENT_MINUTE + 5.0

    exact = _evaluate(
        bars=bars,
        quotes=quotes,
        costs=costs,
        as_of_epoch=CURRENT_MINUTE + 5.0,
    )
    late = _evaluate(
        bars=bars,
        quotes=quotes,
        costs=costs,
        as_of_epoch=CURRENT_MINUTE + 5.000001,
    )

    assert len(exact.proposals) == 22
    assert len(late.proposals) == 22
    assert late.diagnostics.accepted is True
    assert late.diagnostics.reasons == ()


def test_quote_transport_receipt_after_evaluation_time_abstains() -> None:
    bars, quotes, costs = _inputs()
    quotes["EURUSD"]["received_at_epoch"] = AS_OF_EPOCH + 0.000001

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    assert len(result.proposals) == 21
    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_reasons == ("quote_transport_time_future",)


def test_profile_and_all_three_scopes_are_explicit_exact_and_ordered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bars, quotes, costs = _inputs()

    def must_not_evaluate(*_args: Any, **_kwargs: Any):
        raise AssertionError("invalid profile/scope must stop before evaluation")

    monkeypatch.setattr(batch_module, "evaluate_mtvclc", must_not_evaluate)
    wrong_profile = _evaluate(
        profile="scalp_dislocation",
        bars=bars,
        quotes=quotes,
        costs=costs,
    )
    assert wrong_profile.diagnostics.reasons == ("strategy_profile_unsupported",)

    reordered_quotes = OrderedDict(reversed(tuple(quotes.items())))
    reordered = evaluate_mtvclc_profile_batch(
        strategy_profile=MTVCLC_RUNTIME_PROFILE_ID,
        raw_bars_by_symbol=bars,
        raw_quotes_by_symbol=reordered_quotes,
        costs_by_symbol=costs,
        market_source_state=_state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert reordered.proposals == ()
    assert reordered.diagnostics.reasons == ("quote_scope_must_match_exact_ordered_22",)

    bars_with_xrp = dict(bars)
    bars_with_xrp["XRPUSD"] = bars_with_xrp.pop("NZDJPY")
    xrp = evaluate_mtvclc_profile_batch(
        strategy_profile=MTVCLC_RUNTIME_PROFILE_ID,
        raw_bars_by_symbol=bars_with_xrp,
        raw_quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        market_source_state=_state(),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert xrp.proposals == ()
    assert xrp.diagnostics.reasons == ("bar_scope_must_match_exact_ordered_22",)
    assert "XRPUSD" in xrp.diagnostics.observed_bar_symbols


def test_invalid_frozen_cost_row_is_a_strategy_refusal_not_live_spread_substitution() -> (
    None
):
    bars, quotes, costs = _inputs()
    costs["EURUSD"] = replace(costs["EURUSD"], p90_spread_bps=0.0)

    result = _evaluate(bars=bars, quotes=quotes, costs=costs)

    diagnostic = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert diagnostic.structural_ready is True
    assert diagnostic.evaluation_allowed is False
    assert diagnostic.evaluation_reasons == ("cost_calibration_invalid",)
    assert "EURUSD" not in {proposal.symbol for proposal in result.proposals}


def test_adapter_imports_no_research_execution_authority_or_io_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "runtime"
        / "mtvclc_proposal_batch.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = (
        "fxstack.scalp",
        "fxstack.risk",
        "fxstack.settings",
        "fxstack.runtime.service",
        "fxstack.runtime.postgres_store",
        "fxstack.runtime.scalp_runtime_admission",
        "fxstack.runtime.scalp_execution_authority",
        "fxstack.runtime.scalp_execution_boundary",
    )
    assert all(
        not any(module == root or module.startswith(f"{root}.") for root in forbidden)
        for module in imported
    )
