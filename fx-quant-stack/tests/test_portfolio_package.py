from __future__ import annotations

import pytest

from fxstack.portfolio import (
    build_portfolio_book,
    compute_concentration_snapshot,
    compute_correlation_snapshot,
    evaluate_book_stress,
    evaluate_portfolio_allocation,
)
from fxstack.strategy.allocator import allocate_candidates, build_allocator_candidate, playbook_to_sleeve
from fxstack.strategy.allocator_types import AllocatorConfig, AllocatorOpenPosition, SleeveHealthSnapshot


def _positions() -> list[dict[str, object]]:
    return [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "lots": 1.0,
            "mark_price": 1.10,
            "contract_size": 100000,
            "session_bucket": "london",
            "sleeve": "trend",
        },
        {
            "symbol": "GBPUSD",
            "side": "SELL",
            "lots": 0.5,
            "mark_price": 1.25,
            "contract_size": 100000,
            "session_bucket": "london",
            "sleeve": "trend",
        },
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "lots": 2.0,
            "mark_price": 62000.0,
            "contract_size": 1.0,
            "session_bucket": "newyork",
            "sleeve": "breakout",
        },
    ]


def test_build_portfolio_book_tracks_exposure_units_and_buckets() -> None:
    book = build_portfolio_book(positions=_positions(), pending_entries=[{"pair": "AUDUSD"}])

    assert book.exposure_unit == "notional_units"
    assert book.gross_exposure == pytest.approx(296500.0)
    assert book.net_exposure == pytest.approx(171500.0)
    assert book.gross_lot_exposure == pytest.approx(3.5)
    assert book.net_lot_exposure == pytest.approx(2.5)
    assert book.open_position_count == 3
    assert book.pending_entry_count == 1
    assert book.per_symbol_exposure["BTCUSDT"] == pytest.approx(124000.0)
    assert book.per_symbol_exposure["EURUSD"] == pytest.approx(110000.0)
    assert book.per_symbol_exposure["GBPUSD"] == pytest.approx(62500.0)
    assert book.per_currency_exposure["EUR"] == pytest.approx(110000.0)
    assert book.per_currency_exposure["GBP"] == pytest.approx(62500.0)
    assert book.per_currency_exposure["USD"] == pytest.approx(172500.0)
    assert book.per_currency_exposure["USDT"] == pytest.approx(124000.0)
    assert book.per_asset_class_exposure == {"crypto": 124000.0, "fx": 172500.0}
    assert book.session_counts == {"london": 2, "newyork": 1}
    assert book.sleeve_counts == {"breakout": 1, "trend": 2}


def test_concentration_snapshot_is_deterministic() -> None:
    book = build_portfolio_book(positions=_positions())

    first = compute_concentration_snapshot(book)
    second = compute_concentration_snapshot(book)

    assert first.to_dict() == second.to_dict()
    assert first.top_symbol == "BTCUSDT"
    assert first.top_symbol_share == pytest.approx(124000.0 / 296500.0)
    assert first.top_currency == "USD"
    assert first.top_currency_share == pytest.approx(172500.0 / (110000.0 + 62500.0 + 172500.0 + 124000.0))
    assert first.session_peak_share == pytest.approx(2 / 3)
    assert first.sleeve_peak_share == pytest.approx(2 / 3)


def test_correlation_snapshot_uses_deterministic_heuristics() -> None:
    snapshot = compute_correlation_snapshot(symbol="EURUSD", active_symbols=["GBPUSD", "AUDNZD", "BTCUSDT"])

    assert snapshot.max_abs_corr == pytest.approx(0.6)
    assert snapshot.avg_abs_corr == pytest.approx((0.6 + 0.15 + 0.1) / 3.0)
    assert snapshot.correlated_symbols == {"AUDNZD": 0.15, "BTCUSDT": 0.1, "GBPUSD": 0.6}


def test_stress_result_is_reproducible() -> None:
    book = build_portfolio_book(positions=_positions())
    concentration = compute_concentration_snapshot(book)
    stress = evaluate_book_stress(book, concentration=concentration)

    assert stress.scenario_losses["spread_widening"] == pytest.approx(book.gross_exposure * 0.05)
    assert stress.scenario_losses["gap_open"] == pytest.approx(book.gross_exposure * 0.08)
    assert stress.scenario_losses["correlation_break"] == pytest.approx(
        book.gross_exposure * (0.06 + concentration.top_symbol_share * 0.04)
    )
    assert stress.scenario_losses["session_liquidity_shock"] == pytest.approx(book.gross_exposure * 0.04)
    assert stress.worst_case_loss_proxy == pytest.approx(stress.scenario_losses["gap_open"])
    assert stress.dominant_scenario == "gap_open"


def test_portfolio_allocator_returns_budget_and_telemetry() -> None:
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[{"pair": "AUDUSD"}],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    )

    assert decision.allowed is True
    assert decision.budget.allowed is True
    assert decision.book.exposure_unit == "notional_units"
    assert decision.book.gross_exposure == pytest.approx(296500.0)
    assert decision.budget.budget_scale > 0.35
    assert decision.telemetry["concentration"]["top_symbol"] == "BTCUSDT"
    assert decision.telemetry["budget"]["target_cap"] == 3
    assert decision.telemetry["budget"]["exposure_unit"] == "notional_units"
    assert decision.telemetry["stress"]["dominant_scenario"] == "gap_open"


def test_portfolio_allocator_ranks_against_crowded_session_and_correlation_pressure() -> None:
    config = AllocatorConfig(
        max_total_positions=6,
        max_pair_positions=2,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve = playbook_to_sleeve("trend_pullback")
    sleeve_health = SleeveHealthSnapshot(sleeve=sleeve, score=0.56, state="healthy")
    open_positions = [
        AllocatorOpenPosition(
            position_id="open-1",
            pair="EURUSD",
            side="BUY",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.54,
            age_bars=4.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
        AllocatorOpenPosition(
            position_id="open-2",
            pair="GBPUSD",
            side="SELL",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.57,
            age_bars=5.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
    ]
    crowded = build_allocator_candidate(
        candidate_id="crowded",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=sleeve,
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )
    cleaner = build_allocator_candidate(
        candidate_id="cleaner",
        index=1,
        pair="USDJPY",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=playbook_to_sleeve("breakout_expansion"),
        environment_state="PersistentTrend",
        session_bucket="asia",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )

    ranked, summary = allocate_candidates(
        candidates=[crowded, cleaner],
        open_positions=open_positions,
        remaining_slots=1,
        config=config,
        tempo_gap_active=False,
    )

    assert summary.selected_count == 1
    assert ranked[0].pair == "USDJPY"
    assert ranked[0].allocator_selected is True
    assert ranked[1].allocator_selected is False
    assert ranked[0].allocator_score > ranked[1].allocator_score
    assert ranked[0].portfolio_risk_pressure < ranked[1].portfolio_risk_pressure
    assert ranked[1].portfolio_session_pressure > ranked[0].portfolio_session_pressure
    assert ranked[1].portfolio_correlation_pressure > ranked[0].portfolio_correlation_pressure
