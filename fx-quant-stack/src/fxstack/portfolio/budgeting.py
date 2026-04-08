from __future__ import annotations

from dataclasses import asdict, dataclass

from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.concentration import ConcentrationSnapshot
from fxstack.portfolio.correlation import CorrelationSnapshot


@dataclass(slots=True)
class AllocatorBudget:
    allowed: bool = True
    budget_scale: float = 1.0
    target_cap: int = 0
    exposure_unit: str = "lot_units"
    concentration_penalty: float = 0.0
    correlation_penalty: float = 0.0
    session_penalty: float = 0.0
    reason: str = "ok"

    def to_dict(self) -> dict[str, float | int | bool | str]:
        return asdict(self)


def compute_allocator_budget(
    *,
    symbol: str,
    session_bucket: str,
    expected_edge_bps: float,
    uncertainty_score: float,
    book: PortfolioBook,
    concentration: ConcentrationSnapshot,
    correlation: CorrelationSnapshot,
    max_total_positions: int,
    max_pair_positions: int,
) -> AllocatorBudget:
    symbol_key = str(symbol or "").strip().upper()
    pair_count = sum(1 for item in list(book.positions or []) if str(item.symbol).upper() == symbol_key)
    allowed = True
    reasons: list[str] = []
    if int(max_pair_positions) > 0 and int(pair_count) >= int(max_pair_positions):
        allowed = False
        reasons.append("pair_cap")
    if int(max_total_positions) > 0 and int(book.open_position_count) >= int(max_total_positions):
        allowed = False
        reasons.append("portfolio_cap")
    top_symbol_excess = max(0.0, float(concentration.top_symbol_share) - 0.45)
    top_currency_excess = max(0.0, float(concentration.top_currency_share) - 0.40)
    concentration_penalty = min(0.5, top_symbol_excess * 0.55 + top_currency_excess * 0.25)
    max_corr_excess = max(0.0, float(correlation.max_abs_corr) - 0.50)
    avg_corr_excess = max(0.0, float(correlation.avg_abs_corr) - 0.25)
    correlation_penalty = min(0.35, max_corr_excess * 0.35 + avg_corr_excess * 0.20)
    session_peak = float(book.session_counts.get(str(session_bucket).strip().lower(), 0)) / max(1, int(book.open_position_count or 1))
    session_penalty = min(0.25, max(0.0, session_peak - 0.50) * 0.20)
    edge_bonus = min(0.20, max(0.0, float(expected_edge_bps)) / 80.0)
    uncertainty_penalty = min(0.25, max(0.0, float(uncertainty_score)) * 0.35)
    budget_scale = 1.0 - concentration_penalty - correlation_penalty - session_penalty - uncertainty_penalty + edge_bonus
    budget_scale = max(0.35, min(1.0, float(budget_scale)))
    target_cap = max(0, int(max_total_positions) - int(book.open_position_count))
    if not allowed:
        budget_scale = min(float(budget_scale), 0.15)
    return AllocatorBudget(
        allowed=bool(allowed),
        budget_scale=float(budget_scale),
        target_cap=int(target_cap),
        exposure_unit=str(getattr(book, "exposure_unit", "lot_units") or "lot_units"),
        concentration_penalty=float(concentration_penalty),
        correlation_penalty=float(correlation_penalty),
        session_penalty=float(session_penalty),
        reason="ok" if not reasons else ",".join(reasons),
    )
