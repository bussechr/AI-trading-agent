from __future__ import annotations

from dataclasses import asdict, dataclass

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

    def to_dict(self) -> dict[str, float | str]:
        return asdict(self)


def _hhi(weights: dict[str, float]) -> float:
    total = float(sum(abs(float(value)) for value in weights.values()))
    if total <= 0.0:
        return 0.0
    return float(sum((abs(float(value)) / total) ** 2 for value in weights.values()))


def compute_concentration_snapshot(book: PortfolioBook) -> ConcentrationSnapshot:
    symbol_total = float(sum(abs(float(value)) for value in dict(book.per_symbol_exposure).values()))
    currency_total = float(sum(abs(float(value)) for value in dict(book.per_currency_exposure).values()))
    top_symbol = ""
    top_symbol_value = 0.0
    if book.per_symbol_exposure:
        top_symbol, top_symbol_value = max(book.per_symbol_exposure.items(), key=lambda item: float(item[1]))
    top_currency = ""
    top_currency_value = 0.0
    if book.per_currency_exposure:
        top_currency, top_currency_value = max(book.per_currency_exposure.items(), key=lambda item: float(item[1]))
    session_peak_share = 0.0
    if book.open_position_count > 0 and book.session_counts:
        session_peak_share = max(float(value) for value in book.session_counts.values()) / float(book.open_position_count)
    sleeve_peak_share = 0.0
    if book.open_position_count > 0 and book.sleeve_counts:
        sleeve_peak_share = max(float(value) for value in book.sleeve_counts.values()) / float(book.open_position_count)
    return ConcentrationSnapshot(
        top_symbol=str(top_symbol),
        top_symbol_share=float(top_symbol_value / symbol_total) if symbol_total > 0.0 else 0.0,
        top_currency=str(top_currency),
        top_currency_share=float(top_currency_value / currency_total) if currency_total > 0.0 else 0.0,
        symbol_hhi=_hhi(book.per_symbol_exposure),
        currency_hhi=_hhi(book.per_currency_exposure),
        session_peak_share=float(session_peak_share),
        sleeve_peak_share=float(sleeve_peak_share),
    )
