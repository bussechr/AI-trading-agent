from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.concentration import ConcentrationSnapshot


@dataclass(slots=True)
class StressResult:
    worst_case_loss_proxy: float = 0.0
    scenario_losses: dict[str, float] = field(default_factory=dict)
    dominant_scenario: str = ""
    numeric_inputs_valid: bool = True
    numeric_input_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return float(number) if math.isfinite(number) else None


def evaluate_book_stress(book: PortfolioBook, concentration: ConcentrationSnapshot | None = None) -> StressResult:
    errors = [str(item) for item in list(dict(getattr(book, "metadata", {}) or {}).get("numeric_input_errors") or [])]
    gross = _finite_float(book.gross_exposure)
    if gross is None:
        errors.append("nonfinite:gross_exposure")
        gross = 0.0
    elif gross < 0.0:
        errors.append("negative:gross_exposure")
        gross = abs(gross)
    concentration_value = _finite_float(concentration.top_symbol_share if concentration is not None else 0.0)
    if concentration_value is None:
        errors.append("nonfinite:top_symbol_share")
        concentration_value = 1.0
    if concentration is not None and getattr(concentration, "numeric_inputs_valid", True) is False:
        errors.extend(str(item) for item in list(getattr(concentration, "numeric_input_errors", []) or []))
        errors.append("invalid:concentration_numeric_inputs")
    concentration_value = max(0.0, min(1.0, concentration_value))
    # These used to be five invented percentages of gross exposure (5%, 8%,
    # 6%+c*4%, 4%, 6%+(1-c)*3%). None was derived from anything: not from the
    # stops the orders actually carry, not from realized gap history, not from
    # measured spread widening. A fabricated risk number is worse than no risk
    # number, because it looks like a measurement and gets budgeted against.
    #
    # Replaced with the ONE tail loss that is exactly knowable: every open
    # position hits its own stop. With risk-based sizing (risk/sizing.py) each
    # position risks a stated fraction of equity, so simultaneous stop-out is
    # arithmetic rather than assumption. Scenarios that cannot be computed from
    # the book are no longer reported at all.
    stop_loss_total = 0.0
    per_symbol_stops = dict(getattr(book, "per_symbol_stop_risk", {}) or {})
    for value in per_symbol_stops.values():
        number = _finite_float(value)
        if number is None:
            errors.append("nonfinite:per_symbol_stop_risk")
            continue
        stop_loss_total += abs(number)
    if stop_loss_total <= 0.0:
        # No per-position stop risk published: fall back to the book's own stated
        # capital-at-risk, and if that is absent report ZERO rather than invent a
        # percentage. Zero is honest -- it says "not measured".
        fallback = _finite_float(getattr(book, "capital_at_risk", 0.0))
        stop_loss_total = abs(fallback) if fallback is not None else 0.0
    scenarios = {"all_stops_hit": float(stop_loss_total)}
    dominant = "all_stops_hit" if stop_loss_total > 0.0 else ""
    worst_case = float(stop_loss_total)
    return StressResult(
        worst_case_loss_proxy=float(worst_case),
        scenario_losses={str(k): float(v) for k, v in sorted(scenarios.items())},
        dominant_scenario=str(dominant),
        numeric_inputs_valid=not errors,
        numeric_input_errors=sorted(set(errors)),
    )
