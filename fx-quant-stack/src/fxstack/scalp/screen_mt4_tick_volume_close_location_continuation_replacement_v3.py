"""Ledger-authenticated MTVCLC-v1 screen for the 4,830-cell family.

This module keeps the frozen signal and outcome arithmetic in the original
screen, but treats the reservation and outcome ledgers as the only admissible
source for cell summaries.  A caller-provided cell summary is never trusted:
validation reconstructs all 44 cells, rejects duplicate or inconsistent ledger
rows, and compares the reconstructed cells byte-for-byte with the claim.

The module is pure.  It has no file, network, credential, issuer, activation,
runtime, broker, or trade surface.
"""

from __future__ import annotations

import math

# AGENT: ROLE: pure ledger-authenticated fourth-declaration MTVCLC screen.
# AGENT: HANDSHAKE: frozen source inputs -> complete ledgers -> recomputed 44 cells.
# AGENT: ISOLATION: no I/O, issuer, activation, registry, runtime, or trade surface.
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import fields
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation as _base,
)

SCREEN_RESULT_SCHEMA = "fxstack.scalp.mtvclc_screen_result.v2"
IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_786
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 44
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_830
BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.648050309953223
WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95

_SIDES = ("BUY", "SELL")
_ACCOUNTING = {
    "prior_attempted_cells_lower_bound": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
    "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
    "cumulative_attempted_cells_lower_bound": (
        IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    ),
}
_AUTHORITY_FIELDS = (
    "success_claim_authorized",
    "holdout_access_authorized",
    "promotion_authorized",
    "activation_authorized",
    "registry_write_authorized",
    "runtime_authorized",
    "order_authorized",
)
_RESERVATION_FIELDS = frozenset(
    field.name for field in fields(_base.MTVCLCClosedSignal)
) | {"entry_status"}
_OUTCOME_FIELDS = frozenset(field.name for field in fields(_base.MTVCLCOutcome))
_CELL_FIELDS = frozenset(
    {
        "config_id",
        "symbol",
        "side",
        "source_ready",
        "reservations",
        "wins",
        "independent_days",
        "full_target_rate",
        "win_probability_wilson_lower",
        "base_break_even_probability",
        "mean_net_bps",
        "passes_fixed_cell_screen",
    }
)
_ADMITTED_EXIT_REASONS = frozenset(
    {
        "STOP_LOSS",
        "TAKE_PROFIT",
        "TIME_STOP",
        "QUOTE_GAP_ADVERSE",
        "INCOMPLETE_HORIZON_ADVERSE",
    }
)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _close(left: Any, right: Any, *, tolerance: float = 1e-12) -> bool:
    lhs = _finite(left)
    rhs = _finite(right)
    return bool(
        lhs is not None
        and rhs is not None
        and math.isclose(lhs, rhs, rel_tol=0.0, abs_tol=tolerance)
    )


def attempt_manifest() -> dict[str, Any]:
    manifest = deepcopy(_base.attempt_manifest())
    manifest.update(_ACCOUNTING)
    manifest["descriptive_df99_bonferroni_abs_t_threshold"] = (
        BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
    )
    manifest["win_probability_familywise_attempted_cells"] = (
        IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    )
    manifest["win_probability_alpha_allocation"] = "one_sided_0.05_over_4830"
    manifest["cell_summary_source"] = (
        "exclusive_recomputation_from_complete_reservation_and_outcome_ledgers"
    )
    manifest["empty_missing_duplicate_or_inconsistent_ledgers_refuse"] = True
    return manifest


def _wilson_one_sided_lower(wins: int, trials: int) -> float:
    if trials <= 0 or wins < 0 or wins > trials:
        return 0.0
    alpha = (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE) / (
        IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    )
    z = NormalDist().inv_cdf(1.0 - alpha)
    point = wins / trials
    z_sq = z * z
    denominator = 1.0 + z_sq / trials
    center = point + z_sq / (2.0 * trials)
    radius = z * math.sqrt(
        point * (1.0 - point) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, (center - radius) / denominator)


