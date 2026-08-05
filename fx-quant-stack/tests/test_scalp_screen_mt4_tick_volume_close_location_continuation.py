"""Synthetic-only tests for the frozen research-only MTVCLC-v1 evaluator."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import inspect
import math
from datetime import datetime, timezone

import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
import fxstack.scalp.screen_mt4_tick_volume_close_location_continuation as mtvclc


BASE_EPOCH = 1_800_000_000 - (1_800_000_000 % 60)


def _bar(
    index: int,
    *,
    bid_open: float = 100.0,
    bid_high: float = 100.02,
    bid_low: float = 99.98,
    bid_close: float = 100.0,
    tick_volume: int = 100,
    epoch_shift: int = 0,
) -> mtvclc.MT4BidBar:
    return mtvclc.MT4BidBar(
        epoch=BASE_EPOCH + index * 60 + epoch_shift,
        bid_open=bid_open,
        bid_high=bid_high,
        bid_low=bid_low,
        bid_close=bid_close,
        tick_volume=tick_volume,
    )


def _bars(count: int = 300) -> list[mtvclc.MT4BidBar]:
    return [_bar(index) for index in range(count)]


def _signal_bar(index: int, side: str, *, volume: int = 150) -> mtvclc.MT4BidBar:
    if side == "BUY":
        return _bar(
            index,
            bid_open=100.0,
            bid_high=100.031,
            bid_low=99.995,
            bid_close=100.03,
            tick_volume=volume,
        )
    if side == "SELL":
        return _bar(
            index,
            bid_open=100.0,
            bid_high=100.005,
            bid_low=99.969,
            bid_close=99.97,
            tick_volume=volume,
        )
    raise ValueError(side)


def _quote(
    index: int,
    *,
    mid: float = 100.0,
    spread_bps: float = 0.5,
    source_event_token_sha256: str | None = None,
    market_event_sequence: int | None = None,
) -> mtvclc.MT4Quote:
    half = mid * spread_bps / 2e4
    return mtvclc.MT4Quote(
        epoch=index,
        bid=mid - half,
        ask=mid + half,
        source_event_token_sha256=source_event_token_sha256,
        market_event_sequence=market_event_sequence,
    )


def _utc_epoch(
    year: int, month: int, day: int, hour: int, minute: int, second: int = 0
) -> int:
    return int(
        datetime(
            year,
            month,
            day,
            hour,
            minute,
            second,
            tzinfo=timezone.utc,
        ).timestamp()
    )


def _cost(symbol: str = "EURUSD", *, p90: float = 1.0) -> mtvclc.MT4CostCalibration:
    pnl_currency = symbol[-3:]
    return mtvclc.MT4CostCalibration(
        symbol=symbol,
        p90_spread_bps=p90,
        commission_bps_per_round_trip=0.0,
        financing_bps_per_trade=0.0,
        account_currency="USD",
        pnl_currency=pnl_currency,
        convert_on_close_charge_fraction=(0.0 if pnl_currency == "USD" else 0.005),
        source_sha256=hashlib.sha256(f"cost:{symbol}".encode()).hexdigest(),
    )


def _closed(
    side: str = "BUY",
) -> tuple[list[mtvclc.MT4BidBar], mtvclc.MTVCLCClosedSignal]:
    bars = _bars()
    bars[240] = _signal_bar(240, side)
    closed, reason = mtvclc.evaluate_closed_signal(
        prepared=mtvclc.prepare_bars(bars),
        signal_index=240,
        symbol="EURUSD",
        side=side,
        cost=_cost(),
    )
    assert closed is not None, reason
    return bars, closed


def _entry(side: str = "BUY") -> mtvclc.MTVCLCSignal:
    _bars_value, closed = _closed(side)
    quote = _quote(closed.expected_entry_epoch, mid=100.0, spread_bps=0.5)
    signal, reason = mtvclc.attach_entry(closed=closed, quotes=(quote,))
    assert signal is not None, reason
    return signal


def test_scope_grid_and_attempt_accounting_are_exact() -> None:
    assert mtvclc.MTVCLC_SYMBOLS == IG_MT4_SCALP_SYMBOLS
    assert len(mtvclc.GRID) == 1
    assert mtvclc.IMMUTABLE_PRIOR_ATTEMPTED_CELLS == 4_654
    assert mtvclc.IMMUTABLE_CURRENT_ATTEMPTED_CELLS == 44
    assert mtvclc.IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS == 4_698
    manifest = mtvclc.attempt_manifest()
    assert manifest["parameter_configurations"] == 1
    assert manifest["prior_attempted_cells_lower_bound"] == 4_654
    assert manifest["current_attempted_cells"] == 44
    assert manifest["cumulative_attempted_cells_lower_bound"] == 4_698
    assert manifest["provider_volume_interchangeable"] is False
    assert manifest["configuration"]["execution_type"] == "market"
    assert manifest["configuration"]["pending_orders_forbidden"] is True
    assert manifest["configuration"]["convert_on_close_charge_fraction"] == 0.005
    assert manifest["configuration"]["quote_gap_clock"] == (
        "authenticated_transport_snapshot_epoch"
    )
    assert manifest["configuration"]["repeated_unchanged_broker_event_allowed"]
    rollover = manifest["configuration"]["rollover_guard"]
    assert rollover["entry_blackout_start_second"] == 20 * 3600 + 20 * 60
    assert rollover["entry_blackout_end_second"] == 22 * 3600 + 10 * 60
    assert rollover["entry_blackout_utc"] == "[20:20:00,22:10:00)"
    assert rollover["signals_inside_blackout_reserve"] is False
    assert manifest["mt4_history_export_required"] is True
    assert manifest["contemporaneous_tick_outcomes_required"] is True
    assert manifest["anytime_valid"] is False
    assert manifest["order_authorized"] is False


def test_cost_calibration_requires_explicit_ig_currency_conversion_treatment() -> None:
    usd = _cost("EURUSD")
    cross = _cost("CADJPY")

    assert mtvclc.validate_cost_calibration(usd, expected_symbol="EURUSD")
    assert mtvclc.validate_cost_calibration(cross, expected_symbol="CADJPY")
    assert usd.break_even_win_probability == pytest.approx(0.75)
    assert cross.break_even_win_probability > 0.75
    assert not mtvclc.validate_cost_calibration(
        dataclasses.replace(cross, convert_on_close_charge_fraction=0.0),
        expected_symbol="CADJPY",
    )


def test_input_contract_is_mt4_ivolume_and_authentic_bid_ohlc_only() -> None:
    assert mtvclc.ACTIVITY_METRIC_ID == "mt4_m1_ivolume_tick_volume.v1"
    assert "ig_mt4" in mtvclc.SOURCE_CONTRACT_ID
    assert "ivolume" in mtvclc.SOURCE_CONTRACT_ID
    fields = {field.name for field in dataclasses.fields(mtvclc.MT4BidBar)}
    assert fields == {
        "epoch",
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "tick_volume",
    }
    assert not any("ask" in field or "provider" in field for field in fields)
    assert mtvclc.validate_bid_bar(_bar(0))
    assert not mtvclc.validate_bid_bar(dataclasses.replace(_bar(0), tick_volume=-1))
    assert not mtvclc.validate_bid_bar(
        dataclasses.replace(_bar(0), tick_volume=1.5)  # type: ignore[arg-type]
    )


def test_type7_baseline_is_exactly_t_minus_240_through_t_minus_1() -> None:
    bars = [_bar(index, tick_volume=index) for index in range(300)]
    prepared = mtvclc.prepare_bars(bars)
    assert mtvclc.baseline_volume_v90(prepared, signal_index=240) == pytest.approx(
        215.1
    )
    outside = list(bars)
    outside[240] = dataclasses.replace(outside[240], tick_volume=1_000_000)
    assert mtvclc.baseline_volume_v90(
        mtvclc.prepare_bars(outside), signal_index=240
    ) == pytest.approx(215.1)
    inside = list(bars)
    inside[200] = dataclasses.replace(inside[200], tick_volume=1_000_000)
    assert mtvclc.baseline_volume_v90(
        mtvclc.prepare_bars(inside), signal_index=240
    ) != pytest.approx(215.1)


def test_gapped_241_bar_context_is_rejected() -> None:
    bars = _bars()
    bars = [
        dataclasses.replace(bar, epoch=bar.epoch + (60 if index >= 200 else 0))
        for index, bar in enumerate(bars)
    ]
    prepared = mtvclc.prepare_bars(bars)
    assert mtvclc.baseline_volume_v90(prepared, signal_index=240) is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_signal_is_exact_buy_sell_mirror_and_cost_scaled(side: str) -> None:
    _bars_value, closed = _closed(side)
    assert closed.side == side
    assert closed.signal_tick_volume == 150
    assert closed.volume_v90 == 100
    assert closed.bid_body_bps == pytest.approx(3.0)
    assert closed.bid_close_location > 0.80
    assert closed.recorded_cost_bps == pytest.approx(2.0)
    assert closed.target_bps == pytest.approx(8.0)
    assert closed.stop_bps == pytest.approx(16.0)
    assert closed.p_star == pytest.approx(0.75)


def test_volume_body_and_close_location_boundaries_fail_closed() -> None:
    bars = _bars()
    bars[240] = _signal_bar(240, "BUY", volume=100)
    signal, reason = mtvclc.evaluate_closed_signal(
        prepared=mtvclc.prepare_bars(bars),
        signal_index=240,
        symbol="EURUSD",
        side="BUY",
        cost=_cost(),
    )
    assert signal is None and reason == "tick_volume_not_strictly_above_v90"

    bars[240] = _bar(
        240,
        bid_open=100.0,
        bid_high=100.021,
        bid_low=99.995,
        bid_close=100.019,
        tick_volume=150,
    )
    signal, reason = mtvclc.evaluate_closed_signal(
        prepared=mtvclc.prepare_bars(bars),
        signal_index=240,
        symbol="EURUSD",
        side="BUY",
        cost=_cost(),
    )
    assert signal is None and reason == "bid_body_below_recorded_cost"

    bars[240] = _bar(
        240,
        bid_open=100.0,
        bid_high=100.04,
        bid_low=99.99,
        bid_close=100.025,
        tick_volume=150,
    )
    signal, reason = mtvclc.evaluate_closed_signal(
        prepared=mtvclc.prepare_bars(bars),
        signal_index=240,
        symbol="EURUSD",
        side="BUY",
        cost=_cost(),
    )
    assert signal is None and reason == "bid_close_location_below_threshold"


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_entry_uses_first_contemporaneous_executable_tick_and_market_side(
    side: str,
) -> None:
    _bars_value, closed = _closed(side)
    stale = _quote(closed.expected_entry_epoch - 1, mid=99.0)
    current = _quote(closed.expected_entry_epoch + 2, mid=100.0, spread_bps=0.5)
    signal, reason = mtvclc.attach_entry(closed=closed, quotes=(stale, current))
    assert signal is not None, reason
    assert signal.entry_epoch == current.epoch
    assert signal.entry_price == (current.ask if side == "BUY" else current.bid)
    if side == "BUY":
        assert signal.target_price > signal.entry_price > signal.stop_price
    else:
        assert signal.target_price < signal.entry_price < signal.stop_price


def test_entry_requires_fresh_tick_and_rejects_spread_above_frozen_p90() -> None:
    _bars_value, closed = _closed()
    late = _quote(closed.expected_entry_epoch + 6, spread_bps=0.5)
    signal, reason = mtvclc.attach_entry(closed=closed, quotes=(late,))
    assert signal is None and reason == "contemporaneous_entry_quote_missing"
    wide = _quote(closed.expected_entry_epoch, spread_bps=1.01)
    signal, reason = mtvclc.attach_entry(closed=closed, quotes=(wide,))
    assert signal is None and reason == "live_spread_above_frozen_p90"


@pytest.mark.parametrize(
    ("epoch", "expected"),
    [
        (_utc_epoch(2026, 1, 15, 20, 19, 59), False),
        (_utc_epoch(2026, 1, 15, 20, 20, 0), True),
        # 21:00 UTC is the 22:00 London boundary during BST.
        (_utc_epoch(2026, 7, 15, 21, 0, 0), True),
        # 22:00 UTC is the 22:00 London boundary during GMT.
        (_utc_epoch(2026, 1, 15, 22, 0, 0), True),
        (_utc_epoch(2026, 1, 15, 22, 9, 59), True),
        (_utc_epoch(2026, 1, 15, 22, 10, 0), False),
    ],
)
def test_fixed_utc_rollover_blackout_covers_gmt_bst_and_exact_boundaries(
    epoch: int, expected: bool
) -> None:
    assert mtvclc.entry_in_rollover_blackout(epoch) is expected


def _bars_for_expected_entry(entry_epoch: int) -> list[mtvclc.MT4BidBar]:
    first_epoch = entry_epoch - 241 * 60
    bars = [
        dataclasses.replace(_bar(index), epoch=first_epoch + index * 60)
        for index in range(241)
    ]
    bars[240] = dataclasses.replace(
        _signal_bar(240, "BUY"),
        epoch=entry_epoch - 60,
    )
    return bars


def test_signal_with_expected_entry_inside_rollover_blackout_does_not_reserve() -> None:
    entry_epoch = _utc_epoch(2026, 7, 15, 20, 20)
    bars = _bars_for_expected_entry(entry_epoch)
    closed, reason = mtvclc.evaluate_closed_signal(
        prepared=mtvclc.prepare_bars(bars),
        signal_index=240,
        symbol="EURUSD",
        side="BUY",
        cost=_cost(),
    )
    assert closed is None
    assert reason == "entry_in_fixed_utc_rollover_blackout"

    scope_bars, quotes, costs, hashes = _quiet_scope_inputs()
    scope_bars["EURUSD"] = bars
    quotes["EURUSD"] = [_quote(entry_epoch, mid=100.0)]
    result = mtvclc.screen_universe(
        bars_by_symbol=scope_bars,
        quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        source_sha256_by_symbol=hashes,
        source_contract_id=mtvclc.SOURCE_CONTRACT_ID,
    )
    assert not [
        row for row in result["reservation_ledger"] if row["symbol"] == "EURUSD"
    ]


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_outcome_uses_executable_tick_side_and_hits_target(side: str) -> None:
    signal = _entry(side)
    if side == "BUY":
        bid = signal.target_price + 0.001
        quote = mtvclc.MT4Quote(
            epoch=signal.entry_epoch + 1, bid=bid, ask=bid + 0.001
        )
    else:
        ask = signal.target_price - 0.001
        quote = mtvclc.MT4Quote(
            epoch=signal.entry_epoch + 1, bid=ask - 0.001, ask=ask
        )
    outcome = mtvclc.score_signal(signal, quotes=(quote,))
    assert outcome.exit_reason == "TAKE_PROFIT"
    assert outcome.full_target_hit_first is True
    assert outcome.gross_quote_bps == pytest.approx(signal.target_bps)
    assert outcome.net_bps == pytest.approx(
        signal.target_bps - signal.recorded_cost_bps
    )


def test_cross_currency_outcome_debits_ig_convert_on_close_charge() -> None:
    signal = dataclasses.replace(
        _entry("BUY"),
        symbol="CADJPY",
        convert_on_close_charge_fraction=0.005,
    )
    bid = signal.target_price + 0.001
    outcome = mtvclc.score_signal(
        signal,
        quotes=(
            mtvclc.MT4Quote(
                epoch=signal.entry_epoch + 1,
                bid=bid,
                ask=bid + 0.001,
            ),
        ),
    )

    assert outcome.exit_reason == "TAKE_PROFIT"
    assert outcome.currency_conversion_debit_bps == pytest.approx(
        signal.target_bps * 0.005
    )
    assert outcome.net_bps == pytest.approx(
        signal.target_bps
        - signal.recorded_cost_bps
        - signal.target_bps * 0.005
    )


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_stop_gap_is_adverse_and_not_capped(side: str) -> None:
    signal = _entry(side)
    if side == "BUY":
        bid = signal.stop_price - 0.01
        quote = mtvclc.MT4Quote(
            epoch=signal.entry_epoch + 1, bid=bid, ask=bid + 0.001
        )
    else:
        ask = signal.stop_price + 0.01
        quote = mtvclc.MT4Quote(
            epoch=signal.entry_epoch + 1, bid=ask - 0.001, ask=ask
        )
    outcome = mtvclc.score_signal(signal, quotes=(quote,))
    assert outcome.exit_reason == "STOP_LOSS"
    assert outcome.full_target_hit_first is False
    assert outcome.gross_quote_bps < -signal.stop_bps


def test_quote_gap_and_incomplete_horizon_are_adverse() -> None:
    signal = _entry()
    gap = _quote(signal.entry_epoch + 6, mid=100.0)
    outcome = mtvclc.score_signal(signal, quotes=(gap,))
    assert outcome.exit_reason == "QUOTE_GAP_ADVERSE"
    assert outcome.gross_quote_bps == -signal.stop_bps
    incomplete = mtvclc.score_signal(signal, quotes=())
    assert incomplete.exit_reason == "INCOMPLETE_HORIZON_ADVERSE"
    assert incomplete.net_bps == -(signal.stop_bps + signal.recorded_cost_bps)


def test_repeated_broker_event_can_fill_continuous_quiet_transport_seconds() -> None:
    signal = _entry()
    token_hash = hashlib.sha256(b"opaque-broker-server-event").hexdigest()
    snapshots = tuple(
        _quote(
            signal.entry_epoch + offset,
            mid=100.0,
            spread_bps=0.5,
            source_event_token_sha256=token_hash,
            market_event_sequence=7,
        )
        for offset in range(
            mtvclc.MAX_QUOTE_GAP_SECONDS,
            mtvclc.OUTCOME_HORIZON_M1_BARS * 60 + 1,
            mtvclc.MAX_QUOTE_GAP_SECONDS,
        )
    )
    prepared = mtvclc.prepare_quotes(snapshots)
    assert len({quote.source_event_token_sha256 for quote in prepared}) == 1
    outcome = mtvclc.score_signal(signal, quotes=prepared)
    assert outcome.exit_reason == "TIME_STOP"


def test_opaque_broker_identity_and_process_local_sequence_are_not_utc_clocks() -> None:
    first_hash = hashlib.sha256(b"broker-wall-clock-ahead-of-utc").hexdigest()
    second_hash = hashlib.sha256(b"broker-wall-clock-after-dst-step").hexdigest()
    first = _quote(
        BASE_EPOCH,
        source_event_token_sha256=first_hash,
        market_event_sequence=7,
    )
    repeated = _quote(
        BASE_EPOCH + 1,
        source_event_token_sha256=first_hash,
        market_event_sequence=7,
    )
    assert mtvclc.prepare_quotes((first, repeated)) == (first, repeated)
    after_api_restart = _quote(
        BASE_EPOCH + 2,
        source_event_token_sha256=second_hash,
        market_event_sequence=0,
    )
    assert mtvclc.prepare_quotes((first, repeated, after_api_restart)) == (
        first,
        repeated,
        after_api_restart,
    )


def _quiet_scope_inputs() -> tuple[
    dict[str, list[mtvclc.MT4BidBar]],
    dict[str, list[mtvclc.MT4Quote]],
    dict[str, mtvclc.MT4CostCalibration],
    dict[str, str],
]:
    bars = {symbol: _bars(241) for symbol in mtvclc.MTVCLC_SYMBOLS}
    quotes = {
        symbol: [_quote(BASE_EPOCH + 241 * 60, mid=100.0)]
        for symbol in mtvclc.MTVCLC_SYMBOLS
    }
    costs = {symbol: _cost(symbol) for symbol in mtvclc.MTVCLC_SYMBOLS}
    hashes = {
        symbol: hashlib.sha256(f"bars-and-ticks:{symbol}".encode()).hexdigest()
        for symbol in mtvclc.MTVCLC_SYMBOLS
    }
    return bars, quotes, costs, hashes


def test_screen_emits_exact_44_research_only_cells_and_validates_bundle() -> None:
    bars, quotes, costs, hashes = _quiet_scope_inputs()
    result = mtvclc.screen_universe(
        bars_by_symbol=bars,
        quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        source_sha256_by_symbol=hashes,
        source_contract_id=mtvclc.SOURCE_CONTRACT_ID,
    )
    assert result["source_scope_ready"] is True
    assert len(result["cells"]) == 44
    assert {
        (cell["symbol"], cell["side"]) for cell in result["cells"]
    } == {
        (symbol, side)
        for symbol in mtvclc.MTVCLC_SYMBOLS
        for side in ("BUY", "SELL")
    }
    assert result["reservation_ledger"] == []
    assert result["outcome_ledger"] == []
    assert result["all_cells_pass_fixed_screen"] is False
    assert mtvclc.validate_result_bundle(result)
    for authority in (
        "success_claim_authorized",
        "holdout_access_authorized",
        "promotion_authorized",
        "activation_authorized",
        "registry_write_authorized",
        "runtime_authorized",
        "order_authorized",
    ):
        forged = copy.deepcopy(result)
        forged[authority] = True
        assert not mtvclc.validate_result_bundle(forged), authority


def test_wrong_provider_contract_fails_closed_without_outcomes() -> None:
    bars, quotes, costs, hashes = _quiet_scope_inputs()
    result = mtvclc.screen_universe(
        bars_by_symbol=bars,
        quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        source_sha256_by_symbol=hashes,
        source_contract_id="dukascopy_bid_plus_ask_volume",
    )
    assert result["source_scope_ready"] is False
    assert result["reservation_ledger"] == []
    assert result["outcome_ledger"] == []
    assert all(cell["source_ready"] is False for cell in result["cells"])
    assert not mtvclc.validate_result_bundle(result)


def test_first_full_signal_consumes_one_symbol_utc_day() -> None:
    bars, quotes, costs, hashes = _quiet_scope_inputs()
    eurusd = _bars(244)
    eurusd[240] = _signal_bar(240, "BUY")
    eurusd[242] = _signal_bar(242, "BUY")
    bars["EURUSD"] = eurusd
    first_close = eurusd[240].epoch + 60
    second_close = eurusd[242].epoch + 60
    entry_one = _quote(first_close, mid=100.0, spread_bps=0.5)
    entry_two = _quote(second_close, mid=100.0, spread_bps=0.5)
    # The first outcome reaches its target immediately; the second signal is
    # nevertheless suppressed by the immutable one-entry-per-symbol/day rule.
    _bars_value, closed = _closed("BUY")
    synthetic_entry, reason = mtvclc.attach_entry(
        closed=dataclasses.replace(
            closed,
            signal_epoch=eurusd[240].epoch,
            expected_entry_epoch=first_close,
            entry_day=mtvclc._utc_day(first_close),
        ),
        quotes=(entry_one,),
    )
    assert synthetic_entry is not None, reason
    target_bid = synthetic_entry.target_price + 0.001
    target = mtvclc.MT4Quote(
        epoch=first_close + 1,
        bid=target_bid,
        ask=target_bid + 0.001,
    )
    quotes["EURUSD"] = [entry_one, target, entry_two]
    result = mtvclc.screen_universe(
        bars_by_symbol=bars,
        quotes_by_symbol=quotes,
        costs_by_symbol=costs,
        source_sha256_by_symbol=hashes,
        source_contract_id=mtvclc.SOURCE_CONTRACT_ID,
    )
    eurusd_reservations = [
        item for item in result["reservation_ledger"] if item["symbol"] == "EURUSD"
    ]
    assert len(eurusd_reservations) == 1
    assert len(
        [item for item in result["outcome_ledger"] if item["symbol"] == "EURUSD"]
    ) == 1


def test_module_has_no_live_or_external_data_access_surface() -> None:
    source = inspect.getsource(mtvclc)
    for forbidden in (
        "import requests",
        "import sqlalchemy",
        "fxstack.runtime",
        "OrderSend",
        "OP_BUYLIMIT",
        "OP_SELLLIMIT",
        "dukascopy_python",
    ):
        assert forbidden not in source
    assert "no CLI" in source
    assert "order_authorized\": False" in source


def test_cost_calibration_requires_hash_and_fixed_one_bp_debit() -> None:
    assert mtvclc.validate_cost_calibration(_cost(), expected_symbol="EURUSD")
    assert not mtvclc.validate_cost_calibration(
        dataclasses.replace(_cost(), source_sha256=""), expected_symbol="EURUSD"
    )
    assert not mtvclc.validate_cost_calibration(
        dataclasses.replace(_cost(), adverse_execution_debit_bps=0.5),
        expected_symbol="EURUSD",
    )
    assert not mtvclc.validate_cost_calibration(
        _cost("GBPUSD"), expected_symbol="EURUSD"
    )


def test_nonfinite_and_invalid_geometries_are_rejected() -> None:
    assert not mtvclc.validate_bid_bar(
        dataclasses.replace(_bar(0), bid_high=math.inf)
    )
    assert not mtvclc.validate_bid_bar(
        dataclasses.replace(_bar(0), bid_low=101.0)
    )
    assert not mtvclc.validate_quote(
        dataclasses.replace(_quote(BASE_EPOCH), ask=99.0)
    )
    with pytest.raises(ValueError, match="strictly time ordered"):
        mtvclc.prepare_quotes(
            (_quote(BASE_EPOCH), _quote(BASE_EPOCH))
        )
