from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

PHASE3_HARNESS_MANIFEST_VERSION = "phase3_harness_manifest_v1"
EXTERNAL_ECONOMIC_REPORT_VERSION = "fxstack_external_economic_report_v1"
EXTERNAL_STRESS_REPORT_VERSION = "fxstack_external_stress_report_v1"
REQUIRED_EXTERNAL_STRESS_SCENARIOS = (
    "WideSpread",
    "SlippageShock",
    "LatencyShock",
    "PartialFills",
    "QuoteGap",
    "SessionCutover",
)


def directory_file_hashes(
    root: str | Path,
    *,
    excluded_root: str | Path | None = None,
) -> dict[str, str]:
    """Inventory stable input bytes while excluding a nested fresh output directory."""

    source = Path(root).resolve()
    if not source.is_dir():
        raise ValueError("harness bundle directory is missing")
    excluded = Path(excluded_root).resolve() if excluded_root is not None else None
    files: dict[str, str] = {}
    for path in sorted((item for item in source.rglob("*") if item.is_file()), key=lambda item: item.as_posix()):
        resolved = path.resolve()
        if excluded is not None and (resolved == excluded or resolved.is_relative_to(excluded)):
            continue
        digest = hashlib.sha256()
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        files[resolved.relative_to(source).as_posix()] = digest.hexdigest()
    return files


