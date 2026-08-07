from __future__ import annotations

import ast
from dataclasses import asdict, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import pytest

import fxstack.scalp.screen_mt4_tick_volume_close_location_continuation as research
import fxstack.strategy.mtvclc as mtvclc_module
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.strategy.mtvclc import (
    FROZEN_MTVCLC_POLICY,
    MAX_ENTRY_DELAY_SECONDS,
    MT4_BID_PRICE_BASIS,
    MT4_IVOLUME_SOURCE,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    MTVCLC_V1_SYMBOLS,
    MTVCLCAuthenticatedQuote,
    MTVCLCBidM1Bar,
    MTVCLCCostCalibration,
    MTVCLCEvaluationRequest,
    MTVCLCMarketSourceIdentity,
    MTVCLCPolicy,
    MTVCLCTradeCandidate,
    evaluate_mtvclc,
)


BAR_SOURCE_ID = "ig_mt4_direct_completed_bid_m1"
BAR_SOURCE_VERSION = "v1"
QUOTE_SOURCE_ID = "ig_mt4_authenticated_transport_quote"
QUOTE_SOURCE_VERSION = "v1"
CALIBRATION_ID = "sealed-ig-mt4-cost-schedule-v1"
CALIBRATION_SOURCE_SHA256 = "a" * 64
EVENT_TOKEN_SHA256 = "b" * 64
SIGNAL_EPOCH = int(datetime(2027, 2, 1, 12, tzinfo=timezone.utc).timestamp())


def _source_identity() -> MTVCLCMarketSourceIdentity:
    return MTVCLCMarketSourceIdentity(
        broker_account_scope="IG-DEMO:123456",
        broker_account_scope_schema="fxstack.ig_mt4.account_scope.v1",
        broker_account_scope_version=1,
        broker_server="IG-LIVE-DEMO",
        broker_company="IG",
        consumer_identity="bridge-ea-demo",
        producer_instance_id="terminal-demo-1",
        terminal_lease_scope="ig-demo-terminal-1",
        credential_generation_id="demo-generation-1",
        bridge_protocol_version="v2",
    )


def _bar(
    *,
    symbol: str,
    minute_epoch: int,
    bid_open: float,
    bid_high: float,
    bid_low: float,
    bid_close: float,
    tick_volume: int,
) -> MTVCLCBidM1Bar:
    return MTVCLCBidM1Bar(
        symbol=symbol,
        venue_id=IG_MT4_VENUE_ID,
        source_id=BAR_SOURCE_ID,
        source_version=BAR_SOURCE_VERSION,
        source_identity=_source_identity(),
        minute_epoch=minute_epoch,
        bar_seconds=60,
        bid_open=bid_open,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=bid_close,
        tick_volume=tick_volume,
        volume_source=MT4_IVOLUME_SOURCE,
        price_basis=MT4_BID_PRICE_BASIS,
        closed=True,
        quality_flags=(),
    )


def _bars(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    signal_epoch: int = SIGNAL_EPOCH,
) -> tuple[MTVCLCBidM1Bar, ...]:
    first_epoch = signal_epoch - 240 * 60
    bars = [
        _bar(
            symbol=symbol,
            minute_epoch=first_epoch + index * 60,
            bid_open=1.10000,
            bid_high=1.10010,
            bid_low=1.09990,
            bid_close=1.10000,
            tick_volume=100,
        )
        for index in range(240)
    ]
    if side == "BUY":
        signal = _bar(
            symbol=symbol,
            minute_epoch=signal_epoch,
            bid_open=1.10000,
            bid_high=1.10220,
            bid_low=1.09980,
            bid_close=1.10200,
            tick_volume=200,
        )
    else:
        signal = _bar(
            symbol=symbol,
            minute_epoch=signal_epoch,
            bid_open=1.10000,
            bid_high=1.10020,
            bid_low=1.09780,
            bid_close=1.09800,
            tick_volume=200,
        )
    return (*bars, signal)


