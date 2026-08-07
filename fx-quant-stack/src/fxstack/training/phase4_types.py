from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(slots=True)
class SequenceDatasetManifest:
    cache_key: str
    pair: str
    timeframe: str
    dataset_fingerprint: str
    feature_service_name: str
    feature_service_version: str
    feature_contract_hash: str
    feature_schema_version: str = ""
    session_contract_version: str = ""
    multi_tf_contract_version: str = ""
    feature_columns: list[str] = field(default_factory=list)
    label_config: dict[str, Any] = field(default_factory=dict)
    rows: int = 0
    sequence_count: int = 0
    window_size: int = 0
    tensor_bundle_path: str = ""
    manifest_path: str = ""
    created_at: float = 0.0
    source: str = "feast_historical"
    timestamps_start: str = ""
    timestamps_end: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ChallengerSpec:
    name: str
    factory: Callable[[], Any]
    model_family: str = ""
    runtime_role: str = "challenger"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": str(self.name),
            "model_family": str(self.model_family),
            "runtime_role": str(self.runtime_role),
        }


@dataclass(slots=True)
class PortfolioModelSummary:
    name: str
    role: str
    cv_metrics: dict[str, float] = field(default_factory=dict)
    wf_metrics: list[dict[str, float]] = field(default_factory=list)
    cv_score: float = 0.0
    wf_score: float = 0.0
    calibration_error: float = 0.0
    candidate_metric: float = 0.0
    throughput: float = 0.0
    reliability_by_segment: dict[str, Any] = field(default_factory=dict)
    scenario_matrix: dict[str, Any] = field(default_factory=dict)
    class_balance: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PortfolioComparison:
    baseline_name: str
    candidate_name: str
    candidate_metric_delta: float = 0.0
    calibration_delta: float = 0.0
    throughput_delta: float = 0.0
    reliability_regressions: dict[str, Any] = field(default_factory=dict)
    disagreement_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
