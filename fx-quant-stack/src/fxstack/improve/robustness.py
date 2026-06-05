"""Robustness analysis of a tuned config.

A high objective at a single point can be a fragile spike that won't survive live
noise. This computes how much the objective moves when each tuned knob is nudged
+/- one step (clamped + risk-validated through the same allowlist), so an operator
can tell a robust improvement from a curve-fit cliff. It only reads/evaluates -- it
never changes the config.
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from fxstack.improve.evaluator import evaluate_config
from fxstack.improve.knobs import KNOBS_BY_NAME, apply_change_set, knob_values, validate_change_set
from fxstack.improve.objective import score_metrics


def robustness_report(
    config: dict[str, Any],
    dataset: pd.DataFrame,
    *,
    min_trades: int = 30,
    max_drawdown_pct: float = 12.0,
    knobs: list[str] | None = None,
) -> dict[str, Any]:
    """Perturb each named knob +/- one step and measure objective sensitivity."""

    base_objective = score_metrics(
        evaluate_config(config, dataset), min_trades=min_trades, max_drawdown_pct=max_drawdown_pct
    ).objective

    values = knob_values(config)
    targets = [k for k in (knobs if knobs is not None else list(values)) if k in KNOBS_BY_NAME and k in values]

    per_knob: dict[str, Any] = {}
    worst = base_objective
    max_sensitivity = 0.0
    for name in targets:
        knob = KNOBS_BY_NAME[name]
        base_val = float(values[name])
        neighbour_objs: dict[str, float] = {}
        for label, candidate in (("down", base_val - knob.step), ("up", base_val + knob.step)):
            sanitized = validate_change_set({name: candidate}, incumbent=config).sanitized
            if not sanitized:  # risk-locked move blocked -> no perturbation in that direction
                continue
            # Clamped-to-bound or risk-blocked-to-incumbent yields no real change; skip.
            if abs(float(sanitized.get(name, base_val)) - base_val) < 1e-12:
                continue
            perturbed = apply_change_set(config, sanitized)
            obj = score_metrics(
                evaluate_config(perturbed, dataset), min_trades=min_trades, max_drawdown_pct=max_drawdown_pct
            ).objective
            neighbour_objs[label] = obj
            worst = min(worst, obj)
        deltas = [abs(o - base_objective) for o in neighbour_objs.values()]
        sensitivity = max(deltas) if deltas else 0.0
        max_sensitivity = max(max_sensitivity, sensitivity)
        per_knob[name] = {
            "base_value": base_val,
            "neighbours": neighbour_objs,
            "sensitivity": sensitivity,
        }

    # Robustness score in [0, 1]: 1.0 == objective unchanged by any single-step nudge.
    drop = max(0.0, base_objective - worst)
    robustness_score = 1.0 / (1.0 + drop)
    return {
        "base_objective": base_objective,
        "worst_neighbour_objective": worst,
        "max_drop": drop,
        "max_sensitivity": max_sensitivity,
        "robustness_score": robustness_score,
        "per_knob": per_knob,
    }
