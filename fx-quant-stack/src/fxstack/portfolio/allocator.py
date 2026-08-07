from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from fxstack._serialization import (
    clone_flat_dataclass,
    copy_flat_mapping,
    copy_json_payload,
    flat_dataclass_dict,
)
from fxstack.portfolio.book import (
    PortfolioBook,
    PreparedPortfolioBook,
    build_portfolio_book,
    compose_portfolio_book,
)
from fxstack.portfolio.budgeting import AllocatorBudget, compute_allocator_budget
from fxstack.portfolio.concentration import ConcentrationSnapshot, compute_concentration_snapshot
from fxstack.portfolio.correlation import CorrelationSnapshot, compute_correlation_snapshot
from fxstack.portfolio.stress import StressResult, evaluate_book_stress
from fxstack.portfolio.telemetry import build_portfolio_telemetry


def _nonnegative_int(value: Any) -> int:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    if not math.isfinite(number) or number < 0.0 or not number.is_integer():
        return 0
    return int(number)


def _prepared_budget_cache_key(
    *,
    symbol: str,
    session_bucket: str,
    expected_edge_bps: Any,
    uncertainty_score: Any,
    max_total_positions: Any,
    max_pair_positions: Any,
    book: PortfolioBook,
    concentration: ConcentrationSnapshot,
    correlation: CorrelationSnapshot,
    _trusted_inputs: bool = False,
) -> tuple[Any, ...] | None:
    """Key valid scalar budget inputs; malformed contracts always recompute."""

    book_metadata = getattr(book, "metadata", {}) or {}
    if (
        book_metadata.get("numeric_inputs_valid", True) is not True
        or book_metadata.get("exposure_unit_contract_valid", True) is not True
        or concentration.numeric_inputs_valid is not True
        or type(correlation.method) is not str
    ):
        return None
    if _trusted_inputs:
        try:
            finite = (
                math.isfinite(expected_edge_bps)
                and math.isfinite(uncertainty_score)
                and math.isfinite(correlation.max_abs_corr)
                and math.isfinite(correlation.avg_abs_corr)
            )
        except (TypeError, ValueError):
            return None
        if not finite or (
            not 0.0 <= uncertainty_score <= 1.0
            or not 0.0 <= correlation.max_abs_corr <= 1.0
            or not 0.0 <= correlation.avg_abs_corr <= 1.0
            or min(
                max_total_positions,
                max_pair_positions,
                correlation.sample_count,
                correlation.window_bars,
                correlation.min_obs,
            )
            < 0
        ):
            return None
        return (
            symbol,
            session_bucket,
            expected_edge_bps,
            uncertainty_score,
            max_total_positions,
            max_pair_positions,
            correlation.method,
            correlation.max_abs_corr,
            correlation.avg_abs_corr,
            correlation.sample_count,
            correlation.window_bars,
            correlation.min_obs,
        )
    try:
        edge = float(expected_edge_bps)
        uncertainty = float(uncertainty_score)
        total_cap = float(max_total_positions)
        pair_cap = float(max_pair_positions)
        max_corr = float(correlation.max_abs_corr)
        avg_corr = float(correlation.avg_abs_corr)
        sample_count = float(correlation.sample_count)
        window_bars = float(correlation.window_bars)
        min_obs = float(correlation.min_obs)
    except (TypeError, ValueError, OverflowError):
        return None
    scalars = (
        edge,
        uncertainty,
        total_cap,
        pair_cap,
        max_corr,
        avg_corr,
        sample_count,
        window_bars,
        min_obs,
    )
    if not all(math.isfinite(value) for value in scalars):
        return None
    if not 0.0 <= uncertainty <= 1.0 or not 0.0 <= max_corr <= 1.0 or not 0.0 <= avg_corr <= 1.0:
        return None
    integer_values = (total_cap, pair_cap, sample_count, window_bars, min_obs)
    if any(value < 0.0 or not value.is_integer() for value in integer_values):
        return None
    return (
        str(symbol).upper(),
        str(session_bucket),
        edge,
        uncertainty,
        int(total_cap),
        int(pair_cap),
        str(correlation.method),
        max_corr,
        avg_corr,
        int(sample_count),
        int(window_bars),
        int(min_obs),
    )


