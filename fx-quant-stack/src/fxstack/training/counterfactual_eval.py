from __future__ import annotations

import pandas as pd


def counterfactual_policy_value(
    df: pd.DataFrame,
    *,
    action_col: str = "exit_action",
    outcome_col: str = "realized_r",
    quality_cols: list[str] | None = None,
) -> dict[str, object]:
    if df.empty:
        return {"actions": {}, "best_action": "unknown"}
    quality_cols = list(quality_cols or ["good_entry", "bad_hold", "bad_exit", "false_reversal"])
    actions: dict[str, dict[str, float]] = {}
    for action, part in df.groupby(action_col):
        stats = {
            "count": float(len(part)),
            "mean_value": float(part[outcome_col].astype(float).mean()) if outcome_col in part.columns else 0.0,
            "win_rate": float((part[outcome_col].astype(float) > 0.0).mean()) if outcome_col in part.columns else 0.0,
        }
        for col in quality_cols:
            if col in part.columns:
                stats[col] = float(part[col].astype(float).mean())
        actions[str(action)] = stats
    best_action = "unknown"
    if actions:
        best_action = max(actions.items(), key=lambda item: float(item[1].get("mean_value", 0.0)))[0]
    return {"actions": actions, "best_action": best_action}
