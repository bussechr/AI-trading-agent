from __future__ import annotations

import pytest
import pandas as pd

from fxstack.portfolio import (
    build_portfolio_book,
    build_portfolio_telemetry,
    compute_concentration_snapshot,
    compute_correlation_snapshot,
    evaluate_book_stress,
    evaluate_portfolio_allocation,
)
from fxstack.portfolio.budgeting import compute_allocator_budget
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
    assert book.session_counts == {"london": 2, "new_york": 1}
    assert book.sleeve_counts == {"breakout": 1, "trend": 2}


def test_build_portfolio_book_counts_pending_entry_exposure() -> None:
    pending_entries = [
        {
            "index": 0,
            "pair": "AUDUSD",
            "payload": {
                "cmd": "BUY",
                "symbol": "AUDUSD",
                "lots": 0.25,
                "mark_price": 0.70,
                "contract_size": 100000,
                "session_bucket": "asia",
                "sleeve": "trend",
            },
        }
    ]

    book = build_portfolio_book(positions=_positions(), pending_entries=pending_entries)

    assert book.pending_entry_count == 1
    assert len(book.pending_positions) == 1
    assert book.pending_positions[0].symbol == "AUDUSD"
    assert book.pending_positions[0].side == "BUY"
    assert book.gross_exposure == pytest.approx(314000.0)
    assert book.net_exposure == pytest.approx(189000.0)
    assert book.pending_gross_exposure == pytest.approx(17500.0)
    assert book.pending_net_exposure == pytest.approx(17500.0)
    assert book.gross_lot_exposure == pytest.approx(3.75)
    assert book.net_lot_exposure == pytest.approx(2.75)
    assert book.pending_gross_lot_exposure == pytest.approx(0.25)
    assert book.pending_net_lot_exposure == pytest.approx(0.25)
    assert book.per_symbol_exposure["AUDUSD"] == pytest.approx(17500.0)
    assert book.per_currency_exposure["AUD"] == pytest.approx(17500.0)
    assert book.per_currency_exposure["USD"] == pytest.approx(190000.0)
    assert book.per_currency_net_exposure["USD"] == pytest.approx(-65000.0)


def test_build_portfolio_book_tracks_signed_net_exposure_buckets() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "GBPUSD",
                "side": "SELL",
                "lots": 0.5,
                "mark_price": 2.0,
                "contract_size": 100000,
                "session_bucket": "newyork",
            },
        ]
    )

    assert book.gross_exposure == pytest.approx(200000.0)
    assert book.net_exposure == pytest.approx(0.0)
    assert book.per_symbol_net_exposure == {"EURUSD": 100000.0, "GBPUSD": -100000.0}
    assert book.per_currency_net_exposure == {"EUR": 100000.0, "GBP": -100000.0, "USD": 0.0}
    assert book.per_asset_class_net_exposure == {"fx": 0.0}


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


def test_pending_entries_contribute_to_session_concentration() -> None:
    book = build_portfolio_book(
        positions=[
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
        ],
        pending_entries=[
            {
                "pair": "AUDUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "AUDUSD",
                    "lots": 0.25,
                    "mark_price": 0.70,
                    "contract_size": 100000,
                    "session_bucket": "london",
                    "sleeve": "trend",
                },
            }
        ],
    )

    concentration = compute_concentration_snapshot(book)

    assert book.session_counts == {"london": 3}
    assert book.sleeve_counts == {"trend": 3}
    assert concentration.session_peak_share == pytest.approx(1.0)
    assert concentration.sleeve_peak_share == pytest.approx(1.0)