@dataclass(slots=True)
class PortfolioAllocationDecision:
    symbol: str
    allowed: bool
    book: PortfolioBook
    concentration: ConcentrationSnapshot
    correlation: CorrelationSnapshot
    budget: AllocatorBudget
    stress: StressResult
    telemetry: dict[str, Any] = field(default_factory=dict)
    _book_payload: dict[str, Any] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _trusted_snapshots: bool = field(
        default=False,
        repr=False,
        compare=False,
    )
    _runtime_read_only: bool = field(
        default=False,
        repr=False,
        compare=False,
    )

    def _snapshot(self, value: Any) -> dict[str, Any]:
        """Serialize one trusted snapshot only when its output view needs it."""

        return (
            flat_dataclass_dict(value)
            if self._trusted_snapshots
            else value.to_dict()
        )

    def _book_dict(self) -> dict[str, Any]:
        return (
            copy_json_payload(self._book_payload)
            if self._book_payload is not None
            else self.book.to_dict()
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": str(self.symbol),
            "allowed": bool(self.allowed),
            "book": self._book_dict(),
            "concentration": self._snapshot(self.concentration),
            "correlation": self._snapshot(self.correlation),
            "budget": self._snapshot(self.budget),
            "stress": self._snapshot(self.stress),
            "telemetry": dict(self.telemetry or {}),
        }

    def to_runtime_dict(self) -> dict[str, Any]:
        """Serialize the non-duplicated allocation contract used by risk/runtime."""

        telemetry = dict(self.telemetry or {})
        telemetry_budget = telemetry.pop("budget", None)
        budget = (
            telemetry_budget
            if self._runtime_read_only and isinstance(telemetry_budget, dict)
            else self._snapshot(self.budget)
        )
        return {
            "symbol": str(self.symbol),
            "allowed": bool(self.allowed),
            "budget": budget,
            "telemetry": telemetry,
        }


def _prepared_base_snapshots(
    prepared: PreparedPortfolioBook,
    *,
    reuse_cached: bool = False,
) -> tuple[ConcentrationSnapshot, StressResult, tuple[str, ...]]:
    cached = prepared._derived.get("allocation_base")
    if cached is None:
        book = prepared._book
        concentration = compute_concentration_snapshot(book)
        stress = evaluate_book_stress(book, concentration=concentration)
        active_symbols = tuple(
            sorted(
                {
                    str(item.symbol).upper()
                    for item in book.positions
                    if str(item.symbol)
                }
            )
        )
        cached = (concentration, stress, active_symbols)
        prepared._derived["allocation_base"] = cached
    concentration, stress, active_symbols = cached
    if reuse_cached:
        return concentration, stress, active_symbols
    return (
        clone_flat_dataclass(concentration),
        clone_flat_dataclass(stress),
        active_symbols,
    )


