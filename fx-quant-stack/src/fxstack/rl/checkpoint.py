"""Read-only portfolio-RL checkpoint validation, loading, and scoring."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

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
    raise TypeError(
        f"RL checkpoint {path} contains unsupported type {type(value).__name__}"
    )


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str) and value[:1] in {"{", "["}:
        try:
            return json.loads(value)
        except Exception:
            return value
    return value


def _stable_hash(value: str) -> float:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()
    return int(digest[:12], 16) / float(16**12)


def _flatten_payload(value: Any, *, prefix: str, out: dict[str, float]) -> None:
    value = _parse_jsonish(value)
    if value is None:
        return
    if isinstance(value, (bool, np.bool_)):
        out[prefix] = float(bool(value))
        return
    if isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
        value, bool
    ):
        out[prefix] = float(value)
        return
    if isinstance(value, pd.Timestamp):
        timestamp = (
            value.tz_convert("UTC")
            if value.tzinfo is not None
            else value.tz_localize("UTC")
        )
        out[f"{prefix}__unix"] = float(timestamp.timestamp())
        return
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}__{key}" if prefix else str(key)
            _flatten_payload(item, prefix=child, out=out)
        return
    if isinstance(value, (list, tuple)):
        out[f"{prefix}__len"] = float(len(value))
        numeric_values = [
            float(item)
            for item in value
            if isinstance(item, (int, float, np.integer, np.floating))
        ]
        if numeric_values:
            out[f"{prefix}__mean"] = float(np.mean(numeric_values))
            out[f"{prefix}__sum"] = float(np.sum(numeric_values))
        for index, item in enumerate(list(value)[:8]):
            _flatten_payload(item, prefix=f"{prefix}__{index}", out=out)
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
    output = frame.copy()
    if "ts" not in output.columns:
        return output
    timestamps = pd.to_datetime(output["ts"], utc=True, errors="coerce")
    if timestamps.notna().any():
        hour = timestamps.dt.hour.fillna(0).astype(float)
        day_of_week = timestamps.dt.dayofweek.fillna(0).astype(float)
        output["ts_unix"] = timestamps.astype("int64").astype(float) / 1_000_000_000.0
        output["ts_hour_sin"] = np.sin(2.0 * np.pi * hour / 24.0)
        output["ts_hour_cos"] = np.cos(2.0 * np.pi * hour / 24.0)
        output["ts_dow_sin"] = np.sin(2.0 * np.pi * day_of_week / 7.0)
        output["ts_dow_cos"] = np.cos(2.0 * np.pi * day_of_week / 7.0)
    return output


def _ordered_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    columns = [
        column
        for column in ["ts", "episode_id", "step_id", "pair"]
        if column in frame.columns
    ]
    if not columns:
        return frame.copy().reset_index(drop=True)
    output = frame.copy()
    if "ts" in output.columns:
        output["ts"] = pd.to_datetime(output["ts"], utc=True, errors="coerce")
    return output.sort_values(columns, kind="mergesort").reset_index(drop=True)


def _build_feature_matrix(frame: pd.DataFrame) -> pd.DataFrame:
    ordered = _time_features(_ordered_frame(frame))
    rows: list[dict[str, float]] = []
    for _, row in ordered.iterrows():
        features: dict[str, float] = {}
        for column, value in row.items():
            if column in _EXCLUDED_FEATURE_COLUMNS:
                continue
            _flatten_payload(value, prefix=str(column), out=features)
        if "pair" in row.index:
            features["pair_code"] = _stable_hash(str(row.get("pair") or ""))
        if "episode_id" in row.index:
            features["episode_code"] = _stable_hash(
                str(row.get("episode_id") or "")
            )
        rows.append(features)
    feature_frame = pd.DataFrame(rows).fillna(0.0)
    if feature_frame.empty:
        return pd.DataFrame(index=ordered.index)
    return feature_frame.reindex(sorted(feature_frame.columns), axis=1).fillna(0.0)


@dataclass(slots=True)
class RLLinearCheckpoint:
    """Strict read-only checkpoint contract used by the production runtime."""

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
        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.feature_names
        ):
            raise TypeError(
                "RL checkpoint feature_names must contain non-empty strings"
            )
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
                raise TypeError(
                    f"RL checkpoint {name} must be a non-negative integer"
                )
        if not isinstance(self.metrics, dict) or any(
            not isinstance(key, str) or not _is_finite_number(value)
            for key, value in self.metrics.items()
        ):
            raise TypeError(
                "RL checkpoint metrics must map strings to finite numbers"
            )
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
            raise ValueError(
                "RL checkpoint checksum must be a lowercase SHA-256 hex digest"
            )
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


__all__ = [
    "RL_LINEAR_CHECKPOINT_CHECKSUM_CONTRACT",
    "RL_LINEAR_CHECKPOINT_SCHEMA_VERSION",
    "RLLinearCheckpoint",
]
