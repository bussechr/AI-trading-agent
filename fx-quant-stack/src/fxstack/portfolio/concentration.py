from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from fxstack.portfolio.book import PortfolioBook


@dataclass(slots=True)
class ConcentrationSnapshot:
    top_symbol: str = ""
    top_symbol_share: float = 0.0
    top_currency: str = ""
    top_currency_share: float = 0.0
    symbol_hhi: float = 0.0
    currency_hhi: float = 0.0
    session_peak_share: float = 0.0
    sleeve_peak_share: float = 0.0
    numeric_inputs_valid: bool = True
    numeric_input_errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return float(number) if math.isfinite(number) else None


def _finite_weights(weights: dict[str, float], *, field_name: str, errors: list[str]) -> dict[str, float]:
    cleaned: dict[str, float] = {}
    for key, value in sorted(dict(weights or {}).items(), key=lambda item: str(item[0])):
        number = _finite_float(value)
        if number is None:
            errors.append(f"nonfinite:{field_name}.{key}")
            number = 0.0
        cleaned[str(key)] = abs(float(number))
    return cleaned


def _finite_counts(counts: dict[str, int], *, field_name: str, errors: list[str]) -> dict[str, int]:
    cleaned: dict[str, int] = {}
    for key, value in sorted(dict(counts or {}).items(), key=lambda item: str(item[0])):
        number = _finite_float(value)
        if number is None or number < 0.0 or not float(number).is_integer():
            errors.append(f"invalid:{field_name}.{key}")
            continue
        cleaned[str(key)] = int(number)
    return cleaned


def _hhi(weights: dict[str, float]) -> float:
    total = float(sum(weights.values()))
    if total <= 0.0:
        return 0.0
    return float(sum((float(value) / total) ** 2 for value in weights.values()))


def compute_concentration_snapshot(book: PortfolioBook) -> ConcentrationSnapshot:
    errors = [str(item) for item in list(dict(getattr(book, "metadata", {}) or {}).get("numeric_input_errors") or [])]
    symbol_weights = _finite_weights(dict(book.per_symbol_exposure), field_name="per_symbol_exposure", errors=errors)
    currency_weights = _finite_weights(dict(book.per_currency_exposure), field_name="per_currency_exposure", errors=errors)
    symbol_total = float(sum(symbol_weights.values()))
    currency_total = float(sum(currency_weights.values()))
    open_count = _finite_float(book.open_position_count)
    pending_count = _finite_float(book.pending_entry_count)
    if open_count is None or open_count < 0.0 or not open_count.is_integer():
        errors.append("invalid:open_position_count")
        open_count = 0.0
    if pending_count is None or pending_count < 0.0 or not pending_count.is_integer():
        errors.append("invalid:pending_entry_count")
        pending_count = 0.0
    total_positions = int(open_count) + int(pending_count)
    top_symbol = ""
    top_symbol_value = 0.0
    if symbol_weights:
        top_symbol, top_symbol_value = min(symbol_weights.items(), key=lambda item: (-float(item[1]), str(item[0])))
    top_currency = ""
    top_currency_value = 0.0
    if currency_weights:
        top_currency, top_currency_value = min(currency_weights.items(), key=lambda item: (-float(item[1]), str(item[0])))
    session_counts = _finite_counts(dict(book.session_counts), field_name="session_counts", errors=errors)
    sleeve_counts = _finite_counts(dict(book.sleeve_counts), field_name="sleeve_counts", errors=errors)
    session_peak_share = 0.0
    if total_positions > 0 and session_counts:
        session_peak_share = max(float(value) for value in session_counts.values()) / float(total_positions)
        if session_peak_share > 1.0:
            errors.append("out_of_range:session_peak_share")
            session_peak_share = 1.0
    sleeve_peak_share = 0.0
    if total_positions > 0 and sleeve_counts:
        sleeve_peak_share = max(float(value) for value in sleeve_counts.values()) / float(total_positions)
        if sleeve_peak_share > 1.0:
            errors.append("out_of_range:sleeve_peak_share")
            sleeve_peak_share = 1.0
    return ConcentrationSnapshot(
        top_symbol=str(top_symbol),
        top_symbol_share=float(top_symbol_value / symbol_total) if symbol_total > 0.0 else 0.0,
        top_currency=str(top_currency),
        top_currency_share=float(top_currency_value / currency_total) if currency_total > 0.0 else 0.0,
        symbol_hhi=_hhi(symbol_weights),
        currency_hhi=_hhi(currency_weights),
        session_peak_share=float(session_peak_share),
        sleeve_peak_share=float(sleeve_peak_share),
        numeric_inputs_valid=not errors,
        numeric_input_errors=sorted(set(errors)),
    )
