from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

from fxstack.labels.validation import PurgedKFold
from fxstack.training.counterfactual_eval import counterfactual_policy_value
from fxstack.training.promotion import evaluate_promotion
from fxstack.training.splits import calendar_walk_forward_windows
from fxstack.training.uncertainty import UncertaintyModel, ensemble_disagreement, summarize_uncertainty
from fxstack.settings import get_settings


def _safe_auc(y_true: np.ndarray, p: np.ndarray) -> float:
    if len(np.unique(y_true)) < 2:
        return 0.5
    return float(roc_auc_score(y_true, p))


def _expected_calibration_error(y_true: np.ndarray, p: np.ndarray, bins: int = 10) -> float:
    edges = np.linspace(0.0, 1.0, bins + 1)
    ece = 0.0
    for i in range(bins):
        mask = (p >= edges[i]) & (p < edges[i + 1] if i < bins - 1 else p <= edges[i + 1])
        if not np.any(mask):
            continue
        acc = float(np.mean(y_true[mask]))
        conf = float(np.mean(p[mask]))
        ece += float(np.mean(mask)) * abs(acc - conf)
    return float(ece)


def _binary_metrics(y_true: np.ndarray, p: np.ndarray) -> dict[str, float]:
    pred = (p >= 0.5).astype(int)
    return {
        "auc": _safe_auc(y_true, p),
        "accuracy": float(accuracy_score(y_true, pred)),
        "brier": float(brier_score_loss(y_true, np.clip(p, 0.0, 1.0))),
        "ece": _expected_calibration_error(y_true, np.clip(p, 0.0, 1.0)),
        "throughput": float(np.mean(pred)),
    }


def _multiclass_metrics(y_true: np.ndarray, p: np.ndarray) -> dict[str, float]:
    pred = np.asarray(p, dtype=float).argmax(axis=1)
    return {
        "accuracy": float(accuracy_score(y_true, pred)),
        "log_loss": float(log_loss(y_true, np.clip(p, 1e-9, 1.0), labels=list(range(np.asarray(p).shape[1])))),
    }


def _score_report(task: str, metrics: dict[str, float]) -> float:
    if task == "multiclass":
        return float(metrics.get("accuracy", 0.0))
    return float(metrics.get("auc", metrics.get("accuracy", 0.0)))


def _prepare_segments(meta: pd.DataFrame) -> pd.DataFrame:
    x = meta.copy()
    if "pair" not in x.columns:
        x["pair"] = "UNKNOWN"
    if "session_tag" not in x.columns:
        x["session_tag"] = "unknown"
    if "regime_bucket" not in x.columns:
        x["regime_bucket"] = "unknown"
    if "scenario_bucket" not in x.columns:
        x["scenario_bucket"] = "unknown"
    return x


def _reliability_by_segment(meta: pd.DataFrame, y_true: np.ndarray, p: np.ndarray, *, task: str) -> dict[str, Any]:
    base = _prepare_segments(meta)
    base = base.reset_index(drop=True)
    out: dict[str, Any] = {}
    if task == "multiclass":
        pred = np.asarray(p, dtype=float).argmax(axis=1)
        base["pred"] = pred
        base["target"] = y_true
        for (pair, session, regime), part in base.groupby(["pair", "session_tag", "regime_bucket"], dropna=False):
            out[f"{pair}|{session}|{regime}"] = {
                "count": int(len(part)),
                "accuracy": float((part["pred"] == part["target"]).mean()),
            }
        return out

    base["prob"] = np.asarray(p, dtype=float).reshape(-1)
    base["target"] = y_true
    for (pair, session, regime), part in base.groupby(["pair", "session_tag", "regime_bucket"], dropna=False):
        if len(part) == 0:
            continue
        out[f"{pair}|{session}|{regime}"] = {
            "count": int(len(part)),
            "brier": float(brier_score_loss(part["target"], np.clip(part["prob"], 0.0, 1.0))) if part["target"].nunique() > 1 else 0.0,
            "ece": _expected_calibration_error(part["target"].to_numpy(dtype=int), part["prob"].to_numpy(dtype=float)),
            "mean_prob": float(part["prob"].mean()),
            "hit_rate": float(part["target"].mean()),
        }
    return out


