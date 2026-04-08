from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fxstack.portfolio.book import PortfolioBook, build_portfolio_book
from fxstack.portfolio.budgeting import AllocatorBudget, compute_allocator_budget
from fxstack.portfolio.concentration import ConcentrationSnapshot, compute_concentration_snapshot
from fxstack.portfolio.correlation import CorrelationSnapshot, compute_correlation_snapshot
from fxstack.portfolio.stress import StressResult, evaluate_book_stress
from fxstack.portfolio.telemetry import build_portfolio_telemetry


@dataclass(slots=True)
class PortfolioAllocationDecision:
    symbol: str
    allowed: bool
    book: PortfolioBook
    concentration: ConcentrationSnapshot
    correlation: CorrelationSnapshot
    budget: AllocatorBudget
    stress: StressResult
    telemetry: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": str(self.symbol),
            "allowed": bool(self.allowed),
            "book": self.book.to_dict(),
            "concentration": self.concentration.to_dict(),
            "correlation": self.correlation.to_dict(),
            "budget": self.budget.to_dict(),
            "stress": self.stress.to_dict(),
            "telemetry": dict(self.telemetry or {}),
        }


def evaluate_portfolio_allocation(
    *,
    symbol: str,
    session_bucket: str,
    expected_edge_bps: float,
    uncertainty_score: float,
    positions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]] | None,
    max_total_positions: int,
    max_pair_positions: int,
    governance: dict[str, Any] | None = None,
    corr_mode: str = "heuristic",
    realized_returns_by_pair: Any = None,
    corr_window_bars: int = 0,
    corr_min_obs: int = 0,
) -> PortfolioAllocationDecision:
    book = build_portfolio_book(positions=list(positions or []), pending_entries=list(pending_entries or []))
    concentration = compute_concentration_snapshot(book)
    active_symbols = [str(item.symbol).upper() for item in list(book.positions or [])]
    correlation = compute_correlation_snapshot(
        symbol=str(symbol).upper(),
        active_symbols=active_symbols,
        mode=str(corr_mode or "heuristic"),
        realized_returns_by_pair=realized_returns_by_pair,
        window_bars=int(corr_window_bars or 0),
        min_obs=int(corr_min_obs or 0),
    )
    budget = compute_allocator_budget(
        symbol=str(symbol).upper(),
        session_bucket=str(session_bucket),
        expected_edge_bps=float(expected_edge_bps),
        uncertainty_score=float(uncertainty_score),
        book=book,
        concentration=concentration,
        correlation=correlation,
        max_total_positions=int(max_total_positions),
        max_pair_positions=int(max_pair_positions),
    )
    stress = evaluate_book_stress(book, concentration=concentration)
    telemetry = build_portfolio_telemetry(
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=stress,
        governance=governance,
    )
    return PortfolioAllocationDecision(
        symbol=str(symbol).upper(),
        allowed=bool(budget.allowed),
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=stress,
        telemetry=telemetry,
    )
