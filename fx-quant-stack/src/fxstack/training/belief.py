from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.belief.dataset import build_directional_belief_dataset
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_ranker_xgb import BeliefRankerXGB
from fxstack.models.belief_regressor_xgb import BeliefRegressorXGB
from fxstack.settings import get_settings

LABEL_COLUMNS = {
    "query_id",
    "pair",
    "ts",
    "scenario",
    "side",
    "hypothesis_id",
    "row_idx",
    "local_feasible",
    "side_sign",
    "all_in_cost_bps",
    "net_ev_bps",
    "confirm_success",
    "fail_fast",
    "mfe_bps",
    "mae_bps",
    "relevance",
    "ev_above_hurdle",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _feature_matrix_from_frame(frame: pd.DataFrame) -> pd.DataFrame:
    numeric = frame.select_dtypes(include=["number", "bool"]).copy()
    numeric = numeric.drop(columns=[col for col in LABEL_COLUMNS if col in numeric.columns], errors="ignore")
    numeric = numeric.loc[:, ~numeric.columns.duplicated()].fillna(0.0)
    return numeric.astype(float)


def _split_dataset(dataset: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if dataset.empty:
        return dataset.copy(), dataset.copy()
    ts = pd.to_datetime(dataset["ts"], utc=True, errors="coerce")
    unique_ts = pd.Series(ts.dropna().unique()).sort_values().reset_index(drop=True)
    if unique_ts.empty:
        split_idx = max(1, int(len(dataset) * 0.8))
        return dataset.iloc[:split_idx].copy(), dataset.iloc[split_idx:].copy()
    cut_idx = max(1, int(len(unique_ts) * 0.8))
    if cut_idx >= len(unique_ts):
        cut_idx = max(1, len(unique_ts) - 1)
    cut_ts = unique_ts.iloc[cut_idx - 1]
    train_mask = ts <= cut_ts
    train = dataset.loc[train_mask].copy()
    valid = dataset.loc[~train_mask].copy()
    if train.empty or valid.empty:
        split_idx = max(1, int(len(dataset) * 0.8))
        return dataset.iloc[:split_idx].copy(), dataset.iloc[split_idx:].copy()
    return train, valid


def _binary_validation_metric(model: Any, X_valid: pd.DataFrame, y_valid: pd.Series) -> dict[str, float]:
    if X_valid.empty:
        return {"accuracy": 0.0, "mean_prob": 0.0}
    proba = model.predict_proba(X_valid)["p1"].astype(float)
    pred = (proba >= 0.5).astype(int)
    accuracy = float((pred == y_valid.astype(int)).mean()) if len(y_valid) else 0.0
    return {"accuracy": accuracy, "mean_prob": float(proba.mean()) if len(proba) else 0.0}


def _regression_validation_metric(model: Any, X_valid: pd.DataFrame, y_valid: pd.Series) -> dict[str, float]:
    if X_valid.empty:
        return {"mae": 0.0, "mean_pred": 0.0}
    pred = model.predict(X_valid).astype(float)
    actual = y_valid.astype(float)
    mae = float((pred - actual).abs().mean()) if len(actual) else 0.0
    return {"mae": mae, "mean_pred": float(pred.mean()) if len(pred) else 0.0}


def _ranker_validation_metric(model: BeliefRankerXGB, X_valid: pd.DataFrame, valid: pd.DataFrame) -> dict[str, float]:
    if X_valid.empty or valid.empty:
        return {"top1_relevance_mean": 0.0, "top1_net_ev_bps": 0.0, "top1_confirm_rate": 0.0, "top1_fail_fast_rate": 0.0}
    scored = valid[["query_id", "relevance", "net_ev_bps", "confirm_success", "fail_fast"]].copy()
    scored["rank_margin"] = model.predict(X_valid).astype(float).to_numpy()
    top = scored.sort_values(["query_id", "rank_margin"], ascending=[True, False]).groupby("query_id", as_index=False).head(1)
    return {
        "top1_relevance_mean": float(top["relevance"].mean()) if not top.empty else 0.0,
        "top1_net_ev_bps": float(top["net_ev_bps"].mean()) if not top.empty else 0.0,
        "top1_confirm_rate": float(top["confirm_success"].mean()) if not top.empty else 0.0,
        "top1_fail_fast_rate": float(top["fail_fast"].mean()) if not top.empty else 0.0,
    }


def export_directional_belief_dataset(
    *,
    feature_root: str,
    out: str,
    timeframe: str = "M5",
    pairs: list[str] | None = None,
    max_queries_per_pair: int = 20000,
) -> dict[str, Any]:
    dataset = build_directional_belief_dataset(
        feature_root=feature_root,
        timeframe=timeframe,
        pairs=pairs,
        out_path=out,
        max_queries_per_pair=max_queries_per_pair,
        min_expected_edge_bps=get_settings().min_expected_edge_bps,
    )
    return {
        "model": "directional_belief_dataset_v2",
        "rows": int(len(dataset)),
        "path": str(out),
        "pairs": sorted({str(p) for p in dataset.get("pair", pd.Series(dtype=str)).astype(str)}) if not dataset.empty else [],
    }


def train_directional_belief(
    *,
    feature_root: str,
    out: str,
    timeframe: str = "M5",
    pairs: list[str] | None = None,
    max_queries_per_pair: int = 20000,
    dataset_out: str | None = None,
) -> dict[str, Any]:
    s = get_settings()
    dataset = build_directional_belief_dataset(
        feature_root=feature_root,
        timeframe=timeframe,
        pairs=pairs,
        out_path=dataset_out,
        max_queries_per_pair=max_queries_per_pair,
        min_expected_edge_bps=s.min_expected_edge_bps,
    )
    if dataset.empty:
        raise RuntimeError("directional belief v2 dataset is empty")
    train_df, valid_df = _split_dataset(dataset)
    X_train = _feature_matrix_from_frame(train_df)
    X_valid = _feature_matrix_from_frame(valid_df)
    if X_train.empty:
        raise RuntimeError("directional belief v2 feature matrix is empty")
    train_qid = pd.factorize(train_df["query_id"].astype(str))[0]

    ranker = BeliefRankerXGB(params={"device": "cpu", "n_estimators": 180, "max_depth": 5, "learning_rate": 0.06})
    ranker.fit(X_train, train_df["relevance"].astype(float), qid=train_qid)

    ev_above_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 180, "max_depth": 5, "learning_rate": 0.06})
    ev_above_model.fit(X_train, train_df["ev_above_hurdle"].astype(int))

    expected_ev_model = BeliefRegressorXGB(params={"device": "cpu", "n_estimators": 180, "max_depth": 5, "learning_rate": 0.06})
    expected_ev_model.fit(X_train, train_df["net_ev_bps"].astype(float))

    confirm_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 180, "max_depth": 5, "learning_rate": 0.06})
    confirm_model.fit(X_train, train_df["confirm_success"].astype(int))

    fail_fast_model = BeliefHorizonXGB(params={"device": "cpu", "use_calibration": False, "n_estimators": 180, "max_depth": 5, "learning_rate": 0.06})
    fail_fast_model.fit(X_train, train_df["fail_fast"].astype(int))

    out_path = Path(out)
    out_path.mkdir(parents=True, exist_ok=True)
    ranker.save(out_path / "ranker_xgb")
    ev_above_model.save(out_path / "ev_above_hurdle_xgb")
    expected_ev_model.save(out_path / "expected_net_ev_bps_xgb")
    confirm_model.save(out_path / "confirm_success_xgb")
    fail_fast_model.save(out_path / "fail_fast_xgb")

    validation = {
        "ranker": _ranker_validation_metric(ranker, X_valid, valid_df),
        "ev_above_hurdle": _binary_validation_metric(ev_above_model, X_valid, valid_df["ev_above_hurdle"].astype(int)),
        "expected_net_ev_bps": _regression_validation_metric(expected_ev_model, X_valid, valid_df["net_ev_bps"].astype(float)),
        "confirm_success": _binary_validation_metric(confirm_model, X_valid, valid_df["confirm_success"].astype(int)),
        "fail_fast": _binary_validation_metric(fail_fast_model, X_valid, valid_df["fail_fast"].astype(int)),
    }
    meta = {
        "model_version": "directional_belief_v2",
        "belief_contract": "directional_belief_v2",
        "model_scope": "global_cross_pair",
        "query_granularity": "pair_ts_8_hypotheses",
        "label_kernel_version": "entry_ev_v1",
        "feature_columns": list(X_train.columns),
        "hypothesis_scenarios": [
            "trend_pullback",
            "range_mean_reversion",
            "breakout_expansion",
            "failed_breakout_reversal",
        ],
        "hypothesis_sides": ["long", "short"],
        "scenario_confirm_windows": {
            "trend_pullback": 3,
            "range_mean_reversion": 2,
            "breakout_expansion": 2,
            "failed_breakout_reversal": 3,
        },
        "scenario_eval_horizons": {
            "trend_pullback": 12,
            "range_mean_reversion": 6,
            "breakout_expansion": 8,
            "failed_breakout_reversal": 6,
        },
        "trained_at": float(time.time()),
        "training_window_summary": {
            "rows": int(len(dataset)),
            "train_rows": int(len(train_df)),
            "valid_rows": int(len(valid_df)),
            "query_count": int(dataset["query_id"].astype(str).nunique()),
            "pair_count": int(dataset["pair"].astype(str).nunique()),
            "start_ts": str(dataset["ts"].iloc[0]) if len(dataset) else "",
            "end_ts": str(dataset["ts"].iloc[-1]) if len(dataset) else "",
        },
        "validation_metrics": validation,
    }
    (out_path / "meta.json").write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
    return {
        "model": "directional_belief",
        "rows": int(len(dataset)),
        "path": str(out_path),
        "validation_metrics": validation,
        "feature_columns": len(X_train.columns),
    }