def _ledger_key(row: Mapping[str, Any]) -> tuple[str, str, str, int, str] | None:
    signal_epoch = _strict_int(row.get("signal_epoch"), minimum=1)
    key = (
        str(row.get("config_id") or ""),
        str(row.get("symbol") or ""),
        str(row.get("side") or ""),
        signal_epoch or 0,
        str(row.get("entry_day") or ""),
    )
    if (
        signal_epoch is None
        or key[0] != _base.CONFIG_ID
        or key[1] not in _base.MTVCLC_SYMBOLS
        or key[2] not in _SIDES
        or signal_epoch % 60 != 0
    ):
        return None
    try:
        expected_day = datetime.fromtimestamp(signal_epoch + 60, tz=UTC).date()
    except (OSError, OverflowError, ValueError):
        return None
    return key if key[4] == expected_day.isoformat() else None


def _validated_costs(value: Any) -> dict[str, _base.MT4CostCalibration] | None:
    if not isinstance(value, Mapping) or set(value) != set(_base.MTVCLC_SYMBOLS):
        return None
    result: dict[str, _base.MT4CostCalibration] = {}
    expected_fields = {field.name for field in fields(_base.MT4CostCalibration)}
    for symbol in _base.MTVCLC_SYMBOLS:
        row = value.get(symbol)
        if not isinstance(row, Mapping) or set(row) != expected_fields:
            return None
        try:
            calibration = _base.MT4CostCalibration(**dict(row))
        except (TypeError, ValueError):
            return None
        if not _base.validate_cost_calibration(calibration, expected_symbol=symbol):
            return None
        result[symbol] = calibration
    return result


def _reservation_valid(
    row: Mapping[str, Any], calibration: _base.MT4CostCalibration
) -> bool:
    if set(row) != _RESERVATION_FIELDS or _ledger_key(row) is None:
        return False
    signal_index = _strict_int(row.get("signal_index"), minimum=_base.BASELINE_M1_BARS)
    expected_entry = _strict_int(row.get("expected_entry_epoch"), minimum=1)
    signal_epoch = _strict_int(row.get("signal_epoch"), minimum=1)
    tick_volume = _strict_int(row.get("signal_tick_volume"), minimum=0)
    entry_status = row.get("entry_status")
    if (
        signal_index is None
        or expected_entry is None
        or signal_epoch is None
        or expected_entry != signal_epoch + 60
        or tick_volume is None
        or entry_status
        not in {"admitted", "contemporaneous_entry_quote_missing"}
    ):
        return False
    positive_fields = (
        "volume_v90",
        "bid_body_bps",
        "bid_close_location",
        "p90_spread_bps",
        "recorded_cost_bps",
        "target_bps",
        "stop_bps",
        "p_star",
    )
    if any((_finite(row.get(name)) or 0.0) <= 0.0 for name in positive_fields):
        return False
    return bool(
        tick_volume > float(row["volume_v90"])
        and float(row["bid_close_location"]) >= _base.CLOSE_LOCATION_THRESHOLD
        and _close(row["p90_spread_bps"], calibration.p90_spread_bps)
        and _close(row["recorded_cost_bps"], calibration.recorded_cost_bps)
        and _close(
            row["convert_on_close_charge_fraction"],
            calibration.convert_on_close_charge_fraction,
        )
        and _close(
            row["target_bps"],
            _base.TARGET_COST_MULTIPLE * calibration.recorded_cost_bps,
        )
        and _close(
            row["stop_bps"],
            _base.STOP_COST_MULTIPLE * calibration.recorded_cost_bps,
        )
        and _close(row["p_star"], calibration.break_even_win_probability)
    )


