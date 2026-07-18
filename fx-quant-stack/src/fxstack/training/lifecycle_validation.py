from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Sequence

import numpy as np
import pandas as pd
from sklearn.metrics import accuracy_score, brier_score_loss, log_loss, roc_auc_score

from fxstack.labels.validation import PurgedKFold, event_end_times_from_indices
from fxstack.settings import get_settings
from fxstack.training.counterfactual_eval import counterfactual_policy_value
from fxstack.training.phase4_types import ChallengerSpec, PortfolioComparison, PortfolioModelSummary
from fxstack.training.promotion import evaluate_promotion
from fxstack.training.splits import calendar_walk_forward_windows
from fxstack.training.uncertainty import UncertaintyModel, ensemble_disagreement, summarize_uncertainty


_EVENT_END_COLUMNS = (
    "event_end_ts",
    "label_end_ts",
    "outcome_end_ts",
    "t1_ts",
    "t1_time",
)


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
    p_clean = np.clip(np.asarray(p, dtype=float), 0.0, 1.0)
    pred = (p_clean >= 0.5).astype(int)
    return {
        "auc": _safe_auc(y_true, p_clean),
        "accuracy": float(accuracy_score(y_true, pred)),
        "brier": float(brier_score_loss(y_true, p_clean)),
        "ece": _expected_calibration_error(y_true, p_clean),
        "throughput": float(np.mean(pred)),
    }


