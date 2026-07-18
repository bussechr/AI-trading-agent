from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
import uuid

import numpy as np
import pandas as pd

from fxstack.rl._common import _ensure_dir, _json_dump


RL_LINEAR_CHECKPOINT_SCHEMA_VERSION = "rl_linear_checkpoint_v2"
RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT = (
    "rl_linear_checkpoint_canonical_json_sha256_v1"
)
_RL_LINEAR_CHECKPOINT_FIELDS = {
    "schema_version",
    "checksum_contract",
    "target_name",
    "feature_names",
    "feature_means",
    "feature_scales",
    "weights",
    "bias",
    "train_rows",
    "val_rows",
    "metrics",
    "metadata",
    "checksum",
}


_EXCLUDED_FEATURE_COLUMNS = {
    "episode_id",
    "step_id",
    "ts",
    "pair",
    "done",
    "terminated",
    "truncated",
    "reward",
    "terminal_reason",
    "policy_version",
    "feature_service_version",
    "feature_contract_hash",
    "state_json",
    "action_json",
    "next_state_json",
    "market_by_pair_json",
    "features_by_pair_json",
    "portfolio_json",
    "policy_context_json",
    "pair_actions_json",
    "risk_trace_json",
    "execution_trace_json",
    "metadata_json",
    "schema_version",
}


def _canonical_checkpoint_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _checkpoint_checksum(payload: dict[str, Any]) -> str:
    bound_payload = {key: value for key, value in payload.items() if key != "checksum"}
    canonical = _canonical_checkpoint_json(bound_payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"duplicate JSON key in RL checkpoint: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON value in RL checkpoint: {value}")


def _is_finite_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _validate_json_value(value: Any, *, path: str) -> None:
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, int) and not isinstance(value, bool):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"RL checkpoint {path} must be finite")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"RL checkpoint {path} keys must be strings")
            _validate_json_value(item, path=f"{path}.{key}")
        return
    raise TypeError(f"RL checkpoint {path} contains unsupported type {type(value).__name__}")


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


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _stable_hash(value: str) -> float:
    import hashlib

    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def _flatten_payload(value: Any, *, prefix: str, out: dict[str, float]) -> None:
    value = _parse_jsonish(value)
    if value is None:
        return
    if isinstance(value, (bool, np.bool_)):
        out[prefix] = float(bool(value))
        return
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool):
        out[prefix] = float(value)
        return
    if isinstance(value, pd.Timestamp):
        ts = value.tz_convert("UTC") if value.tzinfo is not None else value.tz_localize("UTC")
        out[f"{prefix}__unix"] = float(ts.timestamp())
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}__{key}" if prefix else str(key)
            _flatten_payload(item, prefix=child, out=out)
        return
    if isinstance(value, (list, tuple)):
        out[f"{prefix}__len"] = float(len(value))
        numeric_values = [float(item) for item in value if isinstance(item, (int, float, np.integer, np.floating))]
        if numeric_values:
            out[f"{prefix}__mean"] = float(np.mean(numeric_values))
            out[f"{prefix}__sum"] = float(np.sum(numeric_values))
        for idx, item in enumerate(list(value)[:8]):
            _flatten_payload(item, prefix=f"{prefix}__{idx}", out=out)
        return
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return
        if stripped[:1] in {"{", "["}:
            try:
                _flatten_payload(json.loads(stripped), prefix=prefix, out=out)
                return
            except Exception:
                pass
        if prefix:
            out[f"{prefix}__hash"] = _stable_hash(stripped)
        return
    out[prefix] = _stable_hash(str(value))


