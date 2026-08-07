from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime.market_source_identity import (
    MARKET_SOURCE_SCHEMA,
    AuthenticatedMarketSource,
    build_authenticated_market_source,
)
from fxstack.runtime.mtvclc_entry_qualification import (
    qualify_mtvclc_entry_candidate,
)
from fxstack.runtime.mtvclc_entry_quote import refresh_mtvclc_entry_quote
from fxstack.runtime.mtvclc_runtime_release import (
    RUNTIME_RELEASE_AUTHORITY,
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_runtime_admission import (
    verify_configured_scalp_runtime_admission,
)
from fxstack.strategy.mtvclc import (
    MAX_QUOTE_GAP_SECONDS,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    MTVCLC_V1_SYMBOLS,
    STOP_COST_MULTIPLE,
    TARGET_COST_MULTIPLE,
    TIME_STOP_M1_BARS,
    MTVCLCCostCalibration,
    MTVCLCTradeCandidate,
)


SIGNAL_EPOCH = 1_800_000_000
EXPECTED_ENTRY_EPOCH = SIGNAL_EPOCH + 60
ENTRY_DEADLINE_EPOCH = EXPECTED_ENTRY_EPOCH + 5
AS_OF_EPOCH = EXPECTED_ENTRY_EPOCH + 2.0
CALIBRATION_ID = "mtvclc-test-frozen-costs"
COST_SOURCE_SHA256 = "1" * 64


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _source(*, generation: str = "generation-a") -> AuthenticatedMarketSource:
    result = build_authenticated_market_source(
        broker_account_scope="ig-demo:account-1",
        broker_venue_id=IG_MT4_VENUE_ID,
        producer_identity="mt4-command-consumer",
        producer_instance_id="terminal-1",
        terminal_lease_scope="singleton-demo-terminal",
        credential_generation_id=generation,
        bridge_protocol_version="v2",
    )
    assert result is not None
    return result


def _cost(symbol: str) -> MTVCLCCostCalibration:
    instrument = get_ig_mt4_instrument(symbol)
    assert instrument is not None
    return MTVCLCCostCalibration(
        symbol=symbol,
        calibration_id=CALIBRATION_ID,
        source_sha256=COST_SOURCE_SHA256,
        p90_spread_bps=2.0,
        commission_bps_per_round_trip=0.25,
        financing_bps_per_trade=0.10,
        account_currency="USD",
        pnl_currency=instrument.quote_ccy,
        convert_on_close_charge_fraction=(
            0.0 if instrument.quote_ccy == "USD" else 0.005
        ),
    )


def _cost_row(cost: MTVCLCCostCalibration) -> dict[str, object]:
    return {
        "symbol": cost.symbol,
        "calibration_id": cost.calibration_id,
        "source_sha256": cost.source_sha256,
        "p90_spread_bps": cost.p90_spread_bps,
        "commission_bps_per_round_trip": (
            cost.commission_bps_per_round_trip
        ),
        "financing_bps_per_trade": cost.financing_bps_per_trade,
        "account_currency": cost.account_currency,
        "pnl_currency": cost.pnl_currency,
        "convert_on_close_charge_fraction": (
            cost.convert_on_close_charge_fraction
        ),
        "adverse_execution_debit_bps": (
            cost.adverse_execution_debit_bps
        ),
        "evidence_cost_row_sha256": _digest(f"evidence-cost:{cost.symbol}"),
        "runtime_calibration_row_sha256": cost.row_sha256(),
    }


def _verification(
    *,
    lower: float = 0.90,
) -> MTVCLCRuntimeReleaseVerification:
    bounds = {
        symbol: {"BUY": lower, "SELL": lower}
        for symbol in MTVCLC_V1_SYMBOLS
    }
    cell_hashes = {
        symbol: {
            side: _digest(f"cell:{symbol}:{side}")
            for side in ("BUY", "SELL")
        }
        for symbol in MTVCLC_V1_SYMBOLS
    }
    costs = {symbol: _cost(symbol) for symbol in MTVCLC_V1_SYMBOLS}
    calibrations = {symbol: _cost_row(cost) for symbol, cost in costs.items()}
    base_break_even = {
        symbol: {
            side: cost.break_even_win_probability
            for side in ("BUY", "SELL")
        }
        for symbol, cost in costs.items()
    }
    evidence_cost_rows = {
        symbol: str(row["evidence_cost_row_sha256"])
        for symbol, row in calibrations.items()
    }
    return MTVCLCRuntimeReleaseVerification(
        valid=True,
        reason="",
        errors=(),
        authenticated=True,
        revocation_verified=True,
        certificate_sha256="c" * 64,
        evidence_sha256="e" * 64,
        signing_key_id="d" * 64,
        generation_id="signed-release-generation-1",
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=MTVCLC_CONFIG_SHA256,
        venue_id=IG_MT4_VENUE_ID,
        account_mode="demo",
        scope_version=IG_MT4_SCALP_SCOPE_VERSION,
        symbol_scope=MTVCLC_V1_SYMBOLS,
        max_entries_per_symbol_utc_day=1,
        maximum_account_currency_risk_per_trade=1.0,
        issued_at_epoch=EXPECTED_ENTRY_EPOCH - 30.0,
        expires_at_epoch=ENTRY_DEADLINE_EPOCH + 300.0,
        authority_purpose=(
            "mtvclc_ig_demo_runtime_release_eligibility.v1"
        ),
        authority=dict(RUNTIME_RELEASE_AUTHORITY),
        qualification_surface_sha256="f" * 64,
        win_probability_lower_bounds=bounds,
        base_break_even_probabilities=base_break_even,
        evidence_cell_sha256=cell_hashes,
        evidence_cost_row_sha256=evidence_cost_rows,
        cost_mapping_sha256="9" * 64,
        cost_rows_sha256="8" * 64,
        cost_calibration_id=CALIBRATION_ID,
        cost_calibration_source_sha256="8" * 64,
        cost_calibration_source_sha256_by_symbol={
            symbol: cost.source_sha256 for symbol, cost in costs.items()
        },
        cost_calibrations=calibrations,
    )


def _prices(symbol: str) -> tuple[float, float]:
    if symbol.endswith("JPY"):
        return 150.000, 150.010
    return 1.10000, 1.10010


def _candidate(
    *,
    symbol: str = "EURUSD",
    side: str = "BUY",
    cost: MTVCLCCostCalibration | None = None,
    source: AuthenticatedMarketSource | None = None,
) -> MTVCLCTradeCandidate:
    admitted = cost or _cost(symbol)
    market_source = source or _source()
    instrument = get_ig_mt4_instrument(symbol)
    assert instrument is not None
    bid, ask = _prices(symbol)
    entry = ask if side == "BUY" else bid
    target_bps = TARGET_COST_MULTIPLE * admitted.recorded_cost_bps
    stop_bps = STOP_COST_MULTIPLE * admitted.recorded_cost_bps
    stop = entry * (
        1.0 - stop_bps / 1e4 if side == "BUY" else 1.0 + stop_bps / 1e4
    )
    target = entry * (
        1.0 + target_bps / 1e4
        if side == "BUY"
        else 1.0 - target_bps / 1e4
    )
    mid = bid + (ask - bid) / 2.0
    return MTVCLCTradeCandidate(
        symbol=symbol,
        instrument_id=instrument.instrument_id,
        venue_id=IG_MT4_VENUE_ID,
        allowed=True,
        reasons=(),
        bar_source_id=market_source.source_id,
        bar_source_version=MARKET_SOURCE_SCHEMA,
        quote_source_id=market_source.source_id,
        quote_source_version=MARKET_SOURCE_SCHEMA,
        market_source_identity_sha256=market_source.source_id,
        cost_calibration_id=admitted.calibration_id,
        cost_calibration_source_sha256=admitted.source_sha256,
        cost_calibration_row_sha256=admitted.row_sha256(),
        side=side,  # type: ignore[arg-type]
        signal_epoch=SIGNAL_EPOCH,
        expected_entry_epoch=EXPECTED_ENTRY_EPOCH,
        entry_deadline_epoch=ENTRY_DEADLINE_EPOCH,
        entry_epoch=EXPECTED_ENTRY_EPOCH,
        entry_day="2027-01-15",
        entry_bid=bid,
        entry_ask=ask,
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        live_spread_bps=(ask - bid) / mid * 1e4,
        p90_spread_bps=admitted.p90_spread_bps,
        recorded_cost_bps=admitted.recorded_cost_bps,
        conversion_charge_fraction=(
            admitted.convert_on_close_charge_fraction
        ),
        p_star=admitted.break_even_win_probability,
        target_bps=target_bps,
        stop_bps=stop_bps,
        time_stop_bars=TIME_STOP_M1_BARS,
        maximum_quote_gap_seconds=MAX_QUOTE_GAP_SECONDS,
        volume_v90=100.0,
        signal_tick_volume=150,
        bid_body_bps=4.0 if side == "BUY" else -4.0,
        bid_close_location=0.9 if side == "BUY" else 0.1,
    )


def _tick(
    *,
    symbol: str,
    source: AuthenticatedMarketSource,
    received_at_epoch: float = EXPECTED_ENTRY_EPOCH + 1.5,
    bid: float | None = None,
    ask: float | None = None,
) -> dict[str, object]:
    default_bid, default_ask = _prices(symbol)
    return {
        **source.to_fields(),
        "provider": "mt4_bridge",
        "instrument": {
            "canonical_symbol": symbol,
            "venue": IG_MT4_VENUE_ID,
        },
        "bid": default_bid if bid is None else bid,
        "ask": default_ask if ask is None else ask,
        "received_at_epoch": received_at_epoch,
        "transport_fresh": True,
        "source_event_baseline_initialized": True,
        "source_event_token": "42",
        "market_event_sequence": 7,
        "market_event_received_at_epoch": received_at_epoch - 0.1,
        "quality_flags": (),
    }


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_qualification_accepts_only_signed_exact_immediate_trade(side: str) -> None:
    cost = _cost("EURUSD")
    result = qualify_mtvclc_entry_candidate(
        _candidate(side=side, cost=cost),
        _verification(),
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.qualified is True
    qualified = result.qualified_candidate
    assert qualified is not None
    assert qualified.execution_type == "market"
    assert qualified.pending_orders_forbidden is True
    assert qualified.win_probability_lower_bound == pytest.approx(0.90)
    assert qualified.evidence_sha256 == "e" * 64
    assert qualified.evidence_cell_sha256 == _digest(f"cell:EURUSD:{side}")
    assert qualified.evidence_cost_row_sha256 == _digest("evidence-cost:EURUSD")
    with pytest.raises(FrozenInstanceError):
        qualified.win_probability_lower_bound = 0.99  # type: ignore[misc]


@pytest.mark.parametrize("account_mode", ["demo", "real"])
def test_runtime_native_admission_qualifies_the_same_exact_trade(
    account_mode: str,
) -> None:
    repository_root = Path(__file__).resolve().parents[2]
    capture = (
        repository_root
        / "artifacts"
        / "scalp_research"
        / "staging"
        / "ig_tick_cost_snapshot_20260804_post_reload"
        / "ig_mt4_bid_ask_capture.json"
    )
    settings = SimpleNamespace(
        project_root=repository_root,
        live_expected_account_mode=account_mode,
        production_scalp_cost_capture_file=str(capture),
        production_scalp_cost_capture_sha256=(
            "2c8b1239113dc76b9c1bd1f55da2010a44ebb1f6020cf358ac2110afcecd6190"
        ),
    )
    admission = verify_configured_scalp_runtime_admission(
        settings,
        now_epoch=AS_OF_EPOCH,
    )
    cost = admission.cost_calibration_for("USDCHF")
    assert admission.valid is True
    assert cost is not None

    result = qualify_mtvclc_entry_candidate(
        _candidate(symbol="USDCHF", cost=cost),
        admission.verification,
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.reasons == ()
    assert result.qualified is True
    qualified = result.qualified_candidate
    assert qualified is not None
    assert qualified.release_generation_id == "mtvclc-runtime-native-v1"
    assert qualified.admitted_cost == cost
    assert qualified.conservative_expected_edge_bps > 0.0
    assert qualified.reward_risk_ratio == pytest.approx(
        TARGET_COST_MULTIPLE / STOP_COST_MULTIPLE
    )


def test_qualification_refuses_mutually_consistent_but_unsigned_cost_pair() -> None:
    signed_release = _verification()
    unsigned_cost = replace(_cost("EURUSD"), p90_spread_bps=1.9)
    result = qualify_mtvclc_entry_candidate(
        _candidate(cost=unsigned_cost),
        signed_release,
        unsigned_cost,
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.qualified is False
    assert "mtvclc_qualification_release_cost_p90_spread_bps_invalid" in (
        result.reasons
    )
    assert "mtvclc_qualification_release_cost_row_invalid" in result.reasons


def test_qualification_refuses_non_release_authority_and_wrong_entry_side() -> None:
    cost = _cost("EURUSD")
    release = replace(_verification(), authority={})
    proposal = replace(
        _candidate(cost=cost),
        entry_price=_candidate(cost=cost).entry_bid,
    )
    result = qualify_mtvclc_entry_candidate(
        proposal,
        release,
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.qualified is False
    assert "mtvclc_qualification_release_authority_invalid" in result.reasons
    assert "mtvclc_qualification_entry_price_basis_invalid" in result.reasons


def test_qualification_requires_signed_lower_bound_strictly_above_p_star() -> None:
    cost = _cost("EURUSD")
    release = _verification()
    bounds = {
        symbol: dict(sides)
        for symbol, sides in release.win_probability_lower_bounds.items()
    }
    bounds["EURUSD"]["BUY"] = cost.break_even_win_probability
    release = replace(release, win_probability_lower_bounds=bounds)
    result = qualify_mtvclc_entry_candidate(
        _candidate(cost=cost),
        release,
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.qualified is False
    assert result.reasons == (
        "mtvclc_qualification_probability_not_above_p_star",
    )


@pytest.mark.parametrize(
    ("symbol", "side"),
    [("EURUSD", "BUY"), ("EURUSD", "SELL"), ("USDJPY", "BUY")],
)
def test_quote_refresh_reanchors_immediate_side_and_full_current_cost(
    symbol: str,
    side: str,
) -> None:
    source = _source()
    cost = _cost(symbol)
    qualification = qualify_mtvclc_entry_candidate(
        _candidate(symbol=symbol, side=side, cost=cost, source=source),
        _verification(),
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )
    assert qualification.qualified_candidate is not None
    original_bid, original_ask = _prices(symbol)
    price_step = 0.002 if symbol.endswith("JPY") else 0.00002
    bid = original_bid + price_step
    ask = original_ask + price_step

    result = refresh_mtvclc_entry_quote(
        qualification.qualified_candidate,
        _tick(symbol=symbol, source=source, bid=bid, ask=ask),
        as_of_epoch=AS_OF_EPOCH,
    )

    assert result.accepted is True
    refreshed = result.refreshed_candidate
    assert refreshed is not None
    expected_entry = ask if side == "BUY" else bid
    assert refreshed.refreshed_entry_price == expected_entry
    assert refreshed.execution_type == "market"
    assert refreshed.pending_orders_forbidden is True
    assert refreshed.target_distance_price == pytest.approx(
        expected_entry * refreshed.target_bps / 1e4
    )
    assert refreshed.stop_distance_price == pytest.approx(
        expected_entry * refreshed.stop_bps / 1e4
    )
    spread_bps = (ask - bid) / ((ask + bid) / 2.0) * 1e4
    fixed_cost = (
        cost.commission_bps_per_round_trip
        + cost.financing_bps_per_trade
        + cost.adverse_execution_debit_bps
    )
    assert refreshed.current_total_cost_bps == pytest.approx(
        spread_bps + fixed_cost
    )
    conversion = cost.convert_on_close_charge_fraction
    expected_p_star = (
        refreshed.stop_bps * (1.0 + conversion)
        + refreshed.current_total_cost_bps
    ) / (
        refreshed.target_bps * (1.0 - conversion)
        + refreshed.stop_bps * (1.0 + conversion)
    )
    assert refreshed.live_p_star == pytest.approx(expected_p_star)
    assert refreshed.conservative_expected_edge_bps > 0.0


def test_quote_refresh_refuses_changed_source_stale_tick_and_wide_spread() -> None:
    source = _source()
    cost = _cost("EURUSD")
    qualification = qualify_mtvclc_entry_candidate(
        _candidate(cost=cost, source=source),
        _verification(),
        cost,
        as_of_epoch=AS_OF_EPOCH,
    )
    assert qualification.qualified_candidate is not None

    changed_source = refresh_mtvclc_entry_quote(
        qualification.qualified_candidate,
        _tick(symbol="EURUSD", source=_source(generation="generation-b")),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert changed_source.accepted is False
    assert "mtvclc_entry_quote_market_source_changed" in changed_source.reasons

    stale = refresh_mtvclc_entry_quote(
        qualification.qualified_candidate,
        _tick(
            symbol="EURUSD",
            source=source,
            received_at_epoch=EXPECTED_ENTRY_EPOCH - 10.0,
        ),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert stale.accepted is False
    assert "mtvclc_entry_quote_stale" in stale.reasons

    wide = refresh_mtvclc_entry_quote(
        qualification.qualified_candidate,
        _tick(symbol="EURUSD", source=source, bid=1.1000, ask=1.1005),
        as_of_epoch=AS_OF_EPOCH,
    )
    assert wide.accepted is False
    assert "mtvclc_entry_quote_spread_above_frozen_p90" in wide.reasons
