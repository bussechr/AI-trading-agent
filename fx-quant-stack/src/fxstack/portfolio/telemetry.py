from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from fxstack._serialization import (
    copy_flat_mapping,
    flat_dataclass_dict,
    json_safe,
    json_safe_dataclass,
    json_safe_mapping,
)
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


def _snapshot_payload(value: Any) -> dict[str, Any]:
    """Serialize a slots dataclass once without ``asdict`` deep copies."""

    return json_safe_dataclass(value)


def _trusted_snapshot_payload(value: Any) -> dict[str, Any]:
    """Serialize allocator-owned snapshots already normalized by producers."""

    return flat_dataclass_dict(value)


def _cached_payload(
    cache: dict[str, dict[str, Any]] | None,
    name: str,
    value: Any,
    serializer: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """Copy a normalized template whose containers are one primitive level deep."""

    cached = None if cache is None else cache.get(name)
    if isinstance(cached, dict):
        return copy_flat_mapping(cached)
    payload = serializer(value)
    if cache is not None:
        cache[name] = copy_flat_mapping(payload)
    return payload


def _book_telemetry_payload(book: PortfolioBook) -> dict[str, Any]:
    book_metadata = dict(getattr(book, "metadata", {}) or {})
    return {
        "open_position_count": int(book.open_position_count),
        "pending_entry_count": int(book.pending_entry_count),
        "gross_exposure": float(book.gross_exposure),
        "net_exposure": float(book.net_exposure),
        "pending_gross_exposure": float(getattr(book, "pending_gross_exposure", 0.0)),
        "pending_net_exposure": float(getattr(book, "pending_net_exposure", 0.0)),
        "gross_lot_exposure": float(getattr(book, "gross_lot_exposure", 0.0)),
        "net_lot_exposure": float(getattr(book, "net_lot_exposure", 0.0)),
        "pending_gross_lot_exposure": float(
            getattr(book, "pending_gross_lot_exposure", 0.0)
        ),
        "pending_net_lot_exposure": float(
            getattr(book, "pending_net_lot_exposure", 0.0)
        ),
        "exposure_unit": str(
            getattr(book, "exposure_unit", "lot_units") or "lot_units"
        ),
        "per_symbol_exposure": json_safe(dict(book.per_symbol_exposure)),
        "per_symbol_net_exposure": json_safe(
            dict(getattr(book, "per_symbol_net_exposure", {}) or {})
        ),
        "per_currency_exposure": json_safe(dict(book.per_currency_exposure)),
        "per_currency_net_exposure": json_safe(
            dict(getattr(book, "per_currency_net_exposure", {}) or {})
        ),
        "per_asset_class_exposure": json_safe(dict(book.per_asset_class_exposure)),
        "per_asset_class_net_exposure": json_safe(
            dict(getattr(book, "per_asset_class_net_exposure", {}) or {})
        ),
        "session_counts": json_safe(dict(book.session_counts)),
        "sleeve_counts": json_safe(dict(book.sleeve_counts)),
        "book_numeric_inputs_valid": bool(
            book_metadata.get("numeric_inputs_valid", True)
        ),
        "book_numeric_input_errors": json_safe(
            list(book_metadata.get("numeric_input_errors") or [])
        ),
        "exposure_unit_contract_valid": bool(
            book_metadata.get("exposure_unit_contract_valid", True)
        ),
    }


def build_portfolio_telemetry(
    *,
    book: PortfolioBook,
    concentration: ConcentrationSnapshot,
    correlation: CorrelationSnapshot,
    budget: AllocatorBudget,
    stress: StressResult,
    governance: dict[str, Any] | None = None,
    _prepared_payloads: dict[str, dict[str, Any]] | None = None,
    _prepared_correlation_payload: dict[str, Any] | None = None,
    _prepared_budget_payload: dict[str, Any] | None = None,
    _trusted_snapshots: bool = False,
) -> dict[str, Any]:
    trusted = _trusted_snapshots
    snapshot_serializer = _trusted_snapshot_payload if trusted else _snapshot_payload
    book_payload = _cached_payload(
        _prepared_payloads,
        "book",
        book,
        _book_telemetry_payload,
    )
    concentration_payload = _cached_payload(
        _prepared_payloads,
        "concentration",
        concentration,
        snapshot_serializer,
    )
    correlation_payload = (
        copy_flat_mapping(_prepared_correlation_payload)
        if trusted and isinstance(_prepared_correlation_payload, dict)
        else snapshot_serializer(correlation)
    )
    budget_payload = (
        copy_flat_mapping(_prepared_budget_payload)
        if trusted and isinstance(_prepared_budget_payload, dict)
        else snapshot_serializer(budget)
    )
    stress_payload = _cached_payload(
        _prepared_payloads,
        "stress",
        stress,
        snapshot_serializer,
    )
    governance_payload = json_safe_mapping(dict(governance or {}))
    portfolio_numeric_inputs_valid = bool(
        book_payload.get("book_numeric_inputs_valid", True)
        and concentration_payload.get("numeric_inputs_valid", True)
        and budget_payload.get("numeric_inputs_valid", True)
        and stress_payload.get("numeric_inputs_valid", True)
    )
    payload = {
        **book_payload,
        "top_symbol": concentration.top_symbol
        if trusted
        else str(concentration_payload.get("top_symbol") or ""),
        "top_symbol_share": concentration.top_symbol_share
        if trusted
        else _finite_float(concentration_payload.get("top_symbol_share", 0.0)),
        "top_currency": concentration.top_currency
        if trusted
        else str(concentration_payload.get("top_currency") or ""),
        "top_currency_share": concentration.top_currency_share
        if trusted
        else _finite_float(concentration_payload.get("top_currency_share", 0.0)),
        "symbol_hhi": concentration.symbol_hhi
        if trusted
        else _finite_float(concentration_payload.get("symbol_hhi", 0.0)),
        "currency_hhi": concentration.currency_hhi
        if trusted
        else _finite_float(concentration_payload.get("currency_hhi", 0.0)),
        "session_peak_share": concentration.session_peak_share
        if trusted
        else _finite_float(concentration_payload.get("session_peak_share", 0.0)),
        "sleeve_peak_share": concentration.sleeve_peak_share
        if trusted
        else _finite_float(concentration_payload.get("sleeve_peak_share", 0.0)),
        "correlation_method": correlation.method
        if trusted
        else str(correlation_payload.get("method") or "heuristic"),
        "correlation_window_bars": correlation.window_bars
        if trusted
        else int(correlation_payload.get("window_bars", 0) or 0),
        "correlation_min_obs": correlation.min_obs
        if trusted
        else int(correlation_payload.get("min_obs", 0) or 0),
        "correlation_sample_count": correlation.sample_count
        if trusted
        else int(correlation_payload.get("sample_count", 0) or 0),
        "correlation_freshness_secs": (
            correlation.freshness_secs
            if trusted
            else (
                None
                if correlation_payload.get("freshness_secs") is None
                else _finite_float(correlation_payload.get("freshness_secs"))
            )
        ),
        "correlation_max_abs": correlation.max_abs_corr
        if trusted
        else _finite_float(correlation_payload.get("max_abs_corr", 0.0)),
        "correlation_avg_abs": correlation.avg_abs_corr
        if trusted
        else _finite_float(correlation_payload.get("avg_abs_corr", 0.0)),
        "budget_scale": budget.budget_scale
        if trusted
        else _finite_float(budget_payload.get("budget_scale", 1.0), 1.0),
        "concentration_penalty": budget.concentration_penalty
        if trusted
        else _finite_float(budget_payload.get("concentration_penalty", 0.0)),
        "net_concentration_penalty": budget.net_concentration_penalty
        if trusted
        else _finite_float(budget_payload.get("net_concentration_penalty", 0.0)),
        "correlation_penalty": budget.correlation_penalty
        if trusted
        else _finite_float(budget_payload.get("correlation_penalty", 0.0)),
        "realized_correlation_penalty": budget.realized_correlation_penalty
        if trusted
        else _finite_float(budget_payload.get("realized_correlation_penalty", 0.0)),
        "session_penalty": budget.session_penalty
        if trusted
        else _finite_float(budget_payload.get("session_penalty", 0.0)),
        "resize_pressure": budget.resize_pressure
        if trusted
        else _finite_float(budget_payload.get("resize_pressure", 0.0)),
        "flip_pressure": budget.flip_pressure
        if trusted
        else _finite_float(budget_payload.get("flip_pressure", 0.0)),
        "rebalance_pressure": budget.rebalance_pressure
        if trusted
        else _finite_float(budget_payload.get("rebalance_pressure", 0.0)),
        "concentration_stress": budget.concentration_stress
        if trusted
        else _finite_float(budget_payload.get("concentration_stress", 0.0)),
        "currency_stress": budget.currency_stress
        if trusted
        else _finite_float(budget_payload.get("currency_stress", 0.0)),
        "session_stress": budget.session_stress
        if trusted
        else _finite_float(budget_payload.get("session_stress", 0.0)),
        "governance_mode": str(governance_payload.get("mode") or ""),
        "governance_paused": bool(governance_payload.get("paused", False)),
        "governance_entries_only": bool(governance_payload.get("entries_only", False)),
        "governance_shadow_only": bool(governance_payload.get("shadow_only", False)),
        "governance_budget_scale": _finite_float(
            governance_payload.get("budget_scale", 1.0), 1.0
        ),
        "numeric_inputs_valid": bool(portfolio_numeric_inputs_valid),
        "concentration": concentration_payload,
        "correlation": correlation_payload,
        "budget": budget_payload,
        "stress": stress_payload,
        "governance": governance_payload,
    }
    return payload
