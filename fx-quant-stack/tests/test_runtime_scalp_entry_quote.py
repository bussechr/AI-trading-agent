from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timezone
import math

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_CATALOG,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.scalp_entry_qualification import (
    SCALP_DISLOCATION_CONFIG_SHA256,
    QualifiedScalpEntryCandidate,
)
from fxstack.runtime.scalp_entry_quote import (
    SCALP_ENTRY_QUOTE_SCHEMA_VERSION,
    refresh_scalp_entry_quote,
)
from fxstack.schemas.entry import EntryProposal
from fxstack.strategy.scalp_dislocation import (
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    SCALP_EXECUTION_DEBIT_BPS,
)


NOW = 1_800_000_000.5
MAX_AGE = 2.0


def _qualified_candidate(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    ref_mid: float = 1.1000,
    entry_price: float | None = None,
    stop_bps: float = 5.0,
    target_bps: float = 10.0,
    signal_spread_bps: float = 1.0,
    lower_bound: float = 0.70,
) -> QualifiedScalpEntryCandidate:
    instrument = IG_MT4_SCALP_CATALOG[symbol]
    selected_entry = (
        ref_mid + 0.0001 if side == "BUY" else ref_mid - 0.0001
    ) if entry_price is None else entry_price
    stop_distance = ref_mid * stop_bps / 1e4
    target_distance = ref_mid * target_bps / 1e4
    sl_price = (
        selected_entry - stop_distance
        if side == "BUY"
        else selected_entry + stop_distance
    )
    tp_price = (
        selected_entry + target_distance
        if side == "BUY"
        else selected_entry - target_distance
    )
    proposal = EntryProposal(
        strategy_id=SCALP_DISLOCATION_STRATEGY_ID,
        strategy_version=SCALP_DISLOCATION_STRATEGY_VERSION,
        config_sha256=SCALP_DISLOCATION_CONFIG_SHA256,
        symbol=symbol,
        instrument_id=instrument.instrument_id,
        venue_id=IG_MT4_VENUE_ID,
        source_id="ig_mt4_bridge_m1",
        source_version="bridge-bars-v1",
        allowed=True,
        reasons=(),
        side=side,  # type: ignore[arg-type]
        minute_epoch=29_999_999,
        ref_mid=ref_mid,
        entry_price=selected_entry,
        sl_price=sl_price,
        tp_price=tp_price,
        atr_bps=6.0,
        stop_bps=stop_bps,
        target_bps=target_bps,
        disp_z=2.5,
        spread_bps=signal_spread_bps,
        p_star=(stop_bps + signal_spread_bps + SCALP_EXECUTION_DEBIT_BPS)
        / (target_bps + stop_bps),
        time_stop_bars=20,
        entry_deadline_epoch=int(NOW) + 5,
    )
    expected_edge = (
        lower_bound * target_bps
        - (1.0 - lower_bound) * stop_bps
        - signal_spread_bps
        - SCALP_EXECUTION_DEBIT_BPS
    )
    return QualifiedScalpEntryCandidate(
        proposal=proposal,
        win_probability_lower_bound=lower_bound,
        conservative_expected_edge_bps=expected_edge,
        reward_risk_ratio=target_bps / stop_bps,
    )


def _tick(
    *,
    symbol: str = "EURUSD",
    bid: float = 1.10015,
    ask: float = 1.10025,
    timestamp: object = NOW - 0.5,
) -> dict[str, object]:
    return {
        "instrument": {
            "canonical_symbol": symbol,
            "venue": IG_MT4_VENUE_ID,
        },
        "bid": bid,
        "ask": ask,
        "market_event_received_at_epoch": timestamp,
    }


