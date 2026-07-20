from __future__ import annotations

import math
import os
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.rl._common import _ensure_dir, _json_dump
from fxstack.rl.checkpoint import (
    RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT as _RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT,
)
from fxstack.rl.checkpoint import (
    RL_LINEAR_CHECKPOINT_SCHEMA_VERSION as _RL_LINEAR_CHECKPOINT_SCHEMA_VERSION,
)
from fxstack.rl.checkpoint import (
    RLLinearCheckpoint as _RuntimeRLLinearCheckpoint,
)
from fxstack.rl.checkpoint import (
    _build_feature_matrix,
    _canonical_checkpoint_json,
    _checkpoint_checksum,
    _ordered_frame,
    _parse_jsonish,
)

RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT = _RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT
RL_LINEAR_CHECKPOINT_SCHEMA_VERSION = _RL_LINEAR_CHECKPOINT_SCHEMA_VERSION


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _extract_action_target(row: pd.Series) -> float | None:
    action = _parse_jsonish(row.get("action_json"))
    if isinstance(action, dict):
        if "target_position" in action and action["target_position"] not in (None, ""):
            try:
                return float(action["target_position"])
            except Exception:
                pass
    pair_actions = _parse_jsonish(row.get("pair_actions_json"))
    if isinstance(pair_actions, dict):
        pair = str(row.get("pair") or "").upper()
        payload = pair_actions.get(pair)
        if isinstance(payload, dict) and payload.get("target_position") not in (None, ""):
            try:
                return float(payload["target_position"])
            except Exception:
                pass
        for payload in pair_actions.values():
            if isinstance(payload, dict) and payload.get("target_position") not in (None, ""):
                try:
                    return float(payload["target_position"])
                except Exception:
                    continue
    action = _parse_jsonish(row.get("action"))
    if isinstance(action, dict) and action.get("target_position") not in (None, ""):
        try:
            return float(action["target_position"])
        except Exception:
            pass
    return None


def _resolve_target(frame: pd.DataFrame, target_name: str) -> pd.Series:
    if target_name in frame.columns:
        try:
            series = pd.to_numeric(frame[target_name], errors="coerce")
            if series.notna().any():
                return series.fillna(0.0)
        except Exception:
            pass
    extracted: list[float] = []
    for _, row in frame.iterrows():
        target = _extract_action_target(row)
        if target is None:
            target = float(row.get("reward", 0.0) or 0.0)
        extracted.append(float(target))
    return pd.Series(extracted, index=frame.index, dtype=float)


def _split_indices(length: int, validation_fraction: float) -> tuple[np.ndarray, np.ndarray]:
    if length <= 1:
        train_idx = np.arange(length, dtype=int)
        val_idx = np.array([], dtype=int)
        return train_idx, val_idx
    val_size = int(math.ceil(length * max(0.0, min(0.9, float(validation_fraction)))))
    val_size = max(1, min(length - 1, val_size))
    split = length - val_size
    return np.arange(split, dtype=int), np.arange(split, length, dtype=int)


