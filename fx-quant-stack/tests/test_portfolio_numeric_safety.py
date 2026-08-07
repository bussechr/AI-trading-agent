from __future__ import annotations

import json
import math

import pytest

from fxstack.portfolio import (
    build_portfolio_book,
    compute_concentration_snapshot,
    compute_correlation_snapshot,
    evaluate_book_stress,
    evaluate_portfolio_allocation,
)
from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.budgeting import compute_allocator_budget


def test_malformed_position_math_is_finite_diagnostic_and_fail_closed() -> None:
    decision = evaluate_portfolio_allocation(
        symbol="USDJPY",
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
            },
            {
                "symbol": "GBPUSD",
                "side": "SELL",
                "lots": float("nan"),
                "notional": float("inf"),
                "mark_price": 1.25,
                "contract_size": 100000,
            },
        ],
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert decision.allowed is False
    assert decision.book.gross_exposure == pytest.approx(110000.0)
    assert decision.book.metadata["numeric_inputs_valid"] is False
    assert decision.book.metadata["invalid_position_count"] == 1
    assert decision.concentration.numeric_inputs_valid is False
    assert decision.budget.numeric_inputs_valid is False
    assert "invalid_numeric_contract" in decision.budget.reason
    assert decision.stress.numeric_inputs_valid is False
    assert decision.telemetry["numeric_inputs_valid"] is False
    assert all(
        math.isfinite(value)
        for value in (
            decision.book.gross_exposure,
            decision.book.net_exposure,
            decision.concentration.top_symbol_share,
            decision.concentration.symbol_hhi,
            decision.budget.budget_scale,
            decision.stress.worst_case_loss_proxy,
        )
    )
    json.dumps(decision.to_dict(), allow_nan=False)


def test_negative_lots_preserve_exposure_magnitude_but_block_new_budget() -> None:
    decision = evaluate_portfolio_allocation(
        symbol="USDJPY",
        session_bucket="asia",
        expected_edge_bps=8.0,
        uncertainty_score=0.2,
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": -0.5,
                "mark_price": 1.0,
                "contract_size": 100000,
            }
        ],
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert decision.book.positions[0].lots == pytest.approx(0.5)
    assert decision.book.gross_exposure == pytest.approx(50000.0)
    assert decision.book.gross_lot_exposure == pytest.approx(0.5)
    assert decision.allowed is False
    assert any("negative:lots" in item for item in decision.budget.numeric_input_errors)


def test_missing_price_is_labeled_base_units_and_mixed_units_are_rejected() -> None:
    base_only = build_portfolio_book(
        positions=[{"symbol": "EURUSD", "side": "BUY", "lots": 0.5, "contract_size": 100000}]
    )
    assert base_only.exposure_unit == "base_units"
    assert base_only.gross_exposure == pytest.approx(50000.0)
    assert base_only.gross_lot_exposure == pytest.approx(0.5)

    decision = evaluate_portfolio_allocation(
        symbol="USDJPY",
        session_bucket="asia",
        expected_edge_bps=8.0,
        uncertainty_score=0.2,
        positions=[
            {"symbol": "EURUSD", "side": "BUY", "lots": 0.5, "contract_size": 100000},
            {
                "symbol": "GBPUSD",
                "side": "SELL",
                "lots": 0.5,
                "mark_price": 1.25,
                "contract_size": 100000,
            },
        ],
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert decision.book.exposure_unit == "mixed_units"
    assert decision.book.metadata["exposure_unit_contract_valid"] is False
    assert decision.allowed is False
    assert "invalid:book_exposure_unit_contract" in decision.budget.numeric_input_errors


def test_manually_malformed_book_and_inputs_stay_finite_and_preserve_zero_governance_budget() -> None:
    book = PortfolioBook(
        gross_exposure=float("inf"),
        net_exposure=float("nan"),
        open_position_count=1,
        per_symbol_exposure={"EURUSD": float("nan")},
        per_currency_exposure={"USD": float("inf")},
    )
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(symbol="USDJPY", active_symbols=["EURUSD"], mode="heuristic")
    budget = compute_allocator_budget(
        symbol="USDJPY",
        session_bucket="asia",
        expected_edge_bps=float("nan"),
        uncertainty_score=float("inf"),
        book=book,
        concentration=concentration,
        correlation=correlation,
        max_total_positions=6,
        max_pair_positions=2,
    )
    stress = evaluate_book_stress(book, concentration=concentration)

    assert concentration.numeric_inputs_valid is False
    assert budget.allowed is False
    assert budget.numeric_inputs_valid is False
    assert budget.budget_scale == pytest.approx(0.15)
    assert stress.numeric_inputs_valid is False
    assert stress.worst_case_loss_proxy == pytest.approx(0.0)

    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=8.0,
        uncertainty_score=0.2,
        positions=[],
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "paused", "budget_scale": 0.0},
    )
    assert decision.telemetry["governance_budget_scale"] == pytest.approx(0.0)
    json.dumps(decision.to_dict(), allow_nan=False)