@pytest.mark.parametrize(
    ("side", "bid", "ask", "expected_entry", "expected_slippage"),
    [
        ("BUY", 1.10015, 1.10025, 1.10025, (1.10025 - 1.1001) / 1.1 * 1e4),
        ("SELL", 1.09975, 1.09985, 1.09975, (1.0999 - 1.09975) / 1.1 * 1e4),
    ],
)
def test_adverse_side_quote_reanchors_strategy_distances(
    side: str,
    bid: float,
    ask: float,
    expected_entry: float,
    expected_slippage: float,
) -> None:
    candidate = _qualified_candidate(side=side)

    result = refresh_scalp_entry_quote(
        candidate,
        _tick(bid=bid, ask=ask),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.reasons == ()
    refreshed = result.refreshed_candidate
    assert refreshed is not None
    assert refreshed.qualified_candidate is candidate
    assert refreshed.proposal is candidate.proposal
    assert refreshed.symbol == "EURUSD"
    assert refreshed.side == side
    assert refreshed.win_probability_lower_bound == pytest.approx(0.70)
    assert refreshed.refreshed_entry_price == pytest.approx(expected_entry)
    assert refreshed.adverse_slippage_bps == pytest.approx(expected_slippage)
    assert refreshed.stop_distance_price == pytest.approx(1.1 * 5.0 / 1e4)
    assert refreshed.target_distance_price == pytest.approx(1.1 * 10.0 / 1e4)
    if side == "BUY":
        assert refreshed.refreshed_sl_price == pytest.approx(
            expected_entry - 1.1 * 5.0 / 1e4
        )
        assert refreshed.refreshed_tp_price == pytest.approx(
            expected_entry + 1.1 * 10.0 / 1e4
        )
    else:
        assert refreshed.refreshed_sl_price == pytest.approx(
            expected_entry + 1.1 * 5.0 / 1e4
        )
        assert refreshed.refreshed_tp_price == pytest.approx(
            expected_entry - 1.1 * 10.0 / 1e4
        )

    mid = bid + (ask - bid) / 2.0
    spread_bps = (ask - bid) / mid * 1e4
    assert refreshed.mid_price == pytest.approx(mid)
    assert refreshed.current_spread_bps == pytest.approx(spread_bps)
    assert refreshed.total_current_cost_bps == pytest.approx(
        spread_bps + SCALP_EXECUTION_DEBIT_BPS
    )
    assert refreshed.live_p_star == pytest.approx(
        (5.0 + spread_bps + SCALP_EXECUTION_DEBIT_BPS) / 15.0
    )
    assert refreshed.conservative_expected_edge_bps == pytest.approx(
        0.70 * 10.0 - 0.30 * 5.0 - spread_bps - SCALP_EXECUTION_DEBIT_BPS
    )
    assert refreshed.reward_risk_ratio == pytest.approx(2.0)
    assert result.diagnostics.schema_version == SCALP_ENTRY_QUOTE_SCHEMA_VERSION
    assert result.diagnostics.to_dict()["accepted"] is True


@pytest.mark.parametrize(
    ("side", "bid", "ask"),
    [
        ("BUY", 1.0998, 1.0999),
        ("SELL", 1.1001, 1.1002),
    ],
)
def test_favourable_quote_does_not_improve_signal_entry(
    side: str,
    bid: float,
    ask: float,
) -> None:
    candidate = _qualified_candidate(side=side)

    result = refresh_scalp_entry_quote(
        candidate,
        _tick(bid=bid, ask=ask),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.refreshed_candidate is not None
    assert result.refreshed_candidate.refreshed_entry_price == pytest.approx(
        candidate.proposal.entry_price
    )
    assert result.refreshed_candidate.adverse_slippage_bps == pytest.approx(0.0)


def test_reanchored_adverse_slippage_is_not_double_counted_as_cost() -> None:
    candidate = _qualified_candidate()
    tick = _tick(bid=1.1999, ask=1.2000)

    result = refresh_scalp_entry_quote(
        candidate,
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    refreshed = result.refreshed_candidate
    assert refreshed is not None
    assert refreshed.adverse_slippage_bps > 900.0
    assert refreshed.total_current_cost_bps == pytest.approx(
        refreshed.current_spread_bps + SCALP_EXECUTION_DEBIT_BPS
    )
    assert refreshed.conservative_expected_edge_bps == pytest.approx(
        0.70 * 10.0
        - 0.30 * 5.0
        - refreshed.current_spread_bps
        - SCALP_EXECUTION_DEBIT_BPS
    )


@pytest.mark.parametrize(
    "identity",
    [
        {"canonical_symbol": "EURUSD", "venue": IG_MT4_VENUE_ID},
        {"symbol": "EURUSD", "venue_id": IG_MT4_VENUE_ID},
        {"pair": "EURUSD", "broker_venue_id": IG_MT4_VENUE_ID},
        {
            "instrument": {
                "canonical_symbol": "EURUSD",
                "venue": IG_MT4_VENUE_ID,
            }
        },
        {
            "canonical_symbol": "EURUSD",
            "instrument": {
                "canonical_symbol": "EURUSD",
                "pair": "EURUSD",
                "venue": IG_MT4_VENUE_ID,
            },
        },
    ],
)
def test_supported_canonical_identity_shapes(identity: dict[str, object]) -> None:
    tick = {
        **identity,
        "bid": 1.10015,
        "ask": 1.10025,
        "ts_epoch": NOW - 0.5,
    }

    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.diagnostics.tick_symbol == "EURUSD"
    assert result.diagnostics.tick_venue_id == IG_MT4_VENUE_ID


def test_crypto_empty_pair_alias_does_not_conflict_with_canonical_symbol() -> None:
    candidate = _qualified_candidate(
        symbol="BTCUSD",
        ref_mid=50_000.0,
        entry_price=50_005.0,
    )
    tick = {
        "instrument": {
            "canonical_symbol": "BTCUSD",
            "pair": "",
            "venue": IG_MT4_VENUE_ID,
        },
        "bid": 50_004.0,
        "ask": 50_006.0,
        "ts_epoch": NOW - 0.5,
    }

    result = refresh_scalp_entry_quote(
        candidate,
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.refreshed_candidate is not None
    assert result.refreshed_candidate.refreshed_entry_price == pytest.approx(50_006.0)


@pytest.mark.parametrize(
    ("tick_update", "expected_reason"),
    [
        ({"instrument": {}}, "scalp_entry_quote_tick_symbol_missing"),
        (
            {"canonical_symbol": "GBPUSD"},
            "scalp_entry_quote_tick_symbol_conflict",
        ),
        (
            {"instrument": {"canonical_symbol": "GBPUSD", "venue": "ig_mt4"}},
            "scalp_entry_quote_tick_symbol_mismatch",
        ),
        (
            {"instrument": {"canonical_symbol": "EURUSD"}},
            "scalp_entry_quote_tick_venue_missing",
        ),
        (
            {"instrument": {"canonical_symbol": "EURUSD", "venue": "otc"}},
            "scalp_entry_quote_tick_venue_invalid",
        ),
        (
            {"venue": "otc"},
            "scalp_entry_quote_tick_venue_conflict",
        ),
        ({"instrument": "EURUSD"}, "scalp_entry_quote_instrument_identity_invalid"),
    ],
)
def test_identity_failures_are_closed(
    tick_update: dict[str, object],
    expected_reason: str,
) -> None:
    tick = _tick()
    tick.update(tick_update)

    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert result.refreshed_candidate is None
    assert expected_reason in result.reasons


@pytest.mark.parametrize(
    ("bid", "ask", "expected_reason"),
    [
        (0.0, 1.1, "scalp_entry_quote_bid_invalid"),
        (1.1, 0.0, "scalp_entry_quote_ask_invalid"),
        (math.nan, 1.1, "scalp_entry_quote_bid_invalid"),
        (1.2, 1.1, "scalp_entry_quote_bid_ask_geometry_invalid"),
    ],
)
def test_quote_geometry_failures_are_closed(
    bid: float,
    ask: float,
    expected_reason: str,
) -> None:
    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        _tick(bid=bid, ask=ask),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert expected_reason in result.reasons


@pytest.mark.parametrize(
    ("timestamp_field", "timestamp", "expected_source"),
    [
        ("market_event_received_at_epoch", NOW - 0.25, "market_event_received_at_epoch"),
        ("ts_epoch", str(NOW - 0.25), "ts_epoch"),
        (
            "time",
            datetime.fromtimestamp(NOW - 0.25, tz=timezone.utc).isoformat(),
            "time",
        ),
        (
            "ts",
            datetime.fromtimestamp(NOW - 0.25, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "ts",
        ),
    ],
)
def test_positive_fractional_and_timezone_aware_timestamps_are_parseable(
    timestamp_field: str,
    timestamp: object,
    expected_source: str,
) -> None:
    tick = _tick()
    tick.pop("market_event_received_at_epoch")
    tick[timestamp_field] = timestamp

    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.diagnostics.timestamp_source == expected_source
    assert result.diagnostics.quote_timestamp_epoch == pytest.approx(NOW - 0.25)


def test_metadata_market_event_timestamp_precedes_top_level_quote_time() -> None:
    tick = _tick()
    tick.pop("market_event_received_at_epoch")
    tick["metadata"] = {"market_event_received_at_epoch": NOW - 0.25}
    tick["ts_epoch"] = NOW - 50.0

    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.diagnostics.timestamp_source == (
        "metadata.market_event_received_at_epoch"
    )


@pytest.mark.parametrize(
    ("timestamp", "expected_reason"),
    [
        (None, "scalp_entry_quote_tick_timestamp_invalid"),
        (True, "scalp_entry_quote_tick_timestamp_invalid"),
        (0.0, "scalp_entry_quote_tick_timestamp_invalid"),
        ("not-a-time", "scalp_entry_quote_tick_timestamp_invalid"),
        ("2027-01-01T00:00:00", "scalp_entry_quote_tick_timestamp_invalid"),
        (NOW + 0.001, "scalp_entry_quote_tick_from_future"),
        (NOW - MAX_AGE - 0.001, "scalp_entry_quote_tick_stale"),
    ],
)
def test_invalid_future_and_stale_timestamps_are_closed(
    timestamp: object,
    expected_reason: str,
) -> None:
    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        _tick(timestamp=timestamp),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert expected_reason in result.reasons


def test_exact_maximum_tick_age_is_accepted() -> None:
    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        _tick(timestamp=NOW - MAX_AGE),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is True
    assert result.diagnostics.quote_age_secs == pytest.approx(MAX_AGE)


@pytest.mark.parametrize(
    ("as_of", "max_age", "expected_reason"),
    [
        (0.0, MAX_AGE, "scalp_entry_quote_clock_invalid"),
        (math.nan, MAX_AGE, "scalp_entry_quote_clock_invalid"),
        (NOW, -0.1, "scalp_entry_quote_max_tick_age_invalid"),
        (NOW, math.inf, "scalp_entry_quote_max_tick_age_invalid"),
        (NOW, True, "scalp_entry_quote_max_tick_age_invalid"),
    ],
)
def test_invalid_clock_configuration_is_closed(
    as_of: float,
    max_age: float,
    expected_reason: str,
) -> None:
    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        _tick(),
        as_of_epoch=as_of,
        max_tick_age_secs=max_age,
    )

    assert result.accepted is False
    assert expected_reason in result.reasons


def test_current_spread_is_recomputed_and_cost_dead_candidate_is_refused() -> None:
    tick = _tick(bid=1.0989, ask=1.1011)
    tick["spread_bps"] = 0.01

    result = refresh_scalp_entry_quote(
        _qualified_candidate(),
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert result.reasons == ("scalp_entry_quote_cost_dead",)
    assert result.diagnostics.current_spread_bps == pytest.approx(20.0)
    assert result.diagnostics.total_current_cost_bps == pytest.approx(
        20.0 + SCALP_EXECUTION_DEBIT_BPS
    )
    assert result.diagnostics.live_p_star == pytest.approx(26.0 / 15.0)
    assert result.diagnostics.conservative_expected_edge_bps is not None
    assert result.diagnostics.conservative_expected_edge_bps < 0.0


def test_authenticated_lower_bound_must_strictly_exceed_live_p_star() -> None:
    bid = 1.0998625
    ask = 1.1001375
    spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 1e4
    live_p_star = (5.0 + spread_bps + SCALP_EXECUTION_DEBIT_BPS) / 15.0
    candidate = _qualified_candidate(lower_bound=live_p_star)

    result = refresh_scalp_entry_quote(
        candidate,
        _tick(bid=bid, ask=ask),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert result.reasons == ("scalp_entry_quote_cost_dead",)
    assert result.diagnostics.live_p_star == pytest.approx(live_p_star)
    assert result.diagnostics.conservative_expected_edge_bps == pytest.approx(0.0)


def test_refreshed_sell_target_must_remain_positive() -> None:
    candidate = _qualified_candidate(
        side="SELL",
        ref_mid=1.0,
        entry_price=0.5,
        stop_bps=5.0,
        target_bps=6_000.0,
        signal_spread_bps=1.0,
        lower_bound=0.90,
    )

    result = refresh_scalp_entry_quote(
        candidate,
        _tick(bid=0.4, ask=0.4001),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert result.reasons == ("scalp_entry_quote_refreshed_geometry_invalid",)


def test_inconsistent_qualified_wrapper_is_refused_before_quote_use() -> None:
    candidate = _qualified_candidate()
    forged = replace(candidate, conservative_expected_edge_bps=99.0)

    result = refresh_scalp_entry_quote(
        forged,
        _tick(),
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert result.accepted is False
    assert result.reasons == ("scalp_entry_quote_candidate_payoff_mismatch",)
    assert result.diagnostics.tick_symbol == ""


def test_inputs_are_not_mutated_and_outputs_are_frozen() -> None:
    candidate = _qualified_candidate()
    tick = _tick()
    tick_before = deepcopy(tick)
    proposal_before = candidate.proposal.to_dict()

    first = refresh_scalp_entry_quote(
        candidate,
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )
    second = refresh_scalp_entry_quote(
        candidate,
        tick,
        as_of_epoch=NOW,
        max_tick_age_secs=MAX_AGE,
    )

    assert first == second
    assert tick == tick_before
    assert candidate.proposal.to_dict() == proposal_before
    assert first.refreshed_candidate is not None
    with pytest.raises(FrozenInstanceError):
        first.diagnostics.accepted = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        first.refreshed_candidate.refreshed_entry_price = 0.0  # type: ignore[misc]
