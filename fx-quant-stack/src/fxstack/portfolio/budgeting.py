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
    net_concentration_penalty: float = 0.0
    correlation_penalty: float = 0.0
    realized_correlation_penalty: float = 0.0
    correlation_method: str = "heuristic"
    correlation_sample_count: int = 0
    session_penalty: float = 0.0
    resize_pressure: float = 0.0
    flip_pressure: float = 0.0
    rebalance_pressure: float = 0.0
    concentration_stress: float = 0.0
    currency_stress: float = 0.0
    session_stress: float = 0.0
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
    def _top_abs_share(weights: dict[str, float]) -> float:
        total = float(sum(abs(float(value)) for value in weights.values()))
        if total <= 0.0:
            return 0.0
        return float(max(abs(float(value)) for value in weights.values()) / total)

    def _pending_positions() -> list[object]:
        return list(getattr(book, "pending_positions", []) or [])

    symbol_key = str(symbol or "").strip().upper()
    pending_positions = _pending_positions()
    pair_count = sum(1 for item in list(book.positions or []) if str(item.symbol).upper() == symbol_key)
    pair_count += sum(1 for item in pending_positions if str(getattr(item, "symbol", "")).upper() == symbol_key)
    effective_position_count = int(book.open_position_count) + int(book.pending_entry_count)
    allowed = True
    reasons: list[str] = []
    if int(max_pair_positions) > 0 and int(pair_count) >= int(max_pair_positions):
        allowed = False
        reasons.append("pair_cap")
    if int(max_total_positions) > 0 and int(effective_position_count) >= int(max_total_positions):
        allowed = False
        reasons.append("portfolio_cap")
    top_symbol_excess = max(0.0, float(concentration.top_symbol_share) - 0.45)
    top_currency_excess = max(0.0, float(concentration.top_currency_share) - 0.40)
    concentration_penalty = min(0.5, top_symbol_excess * 0.55 + top_currency_excess * 0.25)
    concentration_stress = min(
        1.0,
        max(
            float(concentration.top_symbol_share),
            float(concentration.top_currency_share),
            float(concentration.symbol_hhi),
            float(concentration.currency_hhi),
        ),
    )
    currency_stress = min(1.0, max(float(concentration.top_currency_share), float(concentration.currency_hhi)))
    net_symbol_share = _top_abs_share(dict(getattr(book, "per_symbol_net_exposure", {}) or {}))
    net_currency_share = _top_abs_share(dict(getattr(book, "per_currency_net_exposure", {}) or {}))
    net_symbol_excess = max(0.0, float(net_symbol_share) - 0.35)
    net_currency_excess = max(0.0, float(net_currency_share) - 0.30)
    net_concentration_penalty = min(0.30, net_symbol_excess * 0.45 + net_currency_excess * 0.25)
    heuristic_corr_penalty = min(
        0.35,
        max(0.0, float(correlation.max_abs_corr) - 0.50) * 0.35 + max(0.0, float(correlation.avg_abs_corr) - 0.25) * 0.20,
    )
    realized_corr_penalty = min(
        0.45,
        max(0.0, float(correlation.max_abs_corr) - 0.30) * 0.45 + max(0.0, float(correlation.avg_abs_corr) - 0.20) * 0.25,
    )
    correlation_confidence = 0.0
    if int(correlation.sample_count) > 0:
        denominator = max(int(correlation.window_bars) or int(correlation.min_obs) or 1, 1)
        correlation_confidence = min(1.0, float(correlation.sample_count) / float(denominator))
    if str(correlation.method or "").strip().lower() == "heuristic" or int(correlation.sample_count) <= 0:
        correlation_penalty = heuristic_corr_penalty
    else:
        correlation_penalty = (
            (1.0 - correlation_confidence) * heuristic_corr_penalty
            + correlation_confidence * realized_corr_penalty
        )
        if str(correlation.method or "").strip().lower() == "hybrid":
            correlation_penalty = max(
                correlation_penalty,
                heuristic_corr_penalty * 0.60 + realized_corr_penalty * 0.40,
            )
    correlation_penalty = min(0.45, float(correlation_penalty))
    session_counts = dict(book.session_counts or {})
    for item in pending_positions:
        session_key = str(getattr(item, "session_bucket", "") or "").strip().lower()
        if session_key:
            session_counts[session_key] = int(session_counts.get(session_key, 0)) + 1
    session_total_count = max(1, int(effective_position_count))
    requested_session = str(session_bucket or "").strip().lower()
    if requested_session and requested_session in session_counts:
        session_numerator = int(session_counts.get(requested_session, 0))
    else:
        session_numerator = max((int(value) for value in session_counts.values()), default=0)
    session_peak = float(session_numerator) / float(session_total_count)
    session_penalty = min(0.25, max(0.0, session_peak - 0.50) * 0.20)
    session_stress = min(1.0, max(float(session_peak), float(session_penalty)))
    resize_pressure = min(1.0, max(float(concentration_penalty), float(net_concentration_penalty)))
    flip_pressure = min(1.0, max(float(correlation_penalty), float(realized_corr_penalty)))
    rebalance_pressure = min(1.0, max(float(resize_pressure), float(flip_pressure), float(session_stress)))
    edge_bonus = min(0.20, max(0.0, float(expected_edge_bps)) / 80.0)
    uncertainty_penalty = min(0.25, max(0.0, float(uncertainty_score)) * 0.35)
    budget_scale = 1.0 - concentration_penalty - net_concentration_penalty - correlation_penalty - session_penalty - uncertainty_penalty + edge_bonus
    budget_scale = max(0.25, min(1.0, float(budget_scale)))
    target_cap = max(0, int(max_total_positions) - int(effective_position_count))
    if not allowed:
        budget_scale = min(float(budget_scale), 0.15)
    return AllocatorBudget(
        allowed=bool(allowed),
        budget_scale=float(budget_scale),
        target_cap=int(target_cap),
        exposure_unit=str(getattr(book, "exposure_unit", "lot_units") or "lot_units"),
        concentration_penalty=float(concentration_penalty),
        net_concentration_penalty=float(net_concentration_penalty),
        correlation_penalty=float(correlation_penalty),
        realized_correlation_penalty=float(realized_corr_penalty),
        correlation_method=str(correlation.method or "heuristic"),
        correlation_sample_count=int(correlation.sample_count or 0),
        session_penalty=float(session_penalty),
        resize_pressure=float(resize_pressure),
        flip_pressure=float(flip_pressure),
        rebalance_pressure=float(rebalance_pressure),
        concentration_stress=float(concentration_stress),
        currency_stress=float(currency_stress),
        session_stress=float(session_stress),
        reason="ok" if not reasons else ",".join(reasons),
    )