def _outcome_valid(
    row: Mapping[str, Any],
    reservation: Mapping[str, Any],
    calibration: _base.MT4CostCalibration,
) -> bool:
    if set(row) != _OUTCOME_FIELDS or _ledger_key(row) != _ledger_key(reservation):
        return False
    won = row.get("full_target_hit_first")
    if not isinstance(won, bool):
        return False
    gross = _finite(row.get("gross_quote_bps"))
    conversion = _finite(row.get("currency_conversion_debit_bps"))
    net = _finite(row.get("net_bps"))
    if gross is None or conversion is None or net is None or conversion < 0.0:
        return False
    expected_conversion = abs(gross) * calibration.convert_on_close_charge_fraction
    if not (
        _close(row.get("recorded_cost_bps"), calibration.recorded_cost_bps)
        and _close(conversion, expected_conversion)
        and _close(net, gross - calibration.recorded_cost_bps - conversion)
    ):
        return False
    status = reservation.get("entry_status")
    reason = row.get("exit_reason")
    entry_epoch = row.get("entry_epoch")
    exit_epoch = row.get("exit_epoch")
    entry_price = row.get("entry_price")
    exit_price = row.get("exit_price")
    stop_bps = float(reservation["stop_bps"])
    target_bps = float(reservation["target_bps"])
    if status == "contemporaneous_entry_quote_missing":
        return bool(
            reason == "ENTRY_QUOTE_MISSING"
            and won is False
            and all(value is None for value in (entry_epoch, exit_epoch, entry_price, exit_price))
            and _close(gross, -stop_bps)
        )
    if reason not in _ADMITTED_EXIT_REASONS:
        return False
    admitted_epoch = _strict_int(entry_epoch, minimum=1)
    expected_entry = _strict_int(reservation.get("expected_entry_epoch"), minimum=1)
    admitted_price = _finite(entry_price)
    if (
        admitted_epoch is None
        or expected_entry is None
        or not expected_entry
        <= admitted_epoch
        <= expected_entry + _base.MAX_ENTRY_DELAY_SECONDS
        or admitted_price is None
        or admitted_price <= 0.0
    ):
        return False
    if won is not (reason == "TAKE_PROFIT"):
        return False
    if reason in {"QUOTE_GAP_ADVERSE", "INCOMPLETE_HORIZON_ADVERSE"}:
        if not _close(gross, -stop_bps) or exit_price is not None:
            return False
        if reason == "INCOMPLETE_HORIZON_ADVERSE":
            return exit_epoch is None
        return _strict_int(exit_epoch, minimum=admitted_epoch + 1) is not None

    resolved_exit = _strict_int(exit_epoch, minimum=admitted_epoch + 1)
    resolved_price = _finite(exit_price)
    if resolved_exit is None or resolved_price is None or resolved_price <= 0.0:
        return False
    horizon_epoch = admitted_epoch + _base.OUTCOME_HORIZON_M1_BARS * 60
    if resolved_exit > horizon_epoch + _base.MAX_QUOTE_GAP_SECONDS:
        return False
    price_gross = (
        (resolved_price - admitted_price) / admitted_price * 1e4
        if reservation.get("side") == "BUY"
        else (admitted_price - resolved_price) / admitted_price * 1e4
    )
    if not _close(gross, price_gross, tolerance=1e-9):
        return False
    if reason == "TAKE_PROFIT":
        return _close(gross, target_bps)
    if reason == "STOP_LOSS":
        return gross <= -stop_bps or _close(gross, -stop_bps)
    if reason == "TIME_STOP":
        return (
            resolved_exit >= horizon_epoch
            and gross > -stop_bps
            and gross < target_bps
        )
    return False


def _validated_ledger_pairs(
    *,
    reservations: Any,
    outcomes: Any,
    costs: Mapping[str, _base.MT4CostCalibration],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]] | None:
    if (
        not isinstance(reservations, list)
        or not isinstance(outcomes, list)
        or not reservations
        or len(reservations) != len(outcomes)
    ):
        return None
    reservation_by_key: dict[tuple[str, str, str, int, str], Mapping[str, Any]] = {}
    outcome_by_key: dict[tuple[str, str, str, int, str], Mapping[str, Any]] = {}
    reserved_symbol_days: set[tuple[str, str]] = set()
    for raw in reservations:
        if not isinstance(raw, Mapping):
            return None
        key = _ledger_key(raw)
        if key is None or key in reservation_by_key:
            return None
        symbol_day = (key[1], key[4])
        if symbol_day in reserved_symbol_days or not _reservation_valid(raw, costs[key[1]]):
            return None
        reserved_symbol_days.add(symbol_day)
        reservation_by_key[key] = raw
    for raw in outcomes:
        if not isinstance(raw, Mapping):
            return None
        key = _ledger_key(raw)
        if key is None or key in outcome_by_key or key not in reservation_by_key:
            return None
        if not _outcome_valid(raw, reservation_by_key[key], costs[key[1]]):
            return None
        outcome_by_key[key] = raw
    if set(outcome_by_key) != set(reservation_by_key):
        return None
    ordered_keys = sorted(reservation_by_key)
    return (
        [reservation_by_key[key] for key in ordered_keys],
        [outcome_by_key[key] for key in ordered_keys],
    )