def _multiclass_metrics(y_true: np.ndarray, p: np.ndarray) -> dict[str, float]:
    proba = np.asarray(p, dtype=float)
    pred = proba.argmax(axis=1)
    return {
        "accuracy": float(accuracy_score(y_true, pred)),
        "log_loss": float(log_loss(y_true, np.clip(proba, 1e-9, 1.0), labels=list(range(proba.shape[1])))),
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
    base = _prepare_segments(meta).reset_index(drop=True)
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
    base = _prepare_segments(meta).reset_index(drop=True)
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


def _normalize_challengers(
    *,
    challenger_factories: list[Callable[[], Any]] | None,
    named_challengers: list[ChallengerSpec | dict[str, Any]] | None,
) -> list[ChallengerSpec]:
    out: list[ChallengerSpec] = []
    for idx, item in enumerate(list(named_challengers or []), start=1):
        if isinstance(item, ChallengerSpec):
            out.append(item)
            continue
        payload = dict(item or {})
        factory = payload.get("factory")
        if not callable(factory):
            continue
        out.append(
            ChallengerSpec(
                name=str(payload.get("name") or f"challenger_{idx}"),
                factory=factory,
                model_family=str(payload.get("model_family") or ""),
                runtime_role=str(payload.get("runtime_role") or "challenger"),
            )
        )
    if out:
        return out
    for idx, factory in enumerate(list(challenger_factories or []), start=1):
        out.append(ChallengerSpec(name=f"challenger_{idx}", factory=factory))
    return out


def _resolve_event_end(meta: pd.DataFrame, timestamps: pd.Series) -> pd.DatetimeIndex | None:
    ordered = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    for column in _EVENT_END_COLUMNS:
        if column not in meta.columns:
            continue
        parsed = pd.to_datetime(meta[column], utc=True, errors="coerce")
        if not parsed.notna().any():
            continue
        resolved = [
            start if pd.isna(end) or end < start else pd.Timestamp(end)
            for start, end in zip(ordered, parsed, strict=True)
        ]
        return pd.DatetimeIndex(resolved)
    if "t1_index" in meta.columns:
        try:
            return event_end_times_from_indices(ordered, meta["t1_index"])
        except (TypeError, ValueError):
            return None
    return None


def _iso(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(pd.Timestamp(value).isoformat())


def _walk_forward_train_indices(
    train_idx: Sequence[int],
    valid_idx: Sequence[int],
    *,
    timestamps: pd.Series,
    event_end: pd.DatetimeIndex | None,
) -> list[int]:
    train = [int(i) for i in train_idx]
    if event_end is None or not train or not valid_idx:
        return train
    valid_start = pd.Timestamp(timestamps.iloc[list(valid_idx)].min())
    return [idx for idx in train if pd.Timestamp(event_end[idx]) < valid_start]


def _evaluate_model_summary(
    *,
    name: str,
    role: str,
    model_factory: Callable[[], Any],
    Xs: pd.DataFrame,
    ys: pd.Series,
    meta_df: pd.DataFrame,
    weights: pd.Series | None,
    timestamps: pd.Series,
    event_end: pd.DatetimeIndex | None,
    cv_folds: list[Any],
    wf_windows: list[Any],
    task: str,
) -> tuple[PortfolioModelSummary, np.ndarray, pd.DataFrame]:
    fold_metrics: list[dict[str, float]] = []
    fold_predictions: list[pd.DataFrame] = []
    fold_provenance: list[dict[str, Any]] = []

    for fold_id, fold in enumerate(cv_folds):
        model = model_factory()
        fit_kwargs: dict[str, Any] = {}
        if weights is not None:
            fit_kwargs["sample_weight"] = weights.iloc[fold.train_idx]
        model.fit(Xs.iloc[fold.train_idx], ys.iloc[fold.train_idx], **fit_kwargs)
        proba = model.predict_proba(Xs.iloc[fold.valid_idx])

        if task == "multiclass":
            metrics = _multiclass_metrics(
                ys.iloc[fold.valid_idx].to_numpy(dtype=int),
                proba.to_numpy(dtype=float),
            )
            fold_prediction = pd.DataFrame(
                {
                    "idx": fold.valid_idx,
                    "target": ys.iloc[fold.valid_idx].to_numpy(dtype=int),
                    "fold_id": int(fold_id),
                    "ts": timestamps.iloc[fold.valid_idx].astype(str).to_numpy(),
                }
            )
            for column in proba.columns:
                fold_prediction[column] = proba[column].to_numpy(dtype=float)
        else:
            p1 = proba["p1"].to_numpy(dtype=float)
            metrics = _binary_metrics(ys.iloc[fold.valid_idx].to_numpy(dtype=int), p1)
            fold_prediction = pd.DataFrame(
                {
                    "idx": fold.valid_idx,
                    "target": ys.iloc[fold.valid_idx].to_numpy(dtype=int),
                    "p1": p1,
                    "fold_id": int(fold_id),
                    "ts": timestamps.iloc[fold.valid_idx].astype(str).to_numpy(),
                }
            )

        calibration = dict(getattr(model, "calibration_provenance", {}) or {})
        provenance = {
            "fold_id": int(fold_id),
            "train_rows": int(len(fold.train_idx)),
            "valid_rows": int(len(fold.valid_idx)),
            "train_start_ts": _iso(timestamps.iloc[fold.train_idx].min()) if len(fold.train_idx) else "",
            "train_end_ts": _iso(timestamps.iloc[fold.train_idx].max()) if len(fold.train_idx) else "",
            "valid_start_ts": _iso(getattr(fold, "valid_start_ts", timestamps.iloc[fold.valid_idx].min())),
            "valid_end_ts": _iso(getattr(fold, "valid_end_ts", timestamps.iloc[fold.valid_idx].max())),
            "purged_event_count": int(getattr(fold, "purged_event_count", 0) or 0),
            "embargo_count": int(getattr(fold, "embargo_count", 0) or 0),
            "calibration": calibration,
        }
        metrics["fold_id"] = float(fold_id)
        metrics["train_rows"] = float(len(fold.train_idx))
        metrics["valid_rows"] = float(len(fold.valid_idx))
        fold_metrics.append(metrics)
        fold_predictions.append(fold_prediction)
        fold_provenance.append(provenance)

    if not fold_predictions:
        raise RuntimeError("candidate validation produced no out-of-fold predictions")

    cv_pred = pd.concat(fold_predictions, ignore_index=True).sort_values("idx").reset_index(drop=True)
    duplicate_rows = int(cv_pred["idx"].duplicated().sum())
    if duplicate_rows:
        raise RuntimeError("candidate validation produced duplicate out-of-fold predictions")

    covered_rows = int(cv_pred["idx"].nunique())
    coverage = float(covered_rows / len(Xs)) if len(Xs) else 0.0
    prediction_columns = [column for column in cv_pred.columns if str(column).startswith("p")]
    evaluation_meta = meta_df.iloc[cv_pred["idx"].astype(int).to_numpy()].reset_index(drop=True)
    evaluation_target = cv_pred["target"].to_numpy(dtype=int)

    if task == "multiclass":
        evaluation_proba = cv_pred[prediction_columns].fillna(0.0).to_numpy(dtype=float)
        cv_metrics = _multiclass_metrics(evaluation_target, evaluation_proba)
        aligned_proba = np.full((len(Xs), len(prediction_columns)), np.nan, dtype=float)
        aligned_proba[cv_pred["idx"].astype(int).to_numpy(), :] = evaluation_proba
        calibration_error = 0.0
    else:
        evaluation_proba = cv_pred["p1"].to_numpy(dtype=float)
        cv_metrics = _binary_metrics(evaluation_target, evaluation_proba)
        aligned_proba = np.full(len(Xs), np.nan, dtype=float)
        aligned_proba[cv_pred["idx"].astype(int).to_numpy()] = evaluation_proba
        calibration_error = float(cv_metrics.get("ece", 0.0))

    reliability = _reliability_by_segment(evaluation_meta, evaluation_target, evaluation_proba, task=task)
    scenario_matrix = _scenario_matrix(evaluation_meta, evaluation_target, evaluation_proba, task=task)

    wf_metrics: list[dict[str, float]] = []
    for window in wf_windows:
        if not window.train_idx or not window.valid_idx:
            continue
        train_idx = _walk_forward_train_indices(
            window.train_idx,
            window.valid_idx,
            timestamps=timestamps,
            event_end=event_end,
        )
        if not train_idx:
            continue
        model = model_factory()
        fit_kwargs = {}
        if weights is not None:
            fit_kwargs["sample_weight"] = weights.iloc[train_idx]
        model.fit(Xs.iloc[train_idx], ys.iloc[train_idx], **fit_kwargs)
        proba = model.predict_proba(Xs.iloc[window.valid_idx])
        if task == "multiclass":
            metrics = _multiclass_metrics(
                ys.iloc[window.valid_idx].to_numpy(dtype=int),
                proba.to_numpy(dtype=float),
            )
        else:
            metrics = _binary_metrics(
                ys.iloc[window.valid_idx].to_numpy(dtype=int),
                proba["p1"].to_numpy(dtype=float),
            )
        metrics["train_rows"] = float(len(train_idx))
        metrics["valid_rows"] = float(len(window.valid_idx))
        wf_metrics.append(metrics)

    cv_score = _score_report(task, cv_metrics)
    wf_score = float(np.mean([_score_report(task, item) for item in wf_metrics])) if wf_metrics else 0.0
    throughput = float(cv_metrics.get("throughput", 1.0 if task == "multiclass" else 0.0))
    summary = PortfolioModelSummary(
        name=str(name),
        role=str(role),
        cv_metrics=cv_metrics,
        wf_metrics=wf_metrics,
        cv_score=float(cv_score),
        wf_score=float(wf_score),
        calibration_error=float(calibration_error),
        candidate_metric=float((cv_score + wf_score) / 2.0 if wf_metrics else cv_score),
        throughput=float(throughput),
        reliability_by_segment=reliability,
        scenario_matrix=scenario_matrix,
        class_balance=pd.Series(ys).value_counts(normalize=True).sort_index().to_dict(),
    )
    cv_pred.attrs["provenance"] = {
        "prediction_source": "out_of_fold",
        "rows": int(len(Xs)),
        "predicted_rows": covered_rows,
        "coverage": coverage,
        "duplicate_rows": duplicate_rows,
        "fold_count": int(len(fold_provenance)),
        "event_aware_purging": bool(event_end is not None),
        "folds": fold_provenance,
    }
    return summary, np.asarray(aligned_proba, dtype=float), cv_pred


def _pairwise_disagreement_summary(
    *,
    candidate_name: str,
    candidate_probs: np.ndarray,
    challenger_probs: dict[str, np.ndarray],
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "candidate_name": str(candidate_name),
        "pairwise_mean_abs_diff": {},
        "pairwise_p95_abs_diff": {},
        "max_abs_diff": 0.0,
    }
    candidate = np.asarray(candidate_probs, dtype=float).reshape(-1)
    for name, probs in challenger_probs.items():
        challenger = np.asarray(probs, dtype=float).reshape(-1)
        finite = np.isfinite(candidate) & np.isfinite(challenger)
        diffs = np.abs(candidate[finite] - challenger[finite])
        out["pairwise_mean_abs_diff"][str(name)] = float(diffs.mean()) if diffs.size else 0.0
        out["pairwise_p95_abs_diff"][str(name)] = float(np.quantile(diffs, 0.95)) if diffs.size else 0.0
        out["max_abs_diff"] = max(float(out["max_abs_diff"]), float(diffs.max()) if diffs.size else 0.0)
    return out


def _segment_reliability_deltas(
    *,
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    task: str,
    min_segment_samples: int,
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    keys = sorted(set(candidate) | set(baseline))
    for key in keys:
        cand = dict(candidate.get(key) or {})
        base = dict(baseline.get(key) or {})
        count = int(max(cand.get("count", 0), base.get("count", 0)) or 0)
        if count < int(min_segment_samples):
            continue
        if task == "multiclass":
            out[str(key)] = {
                "count": count,
                "accuracy_delta": float(cand.get("accuracy", 0.0)) - float(base.get("accuracy", 0.0)),
            }
        else:
            out[str(key)] = {
                "count": count,
                "ece_delta": float(cand.get("ece", 0.0)) - float(base.get("ece", 0.0)),
                "brier_delta": float(cand.get("brier", 0.0)) - float(base.get("brier", 0.0)),
                "hit_rate_delta": float(cand.get("hit_rate", 0.0)) - float(base.get("hit_rate", 0.0)),
            }
    return out


def _material_reliability_regressions(*, deltas: dict[str, Any], task: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, payload in dict(deltas or {}).items():
        item = dict(payload or {})
        if task == "multiclass":
            if float(item.get("accuracy_delta", 0.0)) < 0.0:
                out[str(key)] = item
        else:
            if float(item.get("ece_delta", 0.0)) > 0.0 or float(item.get("brier_delta", 0.0)) > 0.0:
                out[str(key)] = item
    return out


def _oof_ood_score(Xs: pd.DataFrame, cv_folds: list[Any]) -> np.ndarray:
    scores = np.full(len(Xs), np.nan, dtype=float)
    for fold in cv_folds:
        if not len(fold.train_idx) or not len(fold.valid_idx):
            continue
        model = UncertaintyModel()
        model.fit(Xs.iloc[fold.train_idx])
        scores[fold.valid_idx] = model.ood_score(Xs.iloc[fold.valid_idx])
    return np.nan_to_num(scores, nan=0.0, posinf=0.0, neginf=0.0)


def validate_candidate(
    *,
    model_factory: Callable[[], Any],
    X: pd.DataFrame,
    y: pd.Series,
    timestamps: pd.Series,
    meta: pd.DataFrame | None = None,
    sample_weight: pd.Series | None = None,
    challenger_factories: list[Callable[[], Any]] | None = None,
    named_challengers: list[ChallengerSpec | dict[str, Any]] | None = None,
    portfolio_champion_name: str = "",
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
    meta_df = meta.iloc[order].reset_index(drop=True) if meta is not None else pd.DataFrame(index=Xs.index)
    weights = None if sample_weight is None else pd.Series(sample_weight).iloc[order].reset_index(drop=True)
    idx_sorted = pd.Series(idx).iloc[order].reset_index(drop=True)
    event_end = _resolve_event_end(meta_df, idx_sorted)

    splitter = PurgedKFold(n_splits=int(cv_splits), embargo_pct=float(embargo_pct))
    cv_folds = list(splitter.split(pd.DatetimeIndex(idx_sorted), event_end=event_end))
    wf_windows = list(
        calendar_walk_forward_windows(
            pd.DatetimeIndex(idx_sorted),
            train_months=int(wf_train_months),
            valid_months=int(wf_test_months),
            step_months=int(wf_step_months),
        )
    )

    candidate_summary, final_proba, candidate_oof = _evaluate_model_summary(
        name="candidate",
        role="candidate",
        model_factory=model_factory,
        Xs=Xs,
        ys=ys,
        meta_df=meta_df,
        weights=weights,
        timestamps=idx_sorted,
        event_end=event_end,
        cv_folds=cv_folds,
        wf_windows=wf_windows,
        task=task,
    )
    challengers = _normalize_challengers(
        challenger_factories=challenger_factories,
        named_challengers=named_challengers,
    )

    portfolio_models: dict[str, PortfolioModelSummary] = {candidate_summary.name: candidate_summary}
    challenger_probs: dict[str, np.ndarray] = {}
    for spec in challengers:
        summary, probs, _ = _evaluate_model_summary(
            name=str(spec.name),
            role=str(spec.runtime_role),
            model_factory=spec.factory,
            Xs=Xs,
            ys=ys,
            meta_df=meta_df,
            weights=weights,
            timestamps=idx_sorted,
            event_end=event_end,
            cv_folds=cv_folds,
            wf_windows=wf_windows,
            task=task,
        )
        portfolio_models[str(spec.name)] = summary
        challenger_probs[str(spec.name)] = np.asarray(probs, dtype=float)

    disagreement_inputs: list[np.ndarray] = []
    if task != "multiclass":
        disagreement_inputs.append(np.asarray(final_proba, dtype=float).reshape(-1))
        disagreement_inputs.extend(np.asarray(item, dtype=float).reshape(-1) for item in challenger_probs.values())
    ood_score = _oof_ood_score(Xs, cv_folds)
    disagreement = ensemble_disagreement(disagreement_inputs) if disagreement_inputs else np.zeros(len(Xs), dtype=float)
    uncertainty = summarize_uncertainty(
        ood_score=ood_score,
        disagreement=disagreement,
        threshold=float(get_settings().uncertainty_threshold),
    )
    uncertainty["source"] = "out_of_fold"

    baseline_name = str(portfolio_champion_name or "").strip()
    if not baseline_name and challengers:
        baseline_name = str(challengers[0].name)
    baseline_summary = portfolio_models.get(baseline_name)
    effective_champion_metric = float(
        baseline_summary.candidate_metric if baseline_summary is not None else champion_metric
    )
    reliability_deltas = (
        _segment_reliability_deltas(
            candidate=candidate_summary.reliability_by_segment,
            baseline=baseline_summary.reliability_by_segment,
            task=task,
            min_segment_samples=int(get_settings().min_segment_samples),
        )
        if baseline_summary is not None
        else {}
    )
    reliability_regressions = _material_reliability_regressions(deltas=reliability_deltas, task=task)
    comparison = PortfolioComparison(
        baseline_name=str(baseline_name),
        candidate_name="candidate",
        candidate_metric_delta=float(candidate_summary.candidate_metric - effective_champion_metric),
        calibration_delta=(
            float(candidate_summary.calibration_error - baseline_summary.calibration_error)
            if baseline_summary is not None
            else 0.0
        ),
        throughput_delta=(
            float(candidate_summary.throughput - baseline_summary.throughput)
            if baseline_summary is not None
            else 0.0
        ),
        reliability_regressions=reliability_regressions,
        disagreement_summary=(
            _pairwise_disagreement_summary(
                candidate_name="candidate",
                candidate_probs=np.asarray(final_proba, dtype=float).reshape(-1),
                challenger_probs=challenger_probs,
            )
            if task != "multiclass"
            else {}
        ),
    )

    scenario_matrix = candidate_summary.scenario_matrix
    counterfactual = (
        counterfactual_policy_value(meta_df)
        if "exit_action" in meta_df.columns
        else {"actions": {}, "best_action": "unknown"}
    )
    report: dict[str, Any] = {
        "task": task,
        "rows": int(len(Xs)),
        "cv_metrics": dict(candidate_summary.cv_metrics),
        "wf_metrics": list(candidate_summary.wf_metrics),
        "cv_score": float(candidate_summary.cv_score),
        "wf_score": float(candidate_summary.wf_score),
        "calibration_error": float(candidate_summary.calibration_error),
        "candidate_metric": float(candidate_summary.candidate_metric),
        "throughput": float(candidate_summary.throughput),
        "oof_provenance": dict(candidate_oof.attrs.get("provenance") or {}),
        "label_quality": {
            "rows": int(len(ys)),
            "missing_target_share": float(pd.Series(ys).isna().mean()),
            "positive_share": float(pd.Series(ys).mean()) if task != "multiclass" else 0.0,
        },
        "class_balance": candidate_summary.class_balance,
        "reliability_by_segment": dict(candidate_summary.reliability_by_segment),
        "uncertainty": uncertainty,
        "scenario_matrix": scenario_matrix,
        "counterfactual_value": counterfactual,
        "cost_stress": {
            column: float(pd.Series(meta_df[column]).astype(float).gt(0.0).mean())
            for column in (cost_stress_cols or [])
            if column in meta_df.columns
        },
        "portfolio_report": {
            "models": {name: summary.to_dict() for name, summary in portfolio_models.items()},
            "named_challengers": [item.to_dict() for item in challengers],
            "baseline_name": str(baseline_name),
        },
        "challenger_head_to_head": comparison.to_dict(),
        "portfolio_disagreement": dict(comparison.disagreement_summary),
        "reliability_deltas": reliability_deltas,
    }
    promotion = evaluate_promotion(report=report, champion_metric=effective_champion_metric)
    if baseline_summary is not None:
        gates = dict(promotion.get("gates") or {})
        gates["reliability"] = not bool(reliability_regressions)
        promotion["gates"] = gates
        promotion["champion_name"] = str(baseline_name)
        promotion["champion_metric"] = float(effective_champion_metric)
        promotion["reliability_regressions"] = reliability_regressions
        promotion["head_to_head"] = comparison.to_dict()
        promotion["status"] = "eligible" if all(bool(item) for item in gates.values()) else "research_only"
    report["promotion_decision"] = promotion

    if report_root is not None:
        report_dir = Path(report_root)
        report_dir.mkdir(parents=True, exist_ok=True)
        candidate_oof.to_csv(report_dir / "oof_predictions.csv", index=False)
        _write_json(report_dir / "oof_provenance.json", report["oof_provenance"])
        _write_json(report_dir / "label_quality.json", report["label_quality"])
        _write_json(report_dir / "class_balance.json", report["class_balance"])
        _write_json(report_dir / "reliability_by_segment.json", report["reliability_by_segment"])
        _write_json(report_dir / "uncertainty_summary.json", report["uncertainty"])
        _write_json(report_dir / "counterfactual_policy_value.json", report["counterfactual_value"])
        _write_json(report_dir / "scenario_matrix.json", report["scenario_matrix"])
        _write_json(report_dir / "promotion_decision.json", report["promotion_decision"])
        _write_json(report_dir / "portfolio_report.json", report["portfolio_report"])
        _write_json(report_dir / "challenger_head_to_head.json", report["challenger_head_to_head"])
        _write_json(report_dir / "portfolio_disagreement.json", report["portfolio_disagreement"])
        _write_json(report_dir / "training_report.json", report)
        if task == "binary":
            _write_json(
                report_dir / "meta_uplift_report.json",
                {
                    "candidate_metric": float(report["candidate_metric"]),
                    "throughput": float(report["throughput"]),
                    "cost_stress": dict(report["cost_stress"]),
                    "prediction_source": "out_of_fold",
                },
            )

    return report