def _quote(
    *,
    symbol: str = "EURUSD",
    observed_epoch: int = SIGNAL_EPOCH + 60,
    mid: float = 1.10200,
    spread_bps: float = 0.5,
) -> MTVCLCAuthenticatedQuote:
    half_spread = mid * spread_bps / 1e4 / 2.0
    return MTVCLCAuthenticatedQuote(
        symbol=symbol,
        venue_id=IG_MT4_VENUE_ID,
        source_id=QUOTE_SOURCE_ID,
        source_version=QUOTE_SOURCE_VERSION,
        source_identity=_source_identity(),
        observed_epoch=observed_epoch,
        bid=mid - half_spread,
        ask=mid + half_spread,
        source_event_token_sha256=EVENT_TOKEN_SHA256,
        market_event_sequence=7,
    )


def _cost(
    *,
    symbol: str = "EURUSD",
    conversion_applies: bool = False,
) -> MTVCLCCostCalibration:
    return MTVCLCCostCalibration(
        symbol=symbol,
        calibration_id=CALIBRATION_ID,
        source_sha256=CALIBRATION_SOURCE_SHA256,
        p90_spread_bps=1.0,
        commission_bps_per_round_trip=0.1,
        financing_bps_per_trade=0.1,
        account_currency="EUR",
        pnl_currency="USD" if conversion_applies else "EUR",
        convert_on_close_charge_fraction=0.005 if conversion_applies else 0.0,
    )


def _request(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    conversion_applies: bool = False,
    signal_epoch: int = SIGNAL_EPOCH,
    spread_bps: float = 0.5,
) -> MTVCLCEvaluationRequest:
    bars = _bars(symbol=symbol, side=side, signal_epoch=signal_epoch)
    return MTVCLCEvaluationRequest(
        symbol=symbol,
        bars=bars,
        quotes=(
            _quote(
                symbol=symbol,
                observed_epoch=signal_epoch + 60,
                mid=bars[-1].bid_close,
                spread_bps=spread_bps,
            ),
        ),
        cost=_cost(symbol=symbol, conversion_applies=conversion_applies),
    )


def _research_result(
    request: MTVCLCEvaluationRequest,
    *,
    side: str,
) -> tuple[research.MTVCLCSignal | None, str]:
    bars = research.prepare_bars(
        tuple(
            research.MT4BidBar(
                epoch=bar.minute_epoch,
                bid_open=bar.bid_open,
                bid_high=bar.bid_high,
                bid_low=bar.bid_low,
                bid_close=bar.bid_close,
                tick_volume=bar.tick_volume,
            )
            for bar in request.bars
        )
    )
    quotes = research.prepare_quotes(
        tuple(
            research.MT4Quote(
                epoch=quote.observed_epoch,
                bid=quote.bid,
                ask=quote.ask,
                source_event_token_sha256=quote.source_event_token_sha256,
                market_event_sequence=quote.market_event_sequence,
            )
            for quote in request.quotes
        )
    )
    cost = research.MT4CostCalibration(
        symbol=request.cost.symbol,
        p90_spread_bps=request.cost.p90_spread_bps,
        commission_bps_per_round_trip=(request.cost.commission_bps_per_round_trip),
        financing_bps_per_trade=request.cost.financing_bps_per_trade,
        account_currency=request.cost.account_currency,
        pnl_currency=request.cost.pnl_currency,
        convert_on_close_charge_fraction=(
            request.cost.convert_on_close_charge_fraction
        ),
        source_sha256=request.cost.source_sha256,
        adverse_execution_debit_bps=request.cost.adverse_execution_debit_bps,
    )
    return cast(
        tuple[research.MTVCLCSignal | None, str],
        research.evaluate_signal(
            prepared_bars=bars,
            prepared_quotes=quotes,
            signal_index=240,
            symbol=request.symbol,
            side=side,
            cost=cost,
        ),
    )


