from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any

from fxstack.portfolio.book import PortfolioBook
from fxstack.portfolio.budgeting import AllocatorBudget
from fxstack.portfolio.concentration import ConcentrationSnapshot
from fxstack.portfolio.correlation import CorrelationSnapshot
from fxstack.portfolio.stress import StressResult


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return float(number) if math.isfinite(number) else float(default)


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def build_portfolio_telemetry(
    *,
    book: PortfolioBook,
    concentration: ConcentrationSnapshot,
    correlation: CorrelationSnapshot,
    budget: AllocatorBudget,
    stress: StressResult,
    governance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    concentration_payload = concentration.to_dict()
    correlation_payload = correlation.to_dict()
    budget_payload = budget.to_dict()
    stress_payload = stress.to_dict()
    governance_payload = _json_safe(dict(governance or {}))
    book_metadata = dict(getattr(book, "metadata", {}) or {})
    portfolio_numeric_inputs_valid = bool(
        book_metadata.get("numeric_inputs_valid", True)
        and concentration_payload.get("numeric_inputs_valid", True)
        and budget_payload.get("numeric_inputs_valid", True)
        and stress_payload.get("numeric_inputs_valid", True)
    )
    payload = {
        "open_position_count": int(book.open_position_count),
        "pending_entry_count": int(book.pending_entry_count),
        "gross_exposure": float(book.gross_exposure),
        "net_exposure": float(book.net_exposure),
        "pending_gross_exposure": float(getattr(book, "pending_gross_exposure", 0.0)),
        "pending_net_exposure": float(getattr(book, "pending_net_exposure", 0.0)),
        "gross_lot_exposure": float(getattr(book, "gross_lot_exposure", 0.0)),
        "net_lot_exposure": float(getattr(book, "net_lot_exposure", 0.0)),
        "pending_gross_lot_exposure": float(getattr(book, "pending_gross_lot_exposure", 0.0)),
        "pending_net_lot_exposure": float(getattr(book, "pending_net_lot_exposure", 0.0)),
        "exposure_unit": str(getattr(book, "exposure_unit", "lot_units") or "lot_units"),
        "per_symbol_exposure": dict(book.per_symbol_exposure),
        "per_symbol_net_exposure": dict(getattr(book, "per_symbol_net_exposure", {}) or {}),
        "per_currency_exposure": dict(book.per_currency_exposure),
        "per_currency_net_exposure": dict(getattr(book, "per_currency_net_exposure", {}) or {}),
        "per_asset_class_exposure": dict(book.per_asset_class_exposure),
        "per_asset_class_net_exposure": dict(getattr(book, "per_asset_class_net_exposure", {}) or {}),
        "session_counts": dict(book.session_counts),
        "sleeve_counts": dict(book.sleeve_counts),
        "top_symbol": str(concentration_payload.get("top_symbol") or ""),
        "top_symbol_share": _finite_float(concentration_payload.get("top_symbol_share", 0.0)),
        "top_currency": str(concentration_payload.get("top_currency") or ""),
        "top_currency_share": _finite_float(concentration_payload.get("top_currency_share", 0.0)),
        "symbol_hhi": _finite_float(concentration_payload.get("symbol_hhi", 0.0)),
        "currency_hhi": _finite_float(concentration_payload.get("currency_hhi", 0.0)),
        "session_peak_share": _finite_float(concentration_payload.get("session_peak_share", 0.0)),
        "sleeve_peak_share": _finite_float(concentration_payload.get("sleeve_peak_share", 0.0)),
        "correlation_method": str(correlation_payload.get("method") or "heuristic"),
        "correlation_window_bars": int(correlation_payload.get("window_bars", 0) or 0),
        "correlation_min_obs": int(correlation_payload.get("min_obs", 0) or 0),
        "correlation_sample_count": int(correlation_payload.get("sample_count", 0) or 0),
        "correlation_freshness_secs": (
            None
            if correlation_payload.get("freshness_secs") is None
            else _finite_float(correlation_payload.get("freshness_secs"))
        ),
        "correlation_max_abs": _finite_float(correlation_payload.get("max_abs_corr", 0.0)),
        "correlation_avg_abs": _finite_float(correlation_payload.get("avg_abs_corr", 0.0)),
        "budget_scale": _finite_float(budget_payload.get("budget_scale", 1.0), 1.0),
        "concentration_penalty": _finite_float(budget_payload.get("concentration_penalty", 0.0)),
        "net_concentration_penalty": _finite_float(budget_payload.get("net_concentration_penalty", 0.0)),
        "correlation_penalty": _finite_float(budget_payload.get("correlation_penalty", 0.0)),
        "realized_correlation_penalty": _finite_float(budget_payload.get("realized_correlation_penalty", 0.0)),
        "session_penalty": _finite_float(budget_payload.get("session_penalty", 0.0)),
        "resize_pressure": _finite_float(budget_payload.get("resize_pressure", 0.0)),
        "flip_pressure": _finite_float(budget_payload.get("flip_pressure", 0.0)),
        "rebalance_pressure": _finite_float(budget_payload.get("rebalance_pressure", 0.0)),
        "concentration_stress": _finite_float(budget_payload.get("concentration_stress", 0.0)),
        "currency_stress": _finite_float(budget_payload.get("currency_stress", 0.0)),
        "session_stress": _finite_float(budget_payload.get("session_stress", 0.0)),
        "governance_mode": str(governance_payload.get("mode") or ""),
        "governance_paused": bool(governance_payload.get("paused", False)),
        "governance_entries_only": bool(governance_payload.get("entries_only", False)),
        "governance_shadow_only": bool(governance_payload.get("shadow_only", False)),
        "governance_budget_scale": _finite_float(governance_payload.get("budget_scale", 1.0), 1.0),
        "numeric_inputs_valid": bool(portfolio_numeric_inputs_valid),
        "book_numeric_inputs_valid": bool(book_metadata.get("numeric_inputs_valid", True)),
        "book_numeric_input_errors": list(book_metadata.get("numeric_input_errors") or []),
        "exposure_unit_contract_valid": bool(book_metadata.get("exposure_unit_contract_valid", True)),
        "concentration": concentration_payload,
        "correlation": correlation_payload,
        "budget": budget_payload,
        "stress": stress_payload,
        "governance": governance_payload,
    }
    return _json_safe(payload)
