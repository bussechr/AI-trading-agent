from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

from fxstack.live.policy import normalize_session_bucket, session_bucket_family
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


def _bounded_metric(
    value: Any,
    *,
    field_name: str,
    errors: list[str],
    invalid_fallback: float = 1.0,
) -> float:
    number = _finite_float(value)
    if number is None:
        errors.append(f"nonfinite:{field_name}")
        return float(invalid_fallback)
    if number < 0.0 or number > 1.0:
        errors.append(f"out_of_range:{field_name}")
    return float(max(0.0, min(1.0, number)))


def _nonnegative_int(value: Any, *, field_name: str, errors: list[str]) -> int:
    number = _finite_float(value)
    if number is None or number < 0.0 or not number.is_integer():
        errors.append(f"invalid:{field_name}")
        return 0
    return int(number)


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
    numeric_errors: list[str] = []
    book_metadata = dict(getattr(book, "metadata", {}) or {})
    if book_metadata.get("numeric_inputs_valid") is False:
        numeric_errors.extend(str(item) for item in list(book_metadata.get("numeric_input_errors") or []))
        numeric_errors.append("invalid:book_numeric_inputs")
    if book_metadata.get("exposure_unit_contract_valid") is False:
        numeric_errors.append("invalid:book_exposure_unit_contract")
    if getattr(concentration, "numeric_inputs_valid", True) is False:
        numeric_errors.extend(str(item) for item in list(getattr(concentration, "numeric_input_errors", []) or []))
        numeric_errors.append("invalid:concentration_numeric_inputs")

    def _top_abs_share(weights: dict[str, float]) -> float:
        cleaned: list[float] = []
        invalid = False
        for key, value in sorted(dict(weights or {}).items(), key=lambda item: str(item[0])):
            number = _finite_float(value)
            if number is None:
                numeric_errors.append(f"nonfinite:net_exposure.{key}")
                invalid = True
                continue
            cleaned.append(abs(float(number)))
        if invalid:
            return 1.0
        total = float(sum(cleaned))
        if total <= 0.0:
            return 0.0
        return float(max(cleaned) / total)

    symbol_key = str(symbol or "").strip().upper()
    pending_positions = list(getattr(book, "pending_positions", []) or [])
    pair_count = sum(1 for item in list(book.positions or []) if str(item.symbol).upper() == symbol_key)
    pair_count += sum(1 for item in pending_positions if str(getattr(item, "symbol", "")).upper() == symbol_key)
    open_position_count = _nonnegative_int(book.open_position_count, field_name="open_position_count", errors=numeric_errors)
    pending_entry_count = _nonnegative_int(book.pending_entry_count, field_name="pending_entry_count", errors=numeric_errors)
    effective_position_count = int(open_position_count) + int(pending_entry_count)
    total_position_cap = _nonnegative_int(max_total_positions, field_name="max_total_positions", errors=numeric_errors)
    pair_position_cap = _nonnegative_int(max_pair_positions, field_name="max_pair_positions", errors=numeric_errors)
    allowed = True
    reasons: list[str] = []
    if pair_position_cap > 0 and int(pair_count) >= pair_position_cap:
        allowed = False
        reasons.append("pair_cap")
    if total_position_cap > 0 and int(effective_position_count) >= total_position_cap:
        allowed = False
        reasons.append("portfolio_cap")
    top_symbol_share = _bounded_metric(
        concentration.top_symbol_share,
        field_name="top_symbol_share",
        errors=numeric_errors,
    )
    top_currency_share = _bounded_metric(
        concentration.top_currency_share,
        field_name="top_currency_share",
        errors=numeric_errors,
    )
    symbol_hhi = _bounded_metric(concentration.symbol_hhi, field_name="symbol_hhi", errors=numeric_errors)
    currency_hhi = _bounded_metric(concentration.currency_hhi, field_name="currency_hhi", errors=numeric_errors)
    top_symbol_excess = max(0.0, top_symbol_share - 0.45)
    top_currency_excess = max(0.0, top_currency_share - 0.40)
    concentration_penalty = min(0.5, top_symbol_excess * 0.55 + top_currency_excess * 0.25)
    concentration_stress = min(
        1.0,
        max(
            top_symbol_share,
            top_currency_share,
            symbol_hhi,
            currency_hhi,
        ),
    )
    currency_stress = min(1.0, max(top_currency_share, currency_hhi))
    net_symbol_share = _top_abs_share(dict(getattr(book, "per_symbol_net_exposure", {}) or {}))
    net_currency_share = _top_abs_share(dict(getattr(book, "per_currency_net_exposure", {}) or {}))
    net_symbol_excess = max(0.0, float(net_symbol_share) - 0.35)
    net_currency_excess = max(0.0, float(net_currency_share) - 0.30)
    net_concentration_penalty = min(0.30, net_symbol_excess * 0.45 + net_currency_excess * 0.25)
    max_abs_corr = _bounded_metric(correlation.max_abs_corr, field_name="max_abs_corr", errors=numeric_errors)
    avg_abs_corr = _bounded_metric(correlation.avg_abs_corr, field_name="avg_abs_corr", errors=numeric_errors)
    heuristic_corr_penalty = min(
        0.35,
        max(0.0, max_abs_corr - 0.50) * 0.35 + max(0.0, avg_abs_corr - 0.25) * 0.20,
    )
    realized_corr_penalty = min(
        0.45,
        max(0.0, max_abs_corr - 0.30) * 0.45 + max(0.0, avg_abs_corr - 0.20) * 0.25,
    )
    correlation_sample_count = _nonnegative_int(correlation.sample_count, field_name="correlation_sample_count", errors=numeric_errors)
    correlation_window_bars = _nonnegative_int(correlation.window_bars, field_name="correlation_window_bars", errors=numeric_errors)
    correlation_min_obs = _nonnegative_int(correlation.min_obs, field_name="correlation_min_obs", errors=numeric_errors)
    correlation_confidence = 0.0
    if correlation_sample_count > 0:
        denominator = max(correlation_window_bars or correlation_min_obs or 1, 1)
        correlation_confidence = min(1.0, float(correlation_sample_count) / float(denominator))
    if str(correlation.method or "").strip().lower() == "heuristic" or correlation_sample_count <= 0:
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
    session_counts_raw = dict(book.session_counts or {})
    session_counts: dict[str, int] = {}
    session_family_counts: dict[str, int] = {}
    for raw_session, raw_count in session_counts_raw.items():
        session_key = normalize_session_bucket(raw_session) or str(raw_session or "").strip().lower()
        if not session_key:
            continue
        count = _nonnegative_int(raw_count, field_name=f"session_counts.{session_key}", errors=numeric_errors)
        if count <= 0:
            continue
        session_counts[session_key] = int(session_counts.get(session_key, 0)) + count
        family_key = session_bucket_family(session_key)
        if family_key:
            session_family_counts[family_key] = int(session_family_counts.get(family_key, 0)) + count
    session_total_count = max(1, int(effective_position_count))
    requested_session = normalize_session_bucket(session_bucket)
    requested_family = session_bucket_family(requested_session) if requested_session else ""
    if requested_session and requested_session in session_counts:
        session_numerator = int(session_counts.get(requested_session, 0))
    elif requested_family and requested_family in session_family_counts:
        session_numerator = int(session_family_counts.get(requested_family, 0))
    elif requested_session:
        session_numerator = 0
    else:
        session_numerator = max((int(value) for value in session_counts.values()), default=0)
    session_peak = float(session_numerator) / float(session_total_count)
    session_penalty = min(0.25, max(0.0, session_peak - 0.50) * 0.20)
    session_stress = min(1.0, max(float(session_peak), float(session_penalty)))
    resize_pressure = min(1.0, max(float(concentration_penalty), float(net_concentration_penalty)))
    flip_pressure = min(1.0, max(float(correlation_penalty), float(realized_corr_penalty)))
    rebalance_pressure = min(1.0, max(float(resize_pressure), float(flip_pressure), float(session_stress)))
    edge_value = _finite_float(expected_edge_bps)
    if edge_value is None:
        numeric_errors.append("nonfinite:expected_edge_bps")
        edge_value = 0.0
    uncertainty_value = _bounded_metric(
        uncertainty_score,
        field_name="uncertainty_score",
        errors=numeric_errors,
    )
    edge_bonus = min(0.20, max(0.0, edge_value) / 80.0)
    uncertainty_penalty = min(0.25, uncertainty_value * 0.35)
    budget_scale = 1.0 - concentration_penalty - net_concentration_penalty - correlation_penalty - session_penalty - uncertainty_penalty + edge_bonus
    budget_scale = max(0.25, min(1.0, float(budget_scale)))
    target_cap = max(0, total_position_cap - int(effective_position_count))
    if numeric_errors:
        allowed = False
        reasons.append("invalid_numeric_contract")
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
        correlation_sample_count=int(correlation_sample_count),
        session_penalty=float(session_penalty),
        resize_pressure=float(resize_pressure),
        flip_pressure=float(flip_pressure),
        rebalance_pressure=float(rebalance_pressure),
        concentration_stress=float(concentration_stress),
        currency_stress=float(currency_stress),
        session_stress=float(session_stress),
        reason="ok" if not reasons else ",".join(reasons),
        numeric_inputs_valid=not numeric_errors,
        numeric_input_errors=sorted(set(numeric_errors)),
    )
