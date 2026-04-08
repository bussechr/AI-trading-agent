from __future__ import annotations

from dataclasses import asdict, dataclass, field

from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.concentration import ConcentrationSnapshot


@dataclass(slots=True)
class StressResult:
    worst_case_loss_proxy: float = 0.0
    scenario_losses: dict[str, float] = field(default_factory=dict)
    dominant_scenario: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def evaluate_book_stress(book: PortfolioBook, concentration: ConcentrationSnapshot | None = None) -> StressResult:
    gross = float(book.gross_exposure)
    concentration_value = float(concentration.top_symbol_share if concentration is not None else 0.0)
    concentration_value = max(0.0, min(1.0, concentration_value))
    scenarios = {
        "spread_widening": float(gross * 0.05),
        "gap_open": float(gross * 0.08),
        "correlation_break": float(gross * (0.06 + concentration_value * 0.04)),
        "session_liquidity_shock": float(gross * 0.04),
        "stagnation_no_edge": float(gross * (0.06 + (1.0 - concentration_value) * 0.03)),
    }
    dominant = max(scenarios.items(), key=lambda item: float(item[1]))[0] if scenarios else ""
    worst_case = max((float(value) for value in scenarios.values()), default=0.0)
    return StressResult(
        worst_case_loss_proxy=float(worst_case),
        scenario_losses={str(k): float(v) for k, v in sorted(scenarios.items())},
        dominant_scenario=str(dominant),
    )
