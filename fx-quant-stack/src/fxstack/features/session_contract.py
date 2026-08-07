"""Canonical UTC trading-session buckets shared by features, policy, and belief models."""

from __future__ import annotations

from datetime import UTC, datetime
from functools import lru_cache
from typing import Any

from fxstack._lazy import lazy_pandas as pd


FEATURE_SCHEMA_VERSION = "fx_features_v2"
SESSION_CONTRACT_VERSION = "utc_session_buckets_v2"
MULTI_TF_CONTRACT_VERSION = "hierarchical_v2"
SESSION_CONTRACT_TIMEZONE = "UTC"

CANONICAL_SESSION_BUCKETS = (
    "asia",
    "london_open",
    "london_ny_overlap",
    "new_york",
    "pacific",
    "unknown",
)


def feature_contract_metadata() -> dict[str, str]:
    """Return the immutable model/data contract stamped on new artifacts."""
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "session_contract_version": SESSION_CONTRACT_VERSION,
        "session_contract_timezone": SESSION_CONTRACT_TIMEZONE,
        "intraday_contract": MULTI_TF_CONTRACT_VERSION,
    }


def current_feature_schema(feature_schema: dict[str, Any] | None = None) -> dict[str, Any]:
    """Overlay the current contract on a training schema before hashing or persistence."""
    out = dict(feature_schema or {})
    out.update(feature_contract_metadata())
    return out


def feature_contract_mismatches(payload: dict[str, Any] | None) -> dict[str, tuple[str, str]]:
    """Return expected/actual pairs; absent legacy versions are deliberate mismatches."""
    actual = dict(payload or {})
    mismatches: dict[str, tuple[str, str]] = {}
    for key, expected in feature_contract_metadata().items():
        observed = str(actual.get(key) or "").strip()
        if observed != expected:
            mismatches[key] = (expected, observed)
    return mismatches

_SESSION_ALIASES = {
    "londonopen": "london_open",
    "london_new_york_overlap": "london_ny_overlap",
    "london_newyork_overlap": "london_ny_overlap",
    "london_ny": "london_ny_overlap",
    "london_ny_session": "london_ny_overlap",
    "ny_overlap": "london_ny_overlap",
    "newyork": "new_york",
    "newyork_session": "new_york",
    "new_york_session": "new_york",
    "ny": "new_york",
    "rollover": "pacific",
    "unknown_session": "unknown",
    "none": "unknown",
    "na": "unknown",
    "n_a": "unknown",
}


@lru_cache(maxsize=128)
def _normalize_session_bucket_text(raw_bucket: str) -> str:
    bucket = str(raw_bucket).strip().lower()
    if not bucket:
        return ""
    normalized = bucket.replace("-", "_").replace(" ", "_").replace("/", "_")
    return _SESSION_ALIASES.get(normalized, normalized)


def normalize_session_bucket(raw_bucket: Any) -> str:
    """Normalize canonical and legacy session labels without changing unknown extensions."""
    if isinstance(raw_bucket, str):
        return _normalize_session_bucket_text(raw_bucket)
    if raw_bucket is None:
        return ""
    try:
        if bool(pd.isna(raw_bucket)):
            return "unknown"
    except (TypeError, ValueError):
        pass
    return _normalize_session_bucket_text(str(raw_bucket))


def normalize_session_bucket_series(values: pd.Series) -> pd.Series:
    """Vectorized-index-preserving normalization for stored/model session columns."""
    return values.map(normalize_session_bucket).replace({"": "unknown"}).astype("object")


def session_bucket_series_from_ts(values: pd.Series) -> pd.Series:
    """Map timestamps to the canonical UTC session contract."""
    parsed = pd.to_datetime(values, utc=True, errors="coerce")
    hours = parsed.dt.hour
    out = pd.Series("unknown", index=values.index, dtype="object")
    out.loc[hours.between(0, 6)] = "asia"
    out.loc[hours.between(7, 11)] = "london_open"
    out.loc[hours.between(12, 15)] = "london_ny_overlap"
    out.loc[hours.between(16, 20)] = "new_york"
    out.loc[hours.between(21, 23)] = "pacific"
    return out


def _session_bucket_from_utc_hour(hour: int) -> str:
    if hour < 7:
        return "asia"
    if hour < 12:
        return "london_open"
    if hour < 16:
        return "london_ny_overlap"
    if hour < 21:
        return "new_york"
    return "pacific"


@lru_cache(maxsize=512)
def _session_bucket_from_iso_text(raw_value: str) -> str:
    text = raw_value.strip()
    if not text:
        return "unknown"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed_fallback = pd.to_datetime(raw_value, utc=True, errors="coerce")
        if pd.isna(parsed_fallback):
            return "unknown"
        return _session_bucket_from_utc_hour(int(parsed_fallback.hour))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC)
    return _session_bucket_from_utc_hour(int(parsed.hour))


def session_bucket_from_ts(ts_value: Any) -> str:
    """Scalar form of :func:`session_bucket_series_from_ts`."""
    if type(ts_value) is str:
        return _session_bucket_from_iso_text(ts_value)
    if isinstance(ts_value, datetime):
        try:
            parsed_datetime = ts_value
            if parsed_datetime.tzinfo is not None:
                parsed_datetime = parsed_datetime.astimezone(UTC)
            return _session_bucket_from_utc_hour(int(parsed_datetime.hour))
        except (TypeError, ValueError):
            return "unknown"
    parsed = pd.to_datetime(ts_value, utc=True, errors="coerce")
    if pd.isna(parsed):
        return "unknown"
    return _session_bucket_from_utc_hour(int(parsed.hour))