def _cell_payload(
    *,
    symbol: str,
    side: str,
    source_ready: bool,
    outcomes: Sequence[Any],
    break_even_probability: float,
) -> dict[str, Any]:
    selected = [
        outcome
        for outcome in outcomes
        if (
            outcome.get("symbol") == symbol
            and outcome.get("side") == side
            if isinstance(outcome, Mapping)
            else outcome.symbol == symbol and outcome.side == side
        )
    ]
    wins = sum(
        bool(
            outcome.get("full_target_hit_first")
            if isinstance(outcome, Mapping)
            else outcome.full_target_hit_first
        )
        for outcome in selected
    )
    days = len(
        {
            str(outcome.get("entry_day") if isinstance(outcome, Mapping) else outcome.entry_day)
            for outcome in selected
        }
    )
    mean_net = (
        sum(
            float(outcome.get("net_bps") if isinstance(outcome, Mapping) else outcome.net_bps)
            for outcome in selected
        )
        / len(selected)
        if selected
        else 0.0
    )
    lower = _wilson_one_sided_lower(wins, len(selected))
    passes = bool(
        source_ready
        and len(selected) >= _base.MIN_TRADES_PER_CELL
        and days >= _base.MIN_INDEPENDENT_DAYS_PER_CELL
        and lower > break_even_probability
        and mean_net > 0.0
    )
    return {
        "config_id": _base.CONFIG_ID,
        "symbol": symbol,
        "side": side,
        "source_ready": source_ready,
        "reservations": len(selected),
        "wins": wins,
        "independent_days": days,
        "full_target_rate": wins / len(selected) if selected else 0.0,
        "win_probability_wilson_lower": lower,
        "base_break_even_probability": break_even_probability,
        "mean_net_bps": mean_net,
        "passes_fixed_cell_screen": passes,
    }


def recompute_cells_from_ledgers(
    *,
    reservation_ledger: Any,
    outcome_ledger: Any,
    costs: Any,
    source_ready_by_symbol: Any,
) -> list[dict[str, Any]] | None:
    """Return the canonical 44 cells or ``None`` for any ledger defect."""

    calibrations = _validated_costs(costs)
    if calibrations is None:
        return None
    if (
        not isinstance(source_ready_by_symbol, Mapping)
        or list(source_ready_by_symbol) != list(_base.MTVCLC_SYMBOLS)
        or any(value is not True for value in source_ready_by_symbol.values())
    ):
        return None
    ledgers = _validated_ledger_pairs(
        reservations=reservation_ledger,
        outcomes=outcome_ledger,
        costs=calibrations,
    )
    if ledgers is None:
        return None
    _reservations, outcomes = ledgers
    return [
        _cell_payload(
            symbol=symbol,
            side=side,
            source_ready=True,
            outcomes=outcomes,
            break_even_probability=calibrations[symbol].break_even_win_probability,
        )
        for symbol in _base.MTVCLC_SYMBOLS
        for side in _SIDES
    ]


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[Any]],
    quotes_by_symbol: Mapping[str, Sequence[Any]],
    costs_by_symbol: Mapping[str, Any],
    source_sha256_by_symbol: Mapping[str, str],
    source_contract_id: str,
) -> dict[str, Any]:
    result = _base.screen_universe(
        bars_by_symbol=bars_by_symbol,
        quotes_by_symbol=quotes_by_symbol,
        costs_by_symbol=costs_by_symbol,
        source_sha256_by_symbol=source_sha256_by_symbol,
        source_contract_id=source_contract_id,
    )
    result["schema_version"] = SCREEN_RESULT_SCHEMA
    result["attempt_accounting"] = dict(_ACCOUNTING)
    result["attempt_manifest"] = attempt_manifest()
    source_ready = bool(
        result.get("source_scope_ready") is True and result.get("source_errors") == []
    )
    result["source_ready_by_symbol"] = {
        symbol: source_ready for symbol in _base.MTVCLC_SYMBOLS
    }
    cells = recompute_cells_from_ledgers(
        reservation_ledger=result.get("reservation_ledger"),
        outcome_ledger=result.get("outcome_ledger"),
        costs=result.get("costs"),
        source_ready_by_symbol=result["source_ready_by_symbol"],
    )
    if cells is None:
        # A failed source can still be represented, but it can never masquerade
        # as a ledger-authenticated result bundle.
        cells = [
            _cell_payload(
                symbol=symbol,
                side=side,
                source_ready=False,
                outcomes=(),
                break_even_probability=(
                    costs_by_symbol[symbol].break_even_win_probability
                    if symbol in costs_by_symbol
                    and _base.validate_cost_calibration(
                        costs_by_symbol[symbol], expected_symbol=symbol
                    )
                    else _base.BASE_COST_BREAK_EVEN_WIN_PROBABILITY
                ),
            )
            for symbol in _base.MTVCLC_SYMBOLS
            for side in _SIDES
        ]
    result["cells"] = cells
    result["all_cells_pass_fixed_screen"] = all(
        cell["passes_fixed_cell_screen"] for cell in cells
    )
    return result