def test_allocator_budget_does_not_double_count_pending_session_entries() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.10,
                "contract_size": 100000,
                "session_bucket": "london",
                "sleeve": "trend",
            }
        ],
        pending_entries=[
            {
                "pair": "AUDUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "AUDUSD",
                    "lots": 0.25,
                    "mark_price": 0.70,
                    "contract_size": 100000,
                    "session_bucket": "london",
                    "sleeve": "trend",
                },
            }
        ],
    )
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(symbol="EURUSD", active_symbols=["AUDUSD"], mode="heuristic")

    budget = compute_allocator_budget(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        book=book,
        concentration=concentration,
        correlation=correlation,
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert book.session_counts == {"london": 2}
    assert concentration.session_peak_share == pytest.approx(1.0)
    assert budget.session_penalty == pytest.approx(0.1)
    assert budget.session_stress == pytest.approx(1.0)
    assert budget.budget_scale > 0.35


def test_correlation_snapshot_uses_deterministic_heuristics() -> None:
    snapshot = compute_correlation_snapshot(symbol="EURUSD", active_symbols=["GBPUSD", "AUDNZD", "BTCUSDT"])

    assert snapshot.max_abs_corr == pytest.approx(0.6)
    assert snapshot.avg_abs_corr == pytest.approx((0.6 + 0.15 + 0.1) / 3.0)
    assert snapshot.correlated_symbols == {"AUDNZD": 0.15, "BTCUSDT": 0.1, "GBPUSD": 0.6}


def test_correlation_snapshot_uses_realized_returns_and_metadata() -> None:
    realized = {
        "EURUSD": pd.Series(
            [1.0, 2.0, 3.0, 4.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
        "GBPUSD": pd.Series(
            [2.0, 4.0, 6.0, 8.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
        "AUDNZD": pd.Series(
            [-1.0, -2.0, -3.0, -4.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD", "AUDNZD"],
        mode="realized",
        realized_returns_by_pair=realized,
        window_bars=4,
        min_obs=3,
    )

    assert snapshot.method == "realized"
    assert snapshot.window_bars == 4
    assert snapshot.min_obs == 3
    assert snapshot.sample_count == 4
    assert snapshot.freshness_secs is not None
    assert snapshot.max_abs_corr == pytest.approx(1.0)
    assert snapshot.avg_abs_corr == pytest.approx(1.0)
    assert snapshot.correlated_symbols == {"AUDNZD": -1.0, "GBPUSD": 1.0}


def test_correlation_snapshot_hybrid_blends_realized_and_heuristic_scores() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0]),
        "GBPUSD": pd.Series([2.0, 4.0]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        mode="hybrid",
        realized_returns_by_pair=realized,
        window_bars=8,
        min_obs=2,
    )

    assert snapshot.method == "hybrid"
    assert snapshot.sample_count == 2
    assert snapshot.max_abs_corr == pytest.approx(0.7)
    assert snapshot.avg_abs_corr == pytest.approx(0.7)
    assert snapshot.correlated_symbols["GBPUSD"] == pytest.approx(0.7)


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
    assert stress.scenario_losses["stagnation_no_edge"] == pytest.approx(
        book.gross_exposure * (0.06 + (1.0 - concentration.top_symbol_share) * 0.03)
    )
    assert stress.worst_case_loss_proxy == pytest.approx(stress.scenario_losses["gap_open"])
    assert stress.dominant_scenario == "gap_open"


def test_stress_result_promotes_stagnation_no_edge_for_diversified_book() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "GBPUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "AUDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
            {
                "symbol": "NZDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
        ]
    )
    concentration = compute_concentration_snapshot(book)
    stress = evaluate_book_stress(book, concentration=concentration)

    assert concentration.top_symbol_share == pytest.approx(0.25)
    assert stress.scenario_losses["stagnation_no_edge"] > stress.scenario_losses["gap_open"]
    assert stress.dominant_scenario == "stagnation_no_edge"


def test_build_portfolio_telemetry_flattens_reporting_aliases() -> None:
    book = build_portfolio_book(positions=_positions())
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(symbol="EURUSD", active_symbols=["GBPUSD"], mode="heuristic")
    budget = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    ).budget
    telemetry = build_portfolio_telemetry(
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=evaluate_book_stress(book, concentration=concentration),
        governance={"mode": "normal", "budget_scale": 0.9},
    )

    assert telemetry["top_symbol"] == concentration.top_symbol
    assert telemetry["top_symbol_share"] == pytest.approx(concentration.top_symbol_share)
    assert telemetry["session_peak_share"] == pytest.approx(concentration.session_peak_share)
    assert telemetry["correlation_method"] == "heuristic"
    assert telemetry["correlation_sample_count"] == 0
    assert telemetry["resize_pressure"] >= 0.0
    assert telemetry["flip_pressure"] >= 0.0
    assert telemetry["rebalance_pressure"] >= telemetry["resize_pressure"]
    assert telemetry["currency_stress"] >= telemetry["top_currency_share"]
    assert telemetry["session_stress"] >= telemetry["session_penalty"]
    assert telemetry["budget_scale"] == pytest.approx(budget.budget_scale)
    assert telemetry["governance_mode"] == "normal"
    assert telemetry["governance_budget_scale"] == pytest.approx(0.9)
    assert telemetry["session_penalty"] > 0.0


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
    assert decision.telemetry["budget"]["target_cap"] == 2
    assert decision.telemetry["budget"]["exposure_unit"] == "notional_units"
    assert decision.telemetry["stress"]["dominant_scenario"] == "gap_open"


def test_portfolio_allocator_consumes_realized_correlation_mode() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }

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
        corr_mode="realized",
        realized_returns_by_pair=realized,
        corr_window_bars=4,
        corr_min_obs=2,
    )

    assert decision.correlation.method == "realized"
    assert decision.correlation.sample_count == 4
    assert decision.budget.correlation_method == "realized"
    assert decision.budget.resize_pressure >= 0.0
    assert decision.budget.flip_pressure >= decision.budget.correlation_penalty
    assert decision.budget.rebalance_pressure >= decision.budget.resize_pressure
    assert decision.telemetry["correlation"]["method"] == "realized"
    assert decision.telemetry["correlation"]["sample_count"] == 4


def test_portfolio_allocator_includes_pending_symbols_in_realized_correlation() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }

    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.1,
                "contract_size": 100000,
                "session_bucket": "london",
            }
        ],
        pending_entries=[
            {
                "pair": "GBPUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "GBPUSD",
                    "lots": 0.25,
                    "mark_price": 1.25,
                    "contract_size": 100000,
                    "session_bucket": "london",
                },
            }
        ],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
        corr_mode="realized",
        realized_returns_by_pair=realized,
        corr_window_bars=4,
        corr_min_obs=2,
    )

    assert decision.correlation.method == "realized"
    assert "GBPUSD" in decision.correlation.correlated_symbols
    assert decision.correlation.correlated_symbols["GBPUSD"] == pytest.approx(1.0)
    assert decision.telemetry["pending_gross_exposure"] > 0.0
    assert decision.telemetry["pending_net_exposure"] > 0.0


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