def _scenario_matrix(meta: pd.DataFrame, y_true: np.ndarray, p: np.ndarray, *, task: str) -> dict[str, Any]:
    base = _prepare_segments(meta)
    base = base.reset_index(drop=True)
    base["target"] = y_true
    if task == "multiclass":
        base["score"] = np.asarray(p, dtype=float).max(axis=1)
        base["pred"] = np.asarray(p, dtype=float).argmax(axis=1)
    else:
        base["score"] = np.asarray(p, dtype=float).reshape(-1)
        base["pred"] = (base["score"] >= 0.5).astype(int)
    out: dict[str, Any] = {}
    for bucket, part in base.groupby("scenario_bucket", dropna=False):
        out[str(bucket)] = {
            "count": int(len(part)),
            "score_mean": float(part["score"].mean()),
            "target_mean": float(part["target"].mean()) if len(part) else 0.0,
            "accuracy": float((part["pred"] == part["target"]).mean()) if len(part) else 0.0,
        }
    return out


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def validate_candidate(
    *,
    model_factory: Callable[[], Any],
    X: pd.DataFrame,
    y: pd.Series,
    timestamps: pd.Series,
    meta: pd.DataFrame | None = None,
    sample_weight: pd.Series | None = None,
    challenger_factories: list[Callable[[], Any]] | None = None,
    task: str = "binary",
    report_root: Path | None = None,
    champion_metric: float = 0.0,
    cost_stress_cols: list[str] | None = None,
    cv_splits: int = 5,
    embargo_pct: float = 0.02,
    wf_train_months: int = 6,
    wf_test_months: int = 1,
    wf_step_months: int = 1,
) -> dict[str, Any]:
    idx = pd.to_datetime(timestamps, utc=True)
    order = np.argsort(pd.Series(idx).astype("int64").to_numpy())
    Xs = X.iloc[order].reset_index(drop=True)
    ys = pd.Series(y).iloc[order].reset_index(drop=True)
    meta_df = (meta.iloc[order].reset_index(drop=True) if meta is not None else pd.DataFrame(index=Xs.index))
    weights = None if sample_weight is None else pd.Series(sample_weight).iloc[order].reset_index(drop=True)
    idx_sorted = pd.Series(idx).iloc[order].reset_index(drop=True)

    fold_metrics: list[dict[str, float]] = []
    fold_preds: list[pd.DataFrame] = []
    splitter = PurgedKFold(n_splits=int(cv_splits), embargo_pct=float(embargo_pct))
    for fold_id, fold in enumerate(splitter.split(pd.DatetimeIndex(idx_sorted))):
        model = model_factory()
        fit_kwargs = {}
        if weights is not None:
            fit_kwargs["sample_weight"] = weights.iloc[fold.train_idx]
        model.fit(Xs.iloc[fold.train_idx], ys.iloc[fold.train_idx], **fit_kwargs)
        proba = model.predict_proba(Xs.iloc[fold.valid_idx])
        if task == "multiclass":
            metrics = _multiclass_metrics(ys.iloc[fold.valid_idx].to_numpy(dtype=int), proba.to_numpy(dtype=float))
            fold_pred = pd.DataFrame({"idx": fold.valid_idx, "target": ys.iloc[fold.valid_idx].to_numpy(dtype=int)})
            for col in proba.columns:
                fold_pred[col] = proba[col].to_numpy(dtype=float)
        else:
            p1 = proba["p1"].to_numpy(dtype=float)
            metrics = _binary_metrics(ys.iloc[fold.valid_idx].to_numpy(dtype=int), p1)
            fold_pred = pd.DataFrame({"idx": fold.valid_idx, "target": ys.iloc[fold.valid_idx].to_numpy(dtype=int), "p1": p1})
        metrics["fold_id"] = float(fold_id)
        fold_metrics.append(metrics)
        fold_preds.append(fold_pred)

    cv_pred = pd.concat(fold_preds, ignore_index=True).sort_values("idx").reset_index(drop=True)
    if task == "multiclass":
        proba_arr = cv_pred[[c for c in cv_pred.columns if c.startswith("p")]].to_numpy(dtype=float)
        cv_metrics = _multiclass_metrics(cv_pred["target"].to_numpy(dtype=int), proba_arr)
    else:
        proba_arr = cv_pred["p1"].to_numpy(dtype=float)
        cv_metrics = _binary_metrics(cv_pred["target"].to_numpy(dtype=int), proba_arr)

    wf_metrics: list[dict[str, float]] = []
    for window in calendar_walk_forward_windows(
        pd.DatetimeIndex(idx_sorted),
        train_months=int(wf_train_months),
        valid_months=int(wf_test_months),
        step_months=int(wf_step_months),
    ):
        if not window.train_idx or not window.valid_idx:
            continue
        model = model_factory()
        fit_kwargs = {}
        if weights is not None:
            fit_kwargs["sample_weight"] = weights.iloc[window.train_idx]
        model.fit(Xs.iloc[window.train_idx], ys.iloc[window.train_idx], **fit_kwargs)
        proba = model.predict_proba(Xs.iloc[window.valid_idx])
        if task == "multiclass":
            metrics = _multiclass_metrics(ys.iloc[window.valid_idx].to_numpy(dtype=int), proba.to_numpy(dtype=float))
        else:
            metrics = _binary_metrics(ys.iloc[window.valid_idx].to_numpy(dtype=int), proba["p1"].to_numpy(dtype=float))
        wf_metrics.append(metrics)

    model_final = model_factory()
    fit_kwargs = {}
    if weights is not None:
        fit_kwargs["sample_weight"] = weights
    model_final.fit(Xs, ys, **fit_kwargs)
    proba_final_df = model_final.predict_proba(Xs)
    if task == "multiclass":
        final_proba = proba_final_df.to_numpy(dtype=float)
        reliability = _reliability_by_segment(meta_df, ys.to_numpy(dtype=int), final_proba, task=task)
        calibration_error = 0.0
    else:
        final_proba = proba_final_df["p1"].to_numpy(dtype=float)
        reliability = _reliability_by_segment(meta_df, ys.to_numpy(dtype=int), final_proba, task=task)
        calibration_error = float(np.mean([float(v.get("ece", 0.0)) for v in reliability.values()])) if reliability else 0.0

    challengers = list(challenger_factories or [])
    disagreement_inputs: list[np.ndarray] = []
    if task != "multiclass":
        disagreement_inputs.append(np.asarray(final_proba, dtype=float).reshape(-1))
        for factory in challengers:
            challenger = factory()
            challenger.fit(Xs, ys, **fit_kwargs)
            disagreement_inputs.append(challenger.predict_proba(Xs)["p1"].to_numpy(dtype=float))
    uncertainty_model = UncertaintyModel()
    uncertainty_model.fit(Xs)
    ood_score = uncertainty_model.ood_score(Xs)
    disagreement = ensemble_disagreement(disagreement_inputs) if disagreement_inputs else np.zeros(len(Xs), dtype=float)
    uncertainty = summarize_uncertainty(
        ood_score=ood_score,
        disagreement=disagreement,
        threshold=float(get_settings().uncertainty_threshold),
    )

    cv_score = _score_report(task, cv_metrics)
    wf_score = float(np.mean([_score_report(task, item) for item in wf_metrics])) if wf_metrics else 0.0
    throughput = float(cv_metrics.get("throughput", 1.0 if task == "multiclass" else 0.0))
    scenario_matrix = _scenario_matrix(meta_df, ys.to_numpy(dtype=int), final_proba, task=task)
    counterfactual = counterfactual_policy_value(meta_df) if "exit_action" in meta_df.columns else {"actions": {}, "best_action": "unknown"}

    report: dict[str, Any] = {
        "task": task,
        "rows": int(len(Xs)),
        "cv_metrics": cv_metrics,
        "wf_metrics": wf_metrics,
        "cv_score": float(cv_score),
        "wf_score": float(wf_score),
        "calibration_error": float(calibration_error),
        "candidate_metric": float((cv_score + wf_score) / 2.0 if wf_metrics else cv_score),
        "throughput": float(throughput),
        "label_quality": {
            "rows": int(len(ys)),
            "missing_target_share": float(pd.Series(ys).isna().mean()),
            "positive_share": float(pd.Series(ys).mean()) if task != "multiclass" else 0.0,
        },
        "class_balance": pd.Series(ys).value_counts(normalize=True).sort_index().to_dict(),
        "reliability_by_segment": reliability,
        "uncertainty": uncertainty,
        "scenario_matrix": scenario_matrix,
        "counterfactual_value": counterfactual,
        "cost_stress": {
            col: float(pd.Series(meta_df[col]).astype(float).gt(0.0).mean()) for col in (cost_stress_cols or []) if col in meta_df.columns
        },
    }
    report["promotion_decision"] = evaluate_promotion(report=report, champion_metric=float(champion_metric))

    if report_root is not None:
        report_dir = Path(report_root)
        _write_json(report_dir / "label_quality.json", report["label_quality"])
        _write_json(report_dir / "class_balance.json", report["class_balance"])
        _write_json(report_dir / "reliability_by_segment.json", report["reliability_by_segment"])
        _write_json(report_dir / "uncertainty_summary.json", report["uncertainty"])
        _write_json(report_dir / "counterfactual_policy_value.json", report["counterfactual_value"])
        _write_json(report_dir / "scenario_matrix.json", report["scenario_matrix"])
        _write_json(report_dir / "promotion_decision.json", report["promotion_decision"])
        _write_json(report_dir / "training_report.json", report)
        if task == "binary":
            _write_json(
                report_dir / "meta_uplift_report.json",
                {
                    "candidate_metric": float(report["candidate_metric"]),
                    "throughput": float(report["throughput"]),
                    "cost_stress": dict(report["cost_stress"]),
                },
            )

    return report