def directory_sha256(root: str | Path, *, excluded_root: str | Path | None = None) -> str:
    """Hash the canonical pre-run file inventory for one input bundle."""

    encoded = json.dumps(
        directory_file_hashes(root, excluded_root=excluded_root),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


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
    artifacts: dict[str, Any] = field(default_factory=dict)
    environment: dict[str, str] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def bind_executed_economic_evidence(
        self,
        *,
        model_manifest_path: str | Path,
        economic_report_path: str | Path,
        output_dir: str | Path,
        harness_run_id: str,
        input_bundle_sha256: str,
        input_bundle_files: dict[str, str],
        process_started_at_ns: int,
        process_finished_at_ns: int,
        stress_report_paths: dict[str, str | Path],
    ) -> "HarnessRunManifest":
        """Bind a completed external run to exact active-model and report bytes."""

        if str(self.status).strip().lower() != "completed" or self.metadata.get("execute") is not True:
            raise ValueError("only a completed executed harness may bind economic evidence")
        model_manifest = Path(model_manifest_path).resolve()
        economic_report = Path(economic_report_path).resolve()
        output_root = Path(output_dir).resolve()
        if not model_manifest.is_file():
            raise ValueError("executed harness model manifest is missing")
        if not output_root.is_dir():
            raise ValueError("executed harness output directory is missing")
        if not economic_report.is_relative_to(output_root):
            raise ValueError("executed harness economic report must be inside output_dir")
        if not economic_report.is_file() or economic_report.stat().st_size <= 0:
            raise ValueError("executed harness did not create a nonempty economic report")
        if economic_report.stat().st_mtime_ns < int(process_started_at_ns):
            raise ValueError("executed harness economic report predates the process")
        if not str(self.dataset_hash).strip():
            raise ValueError("executed harness dataset_hash is required")
        if not str(self.engine_version).strip():
            raise ValueError("executed harness engine_version is required")
        if not str(harness_run_id).strip() or not str(input_bundle_sha256).strip():
            raise ValueError("executed harness run and input bundle identities are required")

        normalized_stress_paths = {
            str(name): Path(path).resolve() for name, path in dict(stress_report_paths or {}).items()
        }
        if set(normalized_stress_paths) != set(REQUIRED_EXTERNAL_STRESS_SCENARIOS):
            raise ValueError("executed harness must create every required external stress report")
        for name, stress_path in normalized_stress_paths.items():
            if not stress_path.is_relative_to(output_root):
                raise ValueError(f"executed harness stress report must be inside output_dir:{name}")
            if not stress_path.is_file() or stress_path.stat().st_size <= 0:
                raise ValueError(f"executed harness did not create a nonempty stress report:{name}")
            if stress_path.stat().st_mtime_ns < int(process_started_at_ns):
                raise ValueError(f"executed harness stress report predates the process:{name}")

        # Imported locally to keep the base harness contracts lightweight while
        # sharing the exact canonical hashing used by the release validator.
        from fxstack.training.release_evidence import active_manifest_identity, file_sha256, read_json_object

        identity = active_manifest_identity(manifest_path=model_manifest, pair=self.pair)
        if not identity.model_set_id or not identity.bundle_run_id or not identity.artifact_set_sha256:
            raise ValueError("executed harness active model identity is incomplete")
        expected_linkage = {
            "harness_run_id": str(harness_run_id),
            "engine": str(self.engine).strip().lower(),
            "engine_version": str(self.engine_version),
            "pair": str(self.pair).strip().upper(),
            "dataset_hash": str(self.dataset_hash),
            "input_bundle_sha256": str(input_bundle_sha256),
            "bundle_run_id": identity.bundle_run_id,
            "model_set_id": identity.model_set_id,
            "model_manifest_sha256": identity.model_manifest_sha256,
            "artifact_set_sha256": identity.artifact_set_sha256,
        }

        report_payload = read_json_object(economic_report)
        if str(report_payload.get("schema_version") or "") != EXTERNAL_ECONOMIC_REPORT_VERSION:
            raise ValueError("executed harness economic report schema is invalid")
        for field_name, expected_value in expected_linkage.items():
            if str(report_payload.get(field_name) or "") != str(expected_value):
                raise ValueError(f"executed harness economic report linkage mismatch:{field_name}")

        for name, stress_path in normalized_stress_paths.items():
            stress_payload = read_json_object(stress_path)
            if str(stress_payload.get("schema_version") or "") != EXTERNAL_STRESS_REPORT_VERSION:
                raise ValueError(f"executed harness stress report schema is invalid:{name}")
            if str(stress_payload.get("scenario") or "") != name:
                raise ValueError(f"executed harness stress scenario mismatch:{name}")
            for field_name, expected_value in expected_linkage.items():
                if str(stress_payload.get(field_name) or "") != str(expected_value):
                    raise ValueError(
                        f"executed harness stress report linkage mismatch:{name}:{field_name}"
                    )
        self.artifacts["economic_report"] = str(economic_report)
        self.artifacts["stress_reports"] = {
            name: str(path) for name, path in normalized_stress_paths.items()
        }
        artifact_sha256 = {
            str(key): value
            for key, value in dict(self.metadata.get("artifact_sha256") or {}).items()
        }
        artifact_sha256["economic_report"] = file_sha256(economic_report)
        artifact_sha256["stress_reports"] = {
            name: file_sha256(path) for name, path in normalized_stress_paths.items()
        }
        artifact_stats = {
            "economic_report": {
                "size": int(economic_report.stat().st_size),
                "mtime_ns": int(economic_report.stat().st_mtime_ns),
            },
            "stress_reports": {
                name: {
                    "size": int(path.stat().st_size),
                    "mtime_ns": int(path.stat().st_mtime_ns),
                }
                for name, path in normalized_stress_paths.items()
            },
        }
        self.metadata.update(
            {
                "harness_run_id": str(harness_run_id),
                "input_bundle_sha256": str(input_bundle_sha256),
                "input_bundle_files": dict(input_bundle_files),
                "process_started_at_ns": int(process_started_at_ns),
                "process_finished_at_ns": int(process_finished_at_ns),
                "bundle_run_id": identity.bundle_run_id,
                "model_set_id": identity.model_set_id,
                "model_manifest": str(model_manifest),
                "model_manifest_sha256": identity.model_manifest_sha256,
                "artifact_set_sha256": identity.artifact_set_sha256,
                "artifact_sha256": artifact_sha256,
                "artifact_stats": artifact_stats,
                "required_stress_scenarios": list(REQUIRED_EXTERNAL_STRESS_SCENARIOS),
            }
        )
        return self