@pytest.mark.parametrize("side", ("BUY", "SELL"))
@pytest.mark.parametrize("conversion_applies", (False, True))
def test_immediate_market_candidate_has_exact_research_parity(
    side: str,
    conversion_applies: bool,
) -> None:
    request = _request(side=side, conversion_applies=conversion_applies)

    expected, expected_reason = _research_result(request, side=side)
    candidate = evaluate_mtvclc(request)

    assert expected_reason == ""
    assert expected is not None
    assert candidate.allowed is True
    assert candidate.reasons == ()
    assert candidate.side == expected.side == side
    assert candidate.signal_epoch == expected.signal_epoch
    assert candidate.expected_entry_epoch == expected.signal_epoch + 60
    assert candidate.entry_epoch == expected.entry_epoch
    assert candidate.entry_price == expected.entry_price
    assert candidate.stop_price == expected.stop_price
    assert candidate.target_price == expected.target_price
    assert candidate.live_spread_bps == expected.live_spread_bps
    assert candidate.recorded_cost_bps == expected.recorded_cost_bps
    assert candidate.target_bps == expected.target_bps
    assert candidate.stop_bps == expected.stop_bps
    assert candidate.p_star == expected.p_star
    assert candidate.volume_v90 == expected.volume_v90
    assert candidate.signal_tick_volume == expected.signal_tick_volume
    assert candidate.bid_body_bps == expected.bid_body_bps
    assert candidate.bid_close_location == expected.bid_close_location
    assert candidate.time_stop_bars == 30
    assert candidate.maximum_quote_gap_seconds == 5
    assert candidate.market_source_identity_sha256 == (
        request.bars[-1].source_identity.identity_sha256()
    )
    assert candidate.cost_calibration_id == CALIBRATION_ID
    assert candidate.cost_calibration_source_sha256 == CALIBRATION_SOURCE_SHA256
    assert candidate.cost_calibration_row_sha256 == request.cost.row_sha256()

    assert candidate.strategy_id == MTVCLC_STRATEGY_ID
    assert candidate.strategy_version == MTVCLC_STRATEGY_VERSION
    assert candidate.execution_type == "market"
    assert candidate.immediate_market_trade is True
    assert candidate.pending_orders_forbidden is True
    assert candidate.entry_deadline_epoch == (
        candidate.expected_entry_epoch + MAX_ENTRY_DELAY_SECONDS
    )
    assert candidate.qualification == "candidate_unqualified"
    assert candidate.evidence_qualified is False
    assert candidate.release_authorized is False
    assert candidate.activation_authorized is False
    assert candidate.risk_qualified is False
    assert candidate.sizing_authorized is False
    assert candidate.queue_authorized is False
    assert candidate.broker_trade_authorized is False
    assert candidate.execution_qualified is False
    assert candidate.win_probability is None


@pytest.mark.parametrize(
    ("case", "expected_refusal"),
    (
        ("strict_volume", "tick_volume_not_strictly_above_v90"),
        ("body_floor", "bid_body_below_recorded_cost"),
        ("close_location", "bid_close_location_below_threshold"),
        ("live_spread", "live_spread_above_frozen_p90"),
    ),
)
def test_frozen_signal_and_entry_refusals_match_research(
    case: str,
    expected_refusal: str,
) -> None:
    request = _request()
    bars = list(request.bars)
    quotes = list(request.quotes)
    if case == "strict_volume":
        bars[-1] = replace(bars[-1], tick_volume=100)
    elif case == "body_floor":
        bars[-1] = replace(
            bars[-1],
            bid_high=1.10020,
            bid_low=1.09990,
            bid_close=1.10010,
        )
    elif case == "close_location":
        bars[-1] = replace(
            bars[-1],
            bid_high=1.10400,
            bid_low=1.09800,
        )
    elif case == "live_spread":
        quotes[-1] = _quote(spread_bps=1.01)
    request = replace(request, bars=tuple(bars), quotes=tuple(quotes))

    expected, research_reason = _research_result(request, side="BUY")
    candidate = evaluate_mtvclc(request)

    assert expected is None
    assert research_reason == expected_refusal
    assert research_reason == candidate.reasons[0]
    assert candidate.allowed is False
    assert candidate.reasons == (expected_refusal,)
    assert candidate.broker_trade_authorized is False


