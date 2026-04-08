from __future__ import annotations

from typing import Any

from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.budgeting import AllocatorBudget
from fxstack.portfolio.concentration import ConcentrationSnapshot
from fxstack.portfolio.correlation import CorrelationSnapshot
from fxstack.portfolio.stress import StressResult


def build_portfolio_telemetry(
    *,
    book: PortfolioBook,
    concentration: ConcentrationSnapshot,
    correlation: CorrelationSnapshot,
    budget: AllocatorBudget,
    stress: StressResult,
    governance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "open_position_count": int(book.open_position_count),
        "pending_entry_count": int(book.pending_entry_count),
        "gross_exposure": float(book.gross_exposure),
        "net_exposure": float(book.net_exposure),
        "gross_lot_exposure": float(getattr(book, "gross_lot_exposure", 0.0)),
        "net_lot_exposure": float(getattr(book, "net_lot_exposure", 0.0)),
        "exposure_unit": str(getattr(book, "exposure_unit", "lot_units") or "lot_units"),
        "per_symbol_exposure": dict(book.per_symbol_exposure),
        "per_currency_exposure": dict(book.per_currency_exposure),
        "per_asset_class_exposure": dict(book.per_asset_class_exposure),
        "session_counts": dict(book.session_counts),
        "sleeve_counts": dict(book.sleeve_counts),
        "concentration": concentration.to_dict(),
        "correlation": correlation.to_dict(),
        "budget": budget.to_dict(),
        "stress": stress.to_dict(),
        "governance": dict(governance or {}),
    }