def _time_features(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    if "ts" not in out.columns:
        return out
    ts = pd.to_datetime(out["ts"], utc=True, errors="coerce")
    if ts.notna().any():
        hour = ts.dt.hour.fillna(0).astype(float)
        dow = ts.dt.dayofweek.fillna(0).astype(float)
        out["ts_unix"] = ts.astype("int64").astype(float) / 1_000_000_000.0
        out["ts_hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
        out["ts_hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
        out["ts_dow_sin"] = np.sin(2.0 * np.pi * dow / 7.0)
        out["ts_dow_cos"] = np.cos(2.0 * np.pi * dow / 7.0)
    return out


def _ordered_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    cols = [col for col in ["ts", "episode_id", "step_id", "pair"] if col in frame.columns]
    if not cols:
        return frame.copy().reset_index(drop=True)
    out = frame.copy()
    if "ts" in out.columns:
        out["ts"] = pd.to_datetime(out["ts"], utc=True, errors="coerce")
    return out.sort_values(cols, kind="mergesort").reset_index(drop=True)


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


def _build_feature_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = _time_features(_ordered_frame(frame))
    rows: list[dict[str, float]] = []
    for _, row in ordered.iterrows():
        features: dict[str, float] = {}
        for col, value in row.items():
            if col in _EXCLUDED_FEATURE_COLUMNS:
                continue
            if isinstance(value, (pd.Timestamp, np.datetime64)):
                _flatten_payload(value, prefix=str(col), out=features)
                continue
            _flatten_payload(value, prefix=str(col), out=features)
        if "pair" in row.index:
            features["pair_code"] = _stable_hash(str(row.get("pair") or ""))
        if "episode_id" in row.index:
            features["episode_code"] = _stable_hash(str(row.get("episode_id") or ""))
        rows.append(features)
    feature_frame = pd.DataFrame(rows).fillna(0.0)
    if feature_frame.empty:
        return pd.DataFrame(index=ordered.index)
    return feature_frame.reindex(sorted(feature_frame.columns), axis=1).fillna(0.0)


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


@dataclass(slots=True)
class RLLinearCheckpoint:
    schema_version: str = RL_LINEAR_CHECKPOINT_SCHEMA_VERSION
    checksum_contract: str = RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT
    target_name: str = "reward"
    feature_names: list[str] = field(default_factory=list)
    feature_means: list[float] = field(default_factory=list)
    feature_scales: list[float] = field(default_factory=list)
    weights: list[float] = field(default_factory=list)
    bias: float = 0.0
    train_rows: int = 0
    val_rows: int = 0
    metrics: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    checksum: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def _validate_semantics(self) -> None:
        if self.schema_version != RL_LINEAR_CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                "RL checkpoint schema_version must be "
                f"{RL_LINEAR_CHECKPOINT_SCHEMA_VERSION!r}"
            )
        if self.checksum_contract != RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT:
            raise ValueError(
                "RL checkpoint checksum_contract must be "
                f"{RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT!r}"
            )
        if not isinstance(self.target_name, str) or not self.target_name.strip():
            raise TypeError("RL checkpoint target_name must be a non-empty string")
        if not isinstance(self.feature_names, list) or not self.feature_names:
            raise ValueError("RL checkpoint feature_names must be a non-empty list")
        if any(not isinstance(name, str) or not name.strip() for name in self.feature_names):
            raise TypeError("RL checkpoint feature_names must contain non-empty strings")
        if len(set(self.feature_names)) != len(self.feature_names):
            raise ValueError("RL checkpoint feature_names must be unique")

        for name, values in (
            ("feature_means", self.feature_means),
            ("feature_scales", self.feature_scales),
            ("weights", self.weights),
        ):
            if not isinstance(values, list) or any(
                not _is_finite_number(value) for value in values
            ):
                raise TypeError(f"RL checkpoint {name} must contain finite numbers")

        vector_lengths = {
            "feature_names": len(self.feature_names),
            "feature_means": len(self.feature_means),
            "feature_scales": len(self.feature_scales),
            "weights": len(self.weights),
        }
        if len(set(vector_lengths.values())) != 1:
            raise ValueError(f"RL checkpoint vector shape mismatch: {vector_lengths}")
        if any(float(value) < 1e-9 for value in self.feature_scales):
            raise ValueError("RL checkpoint feature_scales must be at least 1e-9")
        if not _is_finite_number(self.bias):
            raise TypeError("RL checkpoint bias must be finite")
        for name, value in (("train_rows", self.train_rows), ("val_rows", self.val_rows)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise TypeError(f"RL checkpoint {name} must be a non-negative integer")
        if not isinstance(self.metrics, dict) or any(
            not isinstance(key, str) or not _is_finite_number(value)
            for key, value in self.metrics.items()
        ):
            raise TypeError("RL checkpoint metrics must map strings to finite numbers")
        if not isinstance(self.metadata, dict):
            raise TypeError("RL checkpoint metadata must be a JSON object")
        _validate_json_value(self.metadata, path="metadata")

    def validate(self, *, require_checksum: bool = True) -> None:
        self._validate_semantics()
        if not self.checksum:
            if require_checksum:
                raise ValueError(
                    "RL checkpoint checksum is missing; legacy checkpoints require retraining"
                )
            return
        if (
            not isinstance(self.checksum, str)
            or len(self.checksum) != 64
            or any(char not in "0123456789abcdef" for char in self.checksum)
        ):
            raise ValueError("RL checkpoint checksum must be a lowercase SHA-256 hex digest")
        expected = _checkpoint_checksum(self.to_dict())
        if self.checksum != expected:
            raise ValueError("RL checkpoint checksum mismatch")

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RLLinearCheckpoint":
        if not isinstance(payload, dict):
            raise TypeError("RL checkpoint payload must be a JSON object")
        if "checksum" not in payload or "checksum_contract" not in payload:
            raise ValueError(
                "RL checkpoint integrity contract is missing; "
                "legacy checkpoints require retraining"
            )
        missing = sorted(_RL_LINEAR_CHECKPOINT_FIELDS - set(payload))
        extra = sorted(set(payload) - _RL_LINEAR_CHECKPOINT_FIELDS)
        if missing or extra:
            raise ValueError(
                f"RL checkpoint fields mismatch: missing={missing}, extra={extra}"
            )
        checksum = payload.get("checksum")
        if (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(char not in "0123456789abcdef" for char in checksum)
        ):
            raise ValueError(
                "RL checkpoint checksum is missing or malformed; "
                "legacy checkpoints require retraining"
            )
        if checksum != _checkpoint_checksum(payload):
            raise ValueError("RL checkpoint checksum mismatch")
        checkpoint = cls(
            schema_version=payload["schema_version"],
            checksum_contract=payload["checksum_contract"],
            target_name=payload["target_name"],
            feature_names=payload["feature_names"],
            feature_means=payload["feature_means"],
            feature_scales=payload["feature_scales"],
            weights=payload["weights"],
            bias=payload["bias"],
            train_rows=payload["train_rows"],
            val_rows=payload["val_rows"],
            metrics=payload["metrics"],
            metadata=payload["metadata"],
            checksum=checksum,
        )
        checkpoint.validate(require_checksum=True)
        return checkpoint

    @classmethod
    def loads(cls, payload: bytes | str) -> "RLLinearCheckpoint":
        if isinstance(payload, bytes):
            text = payload.decode("utf-8")
        elif isinstance(payload, str):
            text = payload
        else:
            raise TypeError("RL checkpoint JSON payload must be bytes or text")
        decoded = json.loads(
            text,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
        return cls.from_dict(decoded)

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

    @classmethod
    def load(cls, path: Path) -> "RLLinearCheckpoint":
        return cls.loads(Path(path).read_bytes())

    def predict_frame(self, frame: pd.DataFrame) -> np.ndarray:
        self.validate(require_checksum=False)
        features = _build_feature_matrix(frame)
        aligned = features.reindex(columns=self.feature_names, fill_value=0.0)
        matrix = aligned.to_numpy(dtype=float, copy=True)
        means = np.asarray(self.feature_means, dtype=float)
        scales = np.asarray(self.feature_scales, dtype=float)
        matrix = (matrix - means) / scales
        weights = np.asarray(self.weights, dtype=float)
        return (matrix @ weights) + float(self.bias)


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