def test_runtime_event_time_uses_first_post_close_tick_without_claiming_legacy_parity() -> (
    None
):
    request = _request()
    delayed_epoch = SIGNAL_EPOCH + 60 + MAX_ENTRY_DELAY_SECONDS + 17
    request = replace(
        request,
        quotes=(replace(request.quotes[0], observed_epoch=delayed_epoch),),
    )

    expected, research_reason = _research_result(request, side="BUY")
    candidate = evaluate_mtvclc(request)

    assert research_reason == "contemporaneous_entry_quote_missing"
    assert expected is None
    assert candidate.allowed is True
    assert candidate.entry_epoch == delayed_epoch
    assert candidate.expected_entry_epoch == delayed_epoch
    assert candidate.entry_deadline_epoch == delayed_epoch + MAX_ENTRY_DELAY_SECONDS


def test_conversion_adjusted_p_star_and_bracket_geometry_match_frozen_formula() -> None:
    request = _request(conversion_applies=True)

    candidate = evaluate_mtvclc(request)

    cost = request.cost.recorded_cost_bps
    target = 4.0 * cost
    stop = 8.0 * cost
    rate = 0.005
    expected_p_star = (stop * (1.0 + rate) + cost) / (
        target * (1.0 - rate) + stop * (1.0 + rate)
    )
    assert candidate.allowed is True
    assert candidate.p_star == expected_p_star
    assert candidate.target_bps == target
    assert candidate.stop_bps == stop
    assert candidate.entry_price is not None
    assert candidate.target_price == candidate.entry_price * (1.0 + target / 1e4)
    assert candidate.stop_price == candidate.entry_price * (1.0 - stop / 1e4)


def test_type7_v90_uses_exactly_the_240_pre_signal_ivolumes() -> None:
    request = _request()
    bars = tuple(
        replace(bar, tick_volume=index) for index, bar in enumerate(request.bars[:-1])
    ) + (replace(request.bars[-1], tick_volume=300),)
    request = replace(request, bars=bars)

    expected, expected_reason = _research_result(request, side="BUY")
    candidate = evaluate_mtvclc(request)

    assert expected is not None, expected_reason
    assert candidate.allowed is True
    assert candidate.volume_v90 == pytest.approx(215.1)
    assert candidate.volume_v90 == expected.volume_v90


def test_first_authenticated_quote_at_or_after_close_is_the_immediate_trade_quote() -> (
    None
):
    request = _request()
    expected_entry = request.bars[-1].minute_epoch + 60
    before_close = _quote(observed_epoch=expected_entry - 1, mid=1.101)
    first_after_close = replace(
        _quote(observed_epoch=expected_entry + 2, mid=1.102),
        market_event_sequence=8,
    )
    request = replace(request, quotes=(before_close, first_after_close))

    expected, expected_reason = _research_result(request, side="BUY")
    candidate = evaluate_mtvclc(request)

    assert expected is not None, expected_reason
    assert candidate.allowed is True
    assert candidate.entry_epoch == expected_entry + 2
    assert candidate.entry_epoch == expected.entry_epoch
    assert candidate.entry_price == first_after_close.ask


def test_exact_241_observed_direct_bars_allow_gaps_but_refuse_reordering() -> None:
    request = _request()
    short = evaluate_mtvclc(replace(request, bars=request.bars[1:]))
    assert short.reasons == ("history_must_contain_exactly_241_completed_m1_bars",)

    gapped = [
        replace(bar, minute_epoch=bar.minute_epoch - 60) if index < 120 else bar
        for index, bar in enumerate(request.bars)
    ]
    assert evaluate_mtvclc(replace(request, bars=tuple(gapped))).allowed is True

    reordered = list(request.bars)
    reordered[119], reordered[120] = reordered[120], reordered[119]
    assert evaluate_mtvclc(replace(request, bars=tuple(reordered))).reasons == (
        "bars_not_strictly_time_ordered",
    )

    open_bar = list(request.bars)
    open_bar[-1] = replace(open_bar[-1], closed=False)
    assert evaluate_mtvclc(replace(request, bars=tuple(open_bar))).reasons == (
        "bar_not_closed",
    )