def evaluate_portfolio_allocation(
    *,
    symbol: str,
    session_bucket: str,
    expected_edge_bps: float,
    uncertainty_score: float,
    positions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]] | None,
    max_total_positions: int,
    max_pair_positions: int,
    governance: dict[str, Any] | None = None,
    corr_mode: str = "heuristic",
    realized_returns_by_pair: Any = None,
    corr_window_bars: int = 0,
    corr_min_obs: int = 0,
    prepared_book: PreparedPortfolioBook | None = None,
    _runtime_read_only: bool = False,
) -> PortfolioAllocationDecision:
    pending = list(pending_entries or [])
    symbol_key = str(symbol).upper()
    session_key = str(session_bucket)
    corr_mode_key = str(corr_mode or "heuristic").strip().lower()
    window_bars = _nonnegative_int(corr_window_bars)
    min_obs = _nonnegative_int(corr_min_obs)
    book_payload: dict[str, Any] | None = None
    if prepared_book is not None and not pending:
        book = prepared_book._book
        concentration, stress, active_symbols = _prepared_base_snapshots(
            prepared_book,
            reuse_cached=_runtime_read_only,
        )
        book_payload = prepared_book._payload
        prepared_telemetry_payloads = prepared_book._derived.setdefault(
            "allocation_telemetry_payloads",
            {},
        )
        prepared_budget_cache = prepared_book._derived.setdefault(
            "allocation_budgets",
            {},
        )
        prepared_correlation_cache = prepared_book._derived.setdefault(
            "allocation_correlations",
            {},
        )
    else:
        prepared_telemetry_payloads = None
        prepared_budget_cache = None
        prepared_correlation_cache = None
        book = (
            compose_portfolio_book(prepared_book, pending_entries=pending)
            if prepared_book is not None
            else build_portfolio_book(
                positions=list(positions or []),
                pending_entries=pending,
            )
        )
        concentration = compute_concentration_snapshot(book)
        stress = evaluate_book_stress(book, concentration=concentration)
        active_symbols = tuple(
            sorted(
                {
                    str(item.symbol).upper()
                    for item in (*book.positions, *book.pending_positions)
                    if str(item.symbol)
                }
            )
        )
    correlation_cache_key = (
        (symbol_key, window_bars, min_obs)
        if prepared_correlation_cache is not None and corr_mode_key == "heuristic"
        else None
    )
    cached_correlation = (
        None
        if correlation_cache_key is None
        else prepared_correlation_cache.get(correlation_cache_key)
    )
    prepared_correlation_payload: dict[str, Any] | None = None
    if (
        isinstance(cached_correlation, tuple)
        and len(cached_correlation) == 2
        and isinstance(cached_correlation[0], CorrelationSnapshot)
        and isinstance(cached_correlation[1], dict)
    ):
        correlation_template, prepared_correlation_payload = cached_correlation
        correlation = (
            correlation_template
            if _runtime_read_only
            else clone_flat_dataclass(correlation_template)
        )
    else:
        correlation = compute_correlation_snapshot(
            symbol=symbol_key,
            active_symbols=active_symbols,
            mode=corr_mode_key,
            realized_returns_by_pair=realized_returns_by_pair,
            window_bars=window_bars,
            min_obs=min_obs,
        )
        if correlation_cache_key is not None:
            prepared_correlation_payload = flat_dataclass_dict(correlation)
            prepared_correlation_cache[correlation_cache_key] = (
                clone_flat_dataclass(correlation),
                copy_flat_mapping(prepared_correlation_payload),
            )
    budget_cache_key = (
        None
        if prepared_budget_cache is None
        else _prepared_budget_cache_key(
            symbol=symbol_key,
            session_bucket=session_key,
            expected_edge_bps=expected_edge_bps,
            uncertainty_score=uncertainty_score,
            max_total_positions=max_total_positions,
            max_pair_positions=max_pair_positions,
            book=book,
            concentration=concentration,
            correlation=correlation,
            _trusted_inputs=_runtime_read_only,
        )
    )
    cached_budget = (
        None
        if budget_cache_key is None
        else prepared_budget_cache.get(budget_cache_key)
    )
    prepared_budget_payload: dict[str, Any] | None = None
    if (
        isinstance(cached_budget, tuple)
        and len(cached_budget) == 2
        and isinstance(cached_budget[0], AllocatorBudget)
        and isinstance(cached_budget[1], dict)
    ):
        budget_template, prepared_budget_payload = cached_budget
        budget = (
            budget_template
            if _runtime_read_only
            else clone_flat_dataclass(budget_template)
        )
    else:
        budget = compute_allocator_budget(
            symbol=symbol_key,
            session_bucket=session_key,
            expected_edge_bps=expected_edge_bps,
            uncertainty_score=uncertainty_score,
            book=book,
            concentration=concentration,
            correlation=correlation,
            max_total_positions=max_total_positions,
            max_pair_positions=max_pair_positions,
        )
        if budget_cache_key is not None and budget.numeric_inputs_valid:
            prepared_budget_payload = flat_dataclass_dict(budget)
            prepared_budget_cache[budget_cache_key] = (
                clone_flat_dataclass(budget),
                copy_flat_mapping(prepared_budget_payload),
            )
    telemetry = build_portfolio_telemetry(
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=stress,
        governance=governance,
        _prepared_payloads=prepared_telemetry_payloads,
        _prepared_correlation_payload=prepared_correlation_payload,
        _prepared_budget_payload=prepared_budget_payload,
        _trusted_snapshots=True,
    )
    return PortfolioAllocationDecision(
        symbol=symbol_key,
        allowed=bool(budget.allowed),
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=stress,
        telemetry=telemetry,
        _book_payload=book_payload,
        _trusted_snapshots=True,
        _runtime_read_only=_runtime_read_only,
    )