def _directional_accuracy(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size == 0:
        return 0.0
    true_sign = np.sign(y_true)
    pred_sign = np.sign(y_pred)
    mask = true_sign != 0.0
    if not mask.any():
        return 0.0
    return float((true_sign[mask] == pred_sign[mask]).mean())


def _corr(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    if y_true.size < 2 or y_pred.size < 2:
        return 0.0
    try:
        value = float(np.corrcoef(y_true, y_pred)[0, 1])
        return 0.0 if np.isnan(value) else value
    except Exception:
        return 0.0


class RLLinearCheckpoint(_RuntimeRLLinearCheckpoint):
    """Offline publisher extension for the runtime's read-only checkpoint."""

    def save(self, path: Path) -> Path:
        self._validate_semantics()
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["checksum"] = _checkpoint_checksum(payload)
        encoded = (_canonical_checkpoint_json(payload) + "\n").encode("utf-8")
        pending = destination.with_name(
            f".{destination.name}.tmp-{uuid.uuid4().hex}"
        )
        try:
            with pending.open("wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(pending, destination)
            _fsync_parent_directory(destination.parent)
        finally:
            try:
                pending.unlink(missing_ok=True)
            except OSError:
                pass
        self.checksum = str(payload["checksum"])
        return destination


def _fit_ridge(X: np.ndarray, y: np.ndarray, *, ridge: float) -> tuple[np.ndarray, float]:
    if X.size == 0:
        return np.zeros((0,), dtype=float), float(np.mean(y) if y.size else 0.0)
    means = X.mean(axis=0)
    scales = X.std(axis=0)
    scales = np.where(np.abs(scales) < 1e-9, 1.0, scales)
    Xn = (X - means) / scales
    X_aug = np.concatenate([np.ones((len(Xn), 1), dtype=float), Xn], axis=1)
    ident = np.eye(X_aug.shape[1], dtype=float)
    ident[0, 0] = 0.0
    try:
        weights = np.linalg.solve(X_aug.T @ X_aug + float(ridge) * ident, X_aug.T @ y)
    except np.linalg.LinAlgError:
        weights = np.linalg.lstsq(X_aug, y, rcond=None)[0]
    bias = float(weights[0]) if weights.size else float(np.mean(y) if y.size else 0.0)
    coeffs = np.asarray(weights[1:], dtype=float) if weights.size > 1 else np.zeros((Xn.shape[1],), dtype=float)
    return coeffs, bias


def _score_matrix(X: np.ndarray, weights: np.ndarray, bias: float) -> np.ndarray:
    if X.size == 0 or weights.size == 0:
        return np.full(len(X), float(bias), dtype=float)
    return (X @ weights) + float(bias)


def fit_replay_policy(
    frame: pd.DataFrame,
    *,
    out_dir: Path,
    run_name: str = "rl_research_policy",
    target_name: str = "reward",
    validation_fraction: float = 0.2,
    ridge: float = 1e-3,
) -> dict[str, Any]:
    out_dir = _ensure_dir(out_dir)
    ordered = _ordered_frame(frame)
    feature_frame = _build_feature_matrix(ordered)
    target_series = _resolve_target(ordered, target_name)
    if feature_frame.empty:
        feature_frame = pd.DataFrame(index=ordered.index)
    feature_frame = feature_frame.fillna(0.0)
    train_idx, val_idx = _split_indices(len(ordered), validation_fraction)
    X = feature_frame.to_numpy(dtype=float, copy=True) if not feature_frame.empty else np.zeros((len(ordered), 0), dtype=float)
    y = target_series.to_numpy(dtype=float, copy=True)
    X_train = X[train_idx] if len(train_idx) else np.zeros((0, X.shape[1]), dtype=float)
    y_train = y[train_idx] if len(train_idx) else np.zeros((0,), dtype=float)
    X_val = X[val_idx] if len(val_idx) else np.zeros((0, X.shape[1]), dtype=float)
    y_val = y[val_idx] if len(val_idx) else np.zeros((0,), dtype=float)

    feature_means = X_train.mean(axis=0) if X_train.size else np.zeros((X.shape[1],), dtype=float)
    feature_scales = X_train.std(axis=0) if X_train.size else np.ones((X.shape[1],), dtype=float)
    feature_scales = np.where(np.abs(feature_scales) < 1e-9, 1.0, feature_scales)
    X_train_norm = (X_train - feature_means) / feature_scales if X_train.size else X_train
    X_val_norm = (X_val - feature_means) / feature_scales if X_val.size else X_val
    weights, bias = _fit_ridge(X_train, y_train, ridge=float(ridge))
    train_pred = _score_matrix(X_train_norm, weights, bias)
    val_pred = _score_matrix(X_val_norm, weights, bias) if len(val_idx) else np.array([], dtype=float)
    train_mse = float(np.mean((y_train - train_pred) ** 2)) if y_train.size else 0.0
    val_mse = float(np.mean((y_val - val_pred) ** 2)) if y_val.size else train_mse
    train_mae = float(np.mean(np.abs(y_train - train_pred))) if y_train.size else 0.0
    val_mae = float(np.mean(np.abs(y_val - val_pred))) if y_val.size else train_mae
    metrics = {
        "rl.train.rows": float(len(ordered)),
        "rl.train.features": float(X.shape[1]),
        "rl.train.train_rows": float(len(train_idx)),
        "rl.train.val_rows": float(len(val_idx)),
        "rl.train.mse": float(train_mse),
        "rl.train.val_mse": float(val_mse),
        "rl.train.mae": float(train_mae),
        "rl.train.val_mae": float(val_mae),
        "rl.train.directional_accuracy": float(_directional_accuracy(y_train, train_pred)) if y_train.size else 0.0,
        "rl.train.val_directional_accuracy": float(_directional_accuracy(y_val, val_pred)) if y_val.size else 0.0,
        "rl.train.reward_correlation": float(_corr(y_train, train_pred)) if y_train.size else 0.0,
        "rl.train.val_reward_correlation": float(_corr(y_val, val_pred)) if y_val.size else 0.0,
    }
    checkpoint = RLLinearCheckpoint(
        target_name=str(target_name if target_name in ordered.columns else "reward"),
        feature_names=list(feature_frame.columns),
        feature_means=[float(value) for value in list(feature_means)],
        feature_scales=[float(value) for value in list(feature_scales)],
        weights=[float(value) for value in list(weights)],
        bias=float(bias),
        train_rows=int(len(train_idx)),
        val_rows=int(len(val_idx)),
        metrics={k: float(v) for k, v in metrics.items()},
        metadata={
            "run_name": run_name,
            "feature_columns": list(feature_frame.columns),
            "target_name": str(target_name),
            "validation_fraction": float(validation_fraction),
            "ridge": float(ridge),
        },
    )
    checkpoint_path = out_dir / "checkpoint.json"
    summary_path = _json_dump(
        out_dir / "training_summary.json",
        {
            "status": "ok",
            "run_name": run_name,
            "target_name": checkpoint.target_name,
            "rows": int(len(ordered)),
            "feature_count": int(X.shape[1]),
            "train_rows": int(len(train_idx)),
            "val_rows": int(len(val_idx)),
            "checkpoint_path": str(checkpoint_path),
        },
    )
    metrics_path = _json_dump(out_dir / "metrics.json", metrics)
    checkpoint.save(checkpoint_path)
    return {
        "status": "ok",
        "summary_path": str(summary_path),
        "metrics_path": str(metrics_path),
        "checkpoint_path": str(checkpoint_path),
        "checkpoint": checkpoint.to_dict(),
        "metrics": metrics,
    }


def load_replay_checkpoint(path: Path) -> RLLinearCheckpoint:
    return RLLinearCheckpoint.load(path)


def score_replay_frame(frame: pd.DataFrame, checkpoint: RLLinearCheckpoint) -> pd.DataFrame:
    ordered = _ordered_frame(frame)
    predictions = checkpoint.predict_frame(ordered)
    target = _resolve_target(ordered, checkpoint.target_name).to_numpy(dtype=float, copy=True)
    scored = ordered.copy()
    scored["prediction"] = predictions
    scored["prediction_residual"] = target - predictions
    scored["prediction_abs_error"] = np.abs(scored["prediction_residual"].astype(float))
    scored["prediction_direction"] = np.sign(scored["prediction"].astype(float))
    scored["target_direction"] = np.sign(target)
    return scored