def test_bar_signal_cache_is_exact_content_keyed() -> None:
    mtvclc_module._bar_signal_evaluation_cached.cache_clear()
    request = _request()

    assert evaluate_mtvclc(request).allowed is True
    first = mtvclc_module._bar_signal_evaluation_cached.cache_info()
    assert (first.hits, first.misses) == (0, 1)

    assert evaluate_mtvclc(request).allowed is True
    repeated = mtvclc_module._bar_signal_evaluation_cached.cache_info()
    assert (repeated.hits, repeated.misses) == (1, 1)

    changed_bar = replace(
        request.bars[0],
        bid_high=request.bars[0].bid_high + 0.00001,
    )
    changed = replace(request, bars=(changed_bar, *request.bars[1:]))
    assert evaluate_mtvclc(changed).allowed is True
    changed_info = mtvclc_module._bar_signal_evaluation_cached.cache_info()
    assert (changed_info.hits, changed_info.misses) == (1, 2)

    changed_cost = replace(
        request,
        cost=replace(
            request.cost,
            commission_bps_per_round_trip=0.2,
        ),
    )
    assert evaluate_mtvclc(changed_cost).allowed is True
    changed_cost_info = mtvclc_module._bar_signal_evaluation_cached.cache_info()
    assert (changed_cost_info.hits, changed_cost_info.misses) == (1, 3)


def test_bar_signal_cache_falls_back_for_unhashable_malformed_fields() -> None:
    mtvclc_module._bar_signal_evaluation_cached.cache_clear()
    request = _request()
    malformed = replace(
        request.bars[-1],
        quality_flags=["tampered"],  # type: ignore[arg-type]
    )

    candidate = evaluate_mtvclc(replace(request, bars=(*request.bars[:-1], malformed)))

    assert candidate.allowed is False
    assert candidate.reasons == ("bar_quality_flags_present",)
    assert mtvclc_module._bar_signal_evaluation_cached.cache_info().currsize == 0


def test_runtime_bar_signal_cache_keeps_quote_evaluation_live() -> None:
    mtvclc_module._bar_signal_evaluation_cached.cache_clear()
    request = _request()
    prepared = replace(
        request,
        bars=mtvclc_module.runtime_prepared_bars(request.bars),
    )

    first = evaluate_mtvclc(prepared)
    first_info = mtvclc_module._bar_signal_evaluation_cached.cache_info()
    moved_quote = _quote(
        observed_epoch=SIGNAL_EPOCH + 60,
        mid=1.10300,
        spread_bps=0.5,
    )
    second = evaluate_mtvclc(replace(prepared, quotes=(moved_quote,)))
    second_info = mtvclc_module._bar_signal_evaluation_cached.cache_info()

    assert first.allowed is True
    assert second.allowed is True
    assert second.entry_price != first.entry_price
    assert (first_info.hits, first_info.misses) == (0, 1)
    assert (second_info.hits, second_info.misses) == (1, 1)


def test_runtime_quote_handoff_skips_only_the_matching_validated_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    bar_identity = request.bars[-1].source_identity
    quote = replace(request.quotes[0], source_identity=bar_identity)
    prepared_quotes = mtvclc_module._runtime_prepared_quotes(
        (quote,),
        symbol=request.symbol,
        source_identity=bar_identity,
    )
    original = mtvclc_module._quote_validation_reasons
    validation_calls = 0

    def counted(*args, **kwargs):
        nonlocal validation_calls
        validation_calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(mtvclc_module, "_quote_validation_reasons", counted)

    prepared = evaluate_mtvclc(replace(request, quotes=prepared_quotes))
    assert prepared.allowed is True
    assert validation_calls == 0

    foreign_identity = replace(
        bar_identity,
        producer_instance_id="different-terminal-instance",
    )
    foreign_quote = replace(quote, source_identity=foreign_identity)
    foreign_quotes = mtvclc_module._runtime_prepared_quotes(
        (foreign_quote,),
        symbol=request.symbol,
        source_identity=foreign_identity,
    )
    refused = evaluate_mtvclc(replace(request, quotes=foreign_quotes))
    assert refused.allowed is False
    assert refused.reasons == ("bar_quote_authenticated_source_mismatch",)
    assert validation_calls == 1


