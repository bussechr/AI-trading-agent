"""Lineage-aware replacement accounting for the frozen MTVCLC-v1 screen.

The mathematical strategy, signal, entry, outcome, cost, and per-cell success
rules remain in the original frozen screen.  This module changes only the
prospective-attempt lineage after two 44-cell collection attempts stopped for
operational data-integrity reasons before any signal, outcome, or performance
evaluation.
"""

from __future__ import annotations

# AGENT: ROLE: replacement-attempt accounting wrapper around frozen MTVCLC-v1 math.
# AGENT: HANDSHAKE: original frozen screen result -> 4,786-cell lineage-aware result.
# AGENT: ISOLATION: pure research math only; no I/O, issuer, activation, or order surface.

from collections.abc import Mapping, Sequence
from copy import deepcopy
import math
from statistics import NormalDist
from typing import Any

from fxstack.scalp import (
    screen_mt4_tick_volume_close_location_continuation as _base,
)


IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_742
IMMUTABLE_CURRENT_ATTEMPTED_CELLS = 44
IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_786
BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.645748208417252
WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95

_REPLACEMENT_ACCOUNTING: dict[str, int] = {
    "prior_attempted_cells_lower_bound": IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
    "current_attempted_cells": IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
    "cumulative_attempted_cells_lower_bound": (IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS),
}
_BASE_ACCOUNTING: dict[str, int] = {
    "prior_attempted_cells_lower_bound": _base.IMMUTABLE_PRIOR_ATTEMPTED_CELLS,
    "current_attempted_cells": _base.IMMUTABLE_CURRENT_ATTEMPTED_CELLS,
    "cumulative_attempted_cells_lower_bound": (
        _base.IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    ),
}


def attempt_manifest() -> dict[str, Any]:
    """Return the frozen manifest with replacement multiplicity accounting."""

    manifest = deepcopy(_base.attempt_manifest())
    manifest.update(_REPLACEMENT_ACCOUNTING)
    manifest["descriptive_df99_bonferroni_abs_t_threshold"] = (
        BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
    )
    manifest["win_probability_familywise_attempted_cells"] = (
        IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS
    )
    manifest["win_probability_alpha_allocation"] = "one_sided_0.05_over_4786"
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
        if outcome.symbol == symbol and outcome.side == side
    ]
    wins = sum(bool(outcome.full_target_hit_first) for outcome in selected)
    days = len({outcome.entry_day for outcome in selected})
    lower = _wilson_one_sided_lower(wins, len(selected))
    mean_net = (
        sum(float(outcome.net_bps) for outcome in selected) / len(selected)
        if selected
        else 0.0
    )
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


def _recalibrate_cell(cell: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(cell)
    reservations = int(result.get("reservations") or 0)
    wins = int(result.get("wins") or 0)
    days = int(result.get("independent_days") or 0)
    break_even = float(result.get("base_break_even_probability") or 0.0)
    mean_net = float(result.get("mean_net_bps") or 0.0)
    lower = _wilson_one_sided_lower(wins, reservations)
    result["win_probability_wilson_lower"] = lower
    result["passes_fixed_cell_screen"] = bool(
        result.get("source_ready") is True
        and reservations >= _base.MIN_TRADES_PER_CELL
        and days >= _base.MIN_INDEPENDENT_DAYS_PER_CELL
        and lower > break_even
        and mean_net > 0.0
    )
    return result


def screen_universe(
    *,
    bars_by_symbol: Mapping[str, Sequence[Any]],
    quotes_by_symbol: Mapping[str, Sequence[Any]],
    costs_by_symbol: Mapping[str, Any],
    source_sha256_by_symbol: Mapping[str, str],
    source_contract_id: str,
) -> dict[str, Any]:
    """Run unchanged frozen math and replace only attempt-lineage fields."""

    result = _base.screen_universe(
        bars_by_symbol=bars_by_symbol,
        quotes_by_symbol=quotes_by_symbol,
        costs_by_symbol=costs_by_symbol,
        source_sha256_by_symbol=source_sha256_by_symbol,
        source_contract_id=source_contract_id,
    )
    result["attempt_accounting"] = dict(_REPLACEMENT_ACCOUNTING)
    result["attempt_manifest"] = attempt_manifest()
    raw_cells = result.get("cells")
    if isinstance(raw_cells, list):
        result["cells"] = [
            _recalibrate_cell(cell) if isinstance(cell, Mapping) else cell
            for cell in raw_cells
        ]
        result["all_cells_pass_fixed_screen"] = all(
            isinstance(cell, Mapping) and cell.get("passes_fixed_cell_screen") is True
            for cell in result["cells"]
        )
    return result


def validate_result_bundle(result: Mapping[str, Any]) -> bool:
    """Validate exact replacement lineage plus all original screen invariants."""

    if result.get("attempt_accounting") != _REPLACEMENT_ACCOUNTING:
        return False
    if result.get("attempt_manifest") != attempt_manifest():
        return False
    cells = result.get("cells")
    if not isinstance(cells, list) or len(cells) != IMMUTABLE_CURRENT_ATTEMPTED_CELLS:
        return False
    for cell in cells:
        if not isinstance(cell, Mapping) or dict(cell) != _recalibrate_cell(cell):
            return False
    if result.get("all_cells_pass_fixed_screen") is not all(
        cell.get("passes_fixed_cell_screen") is True for cell in cells
    ):
        return False
    translated = deepcopy(dict(result))
    translated["attempt_accounting"] = dict(_BASE_ACCOUNTING)
    translated["attempt_manifest"] = _base.attempt_manifest()
    return _base.validate_result_bundle(translated)


def __getattr__(name: str) -> Any:
    """Delegate every unchanged frozen type, constant, and private helper."""

    return getattr(_base, name)


__all__ = sorted(
    set(_base.__all__)
    | {
        "BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD",
        "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS",
        "IMMUTABLE_CURRENT_ATTEMPTED_CELLS",
        "IMMUTABLE_PRIOR_ATTEMPTED_CELLS",
        "attempt_manifest",
        "screen_universe",
        "validate_result_bundle",
        "WIN_PROBABILITY_FAMILY_CONFIDENCE",
        "_cell_payload",
    }
)