def validate_result_bundle(result: Mapping[str, Any]) -> bool:
    """Authenticate every summary exclusively against the two ledgers."""

    if (
        result.get("schema_version") != SCREEN_RESULT_SCHEMA
        or result.get("strategy_id") != _base.STRATEGY_ID
        or result.get("strategy_version") != _base.STRATEGY_VERSION
        or result.get("config_ids") != [_base.CONFIG_ID]
        or result.get("symbol_scope") != list(_base.MTVCLC_SYMBOLS)
        or result.get("source_contract_id") != _base.SOURCE_CONTRACT_ID
        or result.get("activity_metric_id") != _base.ACTIVITY_METRIC_ID
        or result.get("attempt_accounting") != _ACCOUNTING
        or result.get("attempt_manifest") != attempt_manifest()
        or result.get("research_only") is not True
        or result.get("source_scope_ready") is not True
        or result.get("source_errors") != []
        or any(result.get(field) is not False for field in _AUTHORITY_FIELDS)
    ):
        return False
    source_hashes = result.get("source_sha256_by_symbol")
    if (
        not isinstance(source_hashes, Mapping)
        or list(source_hashes) != list(_base.MTVCLC_SYMBOLS)
        or any(not _base._valid_sha256(value) for value in source_hashes.values())
    ):
        return False
    expected_cells = recompute_cells_from_ledgers(
        reservation_ledger=result.get("reservation_ledger"),
        outcome_ledger=result.get("outcome_ledger"),
        costs=result.get("costs"),
        source_ready_by_symbol=result.get("source_ready_by_symbol"),
    )
    cells = result.get("cells")
    if expected_cells is None or not isinstance(cells, list) or cells != expected_cells:
        return False
    if any(not isinstance(cell, Mapping) or set(cell) != _CELL_FIELDS for cell in cells):
        return False
    return result.get("all_cells_pass_fixed_screen") is all(
        cell["passes_fixed_cell_screen"] is True for cell in expected_cells
    )


def __getattr__(name: str) -> Any:
    return getattr(_base, name)


__all__ = sorted(  # noqa: PLE0605 - preserve the versioned base module's public API
    set(_base.__all__)
    | {
        "BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD",
        "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS",
        "IMMUTABLE_CURRENT_ATTEMPTED_CELLS",
        "IMMUTABLE_PRIOR_ATTEMPTED_CELLS",
        "SCREEN_RESULT_SCHEMA",
        "WIN_PROBABILITY_FAMILY_CONFIDENCE",
        "_cell_payload",
        "_wilson_one_sided_lower",
        "attempt_manifest",
        "recompute_cells_from_ledgers",
        "screen_universe",
        "validate_result_bundle",
    }
)