@pytest.mark.parametrize(
    "case",
    ("allowed", "volume_refusal", "spread_refusal"),
)
def test_runtime_prepared_bars_preserve_evaluation_payload(
    case: str,
) -> None:
    request = _request(spread_bps=1.5 if case == "spread_refusal" else 0.5)
    if case == "volume_refusal":
        request = replace(
            request,
            bars=(
                *request.bars[:-1],
                replace(request.bars[-1], tick_volume=50),
            ),
        )
    ordinary = evaluate_mtvclc(request)
    prepared = evaluate_mtvclc(
        replace(request, bars=mtvclc_module.runtime_prepared_bars(request.bars))
    )

    assert prepared == ordinary


def test_frozen_configuration_hash_and_exact_scope_match_preregistration() -> None:
    assert FROZEN_MTVCLC_POLICY.to_canonical_dict() == {
        "baseline_m1_bars": 240,
        "volume_quantile": 0.9,
        "close_location_threshold": 0.8,
        "target_cost_multiple": 4.0,
        "stop_cost_multiple": 8.0,
        "outcome_horizon_m1_bars": 30,
        "maximum_entry_delay_seconds": 5,
        "maximum_quote_gap_seconds": 5,
        "rollover_entry_blackout_start_second": 73_200,
        "rollover_entry_blackout_end_second": 79_800,
    }
    assert FROZEN_MTVCLC_POLICY.config_id == MTVCLC_CONFIG_ID
    assert FROZEN_MTVCLC_POLICY.to_canonical_dict() == asdict(research.GRID[0])
    assert FROZEN_MTVCLC_POLICY.config_sha256() == MTVCLC_CONFIG_SHA256
    assert MTVCLC_CONFIG_SHA256 == (
        "aac8e4bd98d71243b41993983d63ab1da1f8836b74b9243c7e60232f0eb00701"
    )
    assert MTVCLC_V1_SYMBOLS == IG_MT4_SCALP_SYMBOLS
    assert IG_MT4_SCALP_SCOPE_VERSION == "fxstack.ig_mt4.scalp_scope.v3"
    assert len(MTVCLC_V1_SYMBOLS) == 22
    assert "XRPUSD" not in MTVCLC_V1_SYMBOLS


def test_immutable_mtvclc_contract_digests_are_cached_by_exact_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    mtvclc_module._policy_config_sha256.cache_clear()
    mtvclc_module._market_source_identity_sha256.cache_clear()
    mtvclc_module._cost_calibration_row_sha256.cache_clear()
    original = mtvclc_module._canonical_sha256
    calls = 0

    def counted(payload):
        nonlocal calls
        calls += 1
        return original(payload)

    monkeypatch.setattr(mtvclc_module, "_canonical_sha256", counted)
    identity = request.bars[0].source_identity

    assert FROZEN_MTVCLC_POLICY.config_sha256() == FROZEN_MTVCLC_POLICY.config_sha256()
    assert identity.identity_sha256() == identity.identity_sha256()
    assert request.cost.row_sha256() == replace(request.cost).row_sha256()
    assert calls == 3

    changed = replace(request.cost, p90_spread_bps=request.cost.p90_spread_bps + 0.1)
    assert changed.row_sha256() != request.cost.row_sha256()
    assert calls == 4

    malformed = replace(request.cost, source_sha256=cast(str, []))
    assert len(malformed.row_sha256()) == 64
    assert calls == 5


