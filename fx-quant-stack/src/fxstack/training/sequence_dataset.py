from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from fxstack.features.session_contract import (
    FEATURE_SCHEMA_VERSION,
    MULTI_TF_CONTRACT_VERSION,
    SESSION_CONTRACT_VERSION,
)
from fxstack.settings import get_settings
from fxstack.training.phase4_types import SequenceDatasetManifest
from fxstack.utils.hashing import hash_mapping


def _sequence_cache_root(root: str | Path | None = None) -> Path:
    if root is not None and str(root).strip():
        return Path(root)
    return Path(get_settings().sequence_dataset_cache_root)


def _timestamp_bounds(timestamps: pd.Series | None) -> tuple[str, str]:
    if timestamps is None or len(timestamps) == 0:
        return "", ""
    ts = pd.to_datetime(pd.Series(timestamps), utc=True, errors="coerce")
    ts = ts[ts.notna()]
    if ts.empty:
        return "", ""
    return str(ts.min().isoformat()), str(ts.max().isoformat())


def _temporal_metadata(timestamps: pd.Series | None) -> dict[str, Any]:
    ts = pd.to_datetime(pd.Series(timestamps) if timestamps is not None else pd.Series(dtype="datetime64[ns]"), utc=True, errors="coerce")
    ts = ts[ts.notna()].reset_index(drop=True)
    start_ts, end_ts = _timestamp_bounds(ts)
    diffs = ts.diff().dropna().dt.total_seconds() if len(ts) > 1 else pd.Series(dtype=float)
    return {
        "timestamps_start": start_ts,
        "timestamps_end": end_ts,
        "timestamps_count": int(len(ts)),
        "timestamps_monotonic_increasing": bool(ts.is_monotonic_increasing) if len(ts) else True,
        "step_count": int(len(diffs)),
        "median_step_seconds": float(diffs.median()) if len(diffs) else 0.0,
        "min_step_seconds": float(diffs.min()) if len(diffs) else 0.0,
        "max_step_seconds": float(diffs.max()) if len(diffs) else 0.0,
    }


def _build_sequences(X: pd.DataFrame, *, window_size: int) -> np.ndarray:
    arr = X.astype(float).to_numpy(dtype=np.float32)
    if arr.ndim != 2 or arr.shape[0] == 0:
        raise ValueError("X must be a non-empty 2D frame")
    n_rows, n_feat = arr.shape
    win = max(2, int(window_size))
    seq = np.zeros((n_rows, win, n_feat), dtype=np.float32)
    for i in range(n_rows):
        start = max(0, i - win + 1)
        cur = arr[start : i + 1]
        if cur.shape[0] < win:
            pad = np.repeat(cur[:1], win - cur.shape[0], axis=0)
            cur = np.vstack([pad, cur])
        seq[i] = cur[-win:]
    return seq


def build_sequence_dataset_manifest(
    *,
    X: pd.DataFrame,
    y: pd.Series | None,
    timestamps: pd.Series | None,
    pair: str,
    timeframe: str,
    window_size: int,
    dataset_fingerprint: str = "",
    feature_retrieval: dict[str, Any] | None = None,
    label_config: dict[str, Any] | None = None,
    cache_root: str | Path | None = None,
) -> SequenceDatasetManifest:
    retrieval = dict(feature_retrieval or {})
    label_payload = dict(label_config or {})
    feature_columns = [str(col) for col in list(X.columns) if str(col).strip()]
    temporal_metadata = _temporal_metadata(timestamps)
    label_payload["temporal_metadata"] = temporal_metadata
    key_payload = {
        "dataset_fingerprint": str(dataset_fingerprint or ""),
        "pair": str(pair).upper(),
        "timeframe": str(timeframe).upper(),
        "feature_service_name": str(retrieval.get("feature_service_name") or ""),
        "feature_service_version": str(retrieval.get("feature_service_version") or ""),
        "feature_contract_hash": str(retrieval.get("feature_contract_hash") or ""),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "session_contract_version": SESSION_CONTRACT_VERSION,
        "multi_tf_contract_version": MULTI_TF_CONTRACT_VERSION,
        "feature_columns": feature_columns,
        "window_size": int(window_size),
        "label_config": label_payload,
    }
    cache_key = hash_mapping(key_payload)
    cache_dir = _sequence_cache_root(cache_root) / cache_key
    manifest_path = cache_dir / "sequence_dataset_manifest.json"
    bundle_path = cache_dir / "sequence_bundle.npz"
    if manifest_path.exists():
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        return SequenceDatasetManifest(**dict(payload or {}))

    cache_dir.mkdir(parents=True, exist_ok=True)
    sequences = _build_sequences(X, window_size=int(window_size))
    y_arr = None if y is None else pd.Series(y).to_numpy(dtype=np.int64)
    ts_series = None if timestamps is None else pd.Series(timestamps)
    start_ts, end_ts = _timestamp_bounds(ts_series)
    np.savez_compressed(
        bundle_path,
        sequences=sequences,
        targets=y_arr if y_arr is not None else np.asarray([], dtype=np.int64),
        timestamps=(
            pd.to_datetime(ts_series, utc=True, errors="coerce").astype("string").fillna("").to_numpy()
            if ts_series is not None
            else np.asarray([], dtype=str)
        ),
        feature_columns=np.asarray(feature_columns, dtype=str),
        temporal_metadata_json=np.asarray([json.dumps(temporal_metadata, sort_keys=True)], dtype=str),
    )
    manifest = SequenceDatasetManifest(
        cache_key=cache_key,
        pair=str(pair).upper(),
        timeframe=str(timeframe).upper(),
        dataset_fingerprint=str(dataset_fingerprint or ""),
        feature_service_name=str(retrieval.get("feature_service_name") or ""),
        feature_service_version=str(retrieval.get("feature_service_version") or ""),
        feature_contract_hash=str(retrieval.get("feature_contract_hash") or ""),
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        session_contract_version=SESSION_CONTRACT_VERSION,
        multi_tf_contract_version=MULTI_TF_CONTRACT_VERSION,
        feature_columns=feature_columns,
        label_config=label_payload,
        rows=int(len(X)),
        sequence_count=int(sequences.shape[0]),
        window_size=int(window_size),
        tensor_bundle_path=str(bundle_path),
        manifest_path=str(manifest_path),
        created_at=float(time.time()),
        source=str(retrieval.get("source") or "feast_historical"),
        timestamps_start=start_ts,
        timestamps_end=end_ts,
    )
    manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def export_unlabeled_sequence_corpus(
    *,
    X: pd.DataFrame,
    timestamps: pd.Series | None,
    pair: str,
    timeframe: str,
    dataset_fingerprint: str = "",
    feature_retrieval: dict[str, Any] | None = None,
    window_size: int,
    cache_root: str | Path | None = None,
) -> SequenceDatasetManifest:
    return build_sequence_dataset_manifest(
        X=X,
        y=None,
        timestamps=timestamps,
        pair=pair,
        timeframe=timeframe,
        dataset_fingerprint=dataset_fingerprint,
        feature_retrieval=feature_retrieval,
        label_config={"mode": "unlabeled_corpus"},
        window_size=window_size,
        cache_root=cache_root,
    )