def test_portfolio_allocator_blocks_cross_pair_hard_gate() -> None:
    config = AllocatorConfig(
        max_total_positions=4,
        max_pair_positions=2,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve = playbook_to_sleeve("trend_pullback")
    sleeve_health = SleeveHealthSnapshot(sleeve=sleeve, score=0.56, state="healthy")
    strong = build_allocator_candidate(
        candidate_id="strong",
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
        cross_pair_rank_position=1,
        cross_pair_influence_score=0.92,
        cross_pair_recommendation_strength=0.95,
        config=config,
        open_positions=[],
        sleeve_health=sleeve_health,
    )
    blocked = build_allocator_candidate(
        candidate_id="blocked",
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
        cross_pair_rank_position=2,
        cross_pair_influence_score=0.18,
        cross_pair_recommendation_strength=0.22,
        cross_pair_hard_block=True,
        config=config,
        open_positions=[],
        sleeve_health=sleeve_health,
    )

    ranked, summary = allocate_candidates(
        candidates=[strong, blocked],
        open_positions=[],
        remaining_slots=1,
        config=config,
        tempo_gap_active=False,
    )

    assert summary.selected_count == 1
    assert ranked[0].candidate_id == "strong"
    assert ranked[0].allocator_selected is True
    assert ranked[1].allocator_selected is False
    assert ranked[1].allocator_rejection_reason == "cross_pair_hard_gate"


def test_build_allocator_candidate_uses_realized_correlation_pressure_when_available() -> None:
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
    realized_returns = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }
    heuristic = build_allocator_candidate(
        candidate_id="heuristic",
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
    realized = build_allocator_candidate(
        candidate_id="realized",
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
        corr_mode="realized",
        realized_returns_by_pair=realized_returns,
        corr_window_bars=4,
        corr_min_obs=2,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )

    assert heuristic.portfolio_correlation_pressure == pytest.approx(0.75)
    assert realized.portfolio_correlation_pressure == pytest.approx(1.0)
    assert realized.portfolio_correlation_pressure > heuristic.portfolio_correlation_pressure