def test_cost_validation_cache_is_exact_and_malformed_safe() -> None:
    mtvclc_module._valid_cost_calibration_cached.cache_clear()
    cost = _cost()

    assert mtvclc_module._valid_cost_calibration(
        cost,
        expected_symbol="EURUSD",
    )
    assert mtvclc_module._valid_cost_calibration(
        replace(cost),
        expected_symbol="EURUSD",
    )
    repeated = mtvclc_module._valid_cost_calibration_cached.cache_info()
    assert (repeated.hits, repeated.misses) == (1, 1)

    assert not mtvclc_module._valid_cost_calibration(
        cost,
        expected_symbol="GBPUSD",
    )
    changed = mtvclc_module._valid_cost_calibration_cached.cache_info()
    assert (changed.hits, changed.misses) == (1, 2)

    malformed = replace(cost, source_sha256=cast(str, []))
    assert not mtvclc_module._valid_cost_calibration(
        malformed,
        expected_symbol="EURUSD",
    )
    assert mtvclc_module._valid_cost_calibration_cached.cache_info() == changed


def test_evaluator_does_not_apply_the_retired_rollover_clock_veto() -> None:
    signal_epoch = int(datetime(2027, 2, 1, 20, 19, tzinfo=timezone.utc).timestamp())
    request = _request(signal_epoch=signal_epoch)

    candidate = evaluate_mtvclc(request)

    assert candidate.allowed is True
    assert candidate.reasons == ()


@pytest.mark.parametrize("symbol", MTVCLC_V1_SYMBOLS)
def test_every_scope_v3_symbol_reaches_the_same_pure_candidate_layer(
    symbol: str,
) -> None:
    candidate = evaluate_mtvclc(_request(symbol=symbol))

    assert candidate.allowed is True
    assert candidate.symbol == symbol
    assert candidate.instrument_id.endswith(f":{symbol}")
    assert candidate.venue_id == IG_MT4_VENUE_ID


def test_direct_mt4_and_authenticated_quote_identity_fail_closed() -> None:
    request = _request()
    bad_bar = replace(request.bars[-1], volume_source="provider_volume")
    candidate = evaluate_mtvclc(replace(request, bars=(*request.bars[:-1], bad_bar)))
    assert candidate.allowed is False
    assert candidate.reasons == ("bar_volume_source_not_direct_mt4_ivolume",)

    bad_quote = replace(request.quotes[0], source_event_token_sha256="")
    candidate = evaluate_mtvclc(replace(request, quotes=(bad_quote,)))
    assert candidate.allowed is False
    assert candidate.reasons == ("quote_event_identity_invalid",)


def test_cost_row_identity_changes_with_any_geometry_input() -> None:
    cost = _cost()
    changed = replace(cost, p90_spread_bps=cost.p90_spread_bps + 0.01)

    assert cost.row_sha256() != changed.row_sha256()


def test_candidate_contract_cannot_represent_a_pending_trade() -> None:
    candidate = evaluate_mtvclc(_request())
    candidate_fields = {item.name: item for item in fields(MTVCLCTradeCandidate)}

    assert candidate.allowed is True
    assert candidate_fields["execution_type"].init is False
    assert candidate_fields["pending_orders_forbidden"].init is False
    assert candidate.execution_type == "market"
    assert candidate.pending_orders_forbidden is True
    with pytest.raises(TypeError):
        MTVCLCTradeCandidate(
            symbol="EURUSD",
            instrument_id="fx:ig_mt4:EURUSD",
            venue_id="ig_mt4",
            allowed=False,
            reasons=("test",),
            execution_type="pending",  # type: ignore[call-arg]
        )


def test_non_frozen_configuration_refuses() -> None:
    changed = replace(FROZEN_MTVCLC_POLICY, volume_quantile=0.89)

    candidate = evaluate_mtvclc(_request(), policy=changed)

    assert candidate.allowed is False
    assert candidate.reasons == ("configuration_not_frozen",)


def test_production_module_has_no_research_or_io_dependency() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "strategy"
        / "mtvclc.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = ("fxstack.scalp", "fxstack.runtime", "requests", "pathlib")
    assert all(
        module != prefix and not module.startswith(f"{prefix}.")
        for module in imported
        for prefix in forbidden
    )


def test_policy_type_is_exactly_frozen_for_static_callers() -> None:
    assert MTVCLCPolicy() == FROZEN_MTVCLC_POLICY
    assert MTVCLC_STRATEGY_ID == ("ig_mt4_tick_volume_close_location_continuation")
    assert MTVCLC_STRATEGY_VERSION == "mtvclc.v1"
