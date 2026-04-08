from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class FeatureServiceRef:
    name: str
    version: str
    pair: str
    timeframe: str
    component_key: str
    feature_contract_hash: str
    feature_columns: list[str] = field(default_factory=list)
    feature_view_names: list[str] = field(default_factory=list)
    entity_keys: list[str] = field(default_factory=lambda: ["pair"])
    point_in_time_key: str = "event_timestamp"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HistoricalDatasetProvenance:
    pair: str
    timeframe: str
    component_key: str
    feature_service_name: str
    feature_service_version: str
    feature_contract_hash: str
    feature_view_names: list[str] = field(default_factory=list)
    retrieval_source: str = ""
    point_in_time_key: str = "event_timestamp"
    provider: str = ""
    repo_root: str = ""
    fallback_reason: str = ""
    entity_rows: int = 0
    matched_rows: int = 0
    all_pairs: list[str] = field(default_factory=list)
    context_timeframes: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FeatureServingSnapshot:
    pair: str
    timeframe: str
    feature_service_name: str
    feature_service_version: str
    feature_contract_hash: str
    source: str
    source_chain: list[str] = field(default_factory=list)
    cache_hit: bool = False
    fresh: bool = False
    age_secs: float | None = None
    event_timestamp: str = ""
    latency_ms: float | None = None
    fallback_active: bool = False
    fallback_reason: str = ""
    missing_required_columns: list[str] = field(default_factory=list)
    last_push_status: str = ""
    last_push_age_secs: float | None = None
    outbox_backlog: int = 0
    last_parity_status: str = ""
    parity_max_abs_diff: float | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class FeaturePushIntent:
    outbox_key: str
    pair: str
    feature_service: str
    entity_key: str
    event_timestamp: float
    feature_version: str = ""
    checksum: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "runtime_feature_tail"

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["payload_json"] = dict(payload.pop("payload", {}) or {})
        return payload


@dataclass(slots=True)
class FeaturePushAttempt:
    outbox_key: str
    feature_service: str
    status: str
    worker_id: str = ""
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["payload_json"] = dict(payload.pop("payload", {}) or {})
        return payload


@dataclass(slots=True)
class FeatureParityResult:
    pair: str
    feature_service: str
    entity_key: str
    event_timestamp: float
    source: str
    parity_ok: bool
    drift_score: float | None = None
    parity_max_abs_diff: float | None = None
    message: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["payload_json"] = dict(payload.pop("payload", {}) or {})
        return payload
