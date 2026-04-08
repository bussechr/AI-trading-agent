from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PHASE3_HARNESS_MANIFEST_VERSION = "phase3_harness_manifest_v1"


@dataclass(slots=True)
class MarketReplayBundle:
    pair: str
    timeframe: str
    dataset_hash: str = ""
    feature_service_name: str = ""
    feature_service_version: str = ""
    bars_path: str = ""
    quotes_path: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class IntentReplayBundle:
    pair: str
    intents_path: str
    policy_version: str = ""
    kernel_version: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionLedger:
    engine: str
    pair: str
    fills: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class LifecycleLedger:
    engine: str
    pair: str
    events: list[dict[str, Any]] = field(default_factory=list)
    state_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class EconomicReport:
    engine: str
    pair: str
    status: str = "pending"
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    turnover_lots: float = 0.0
    max_drawdown_pct: float = 0.0
    margin_utilization_peak: float = 0.0
    trade_count: int = 0
    partial_fill_count: int = 0
    latency_ms_p95: float = 0.0
    rejection_rate: float = 0.0
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ParityReport:
    base_engine: str
    comparison_engine: str
    pair: str
    within_tolerance: bool = False
    tolerance: dict[str, float] = field(default_factory=dict)
    deltas: dict[str, float] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ScenarioSpec:
    name: str
    spread_multiplier: float = 1.0
    slippage_multiplier: float = 1.0
    latency_ms: float = 0.0
    partial_fill_probability: float = 0.0
    quote_gap_probability: float = 0.0
    session_cutover_penalty_bps: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class HarnessRunManifest:
    engine: str
    status: str
    pair: str
    manifest_version: str = PHASE3_HARNESS_MANIFEST_VERSION
    dataset_hash: str = ""
    feature_service_name: str = ""
    feature_service_version: str = ""
    kernel_version: str = ""
    engine_version: str = ""
    command: list[str] = field(default_factory=list)
    working_directory: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
