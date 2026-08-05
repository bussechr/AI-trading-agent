"""Bounded continuity preflight for the gap-ledger MTVCLC collector.

This adapter reuses the already-tested manifest/journal edge validators without
changing their v1 source.  It binds them to the v5 collector adapter, its exact
v3 template, wrapper, and base support sources, a distinct immutable guard
identity, and the embedded gap-ledger/readiness contract.  The helper is read-only except for
exclusive creation of that guard identity when ``--initialize-guard`` is
explicitly supplied.
"""

from __future__ import annotations

# AGENT: ROLE: no-network continuity preflight for the gap-ledger collector.
# AGENT: HANDSHAKE: exact v5 adapter/template/prereg/output/config identity ->
# gap-v5 Windows guard.
# AGENT: ISOLATION: no credential read, API call, evaluation, issuer, runtime,
# activation, or trade path.
# AGENT: SIDE EFFECTS: optional exclusive publication of one immutable gap-v5
# guard identity.
import argparse
import hashlib
import importlib.util
import json
import sys
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, ClassVar

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from tools import capture_ig_mt4_m1_activity_resilient_v4 as collector_v4

TOOL_PATH = Path(__file__).resolve()
CORE_PATH = (
    REPOSITORY_ROOT
    / "tools"
    / "check_mt4_tick_volume_collector_continuity_resilient.py"
).resolve(strict=True)
GUARD_SCHEMA_VERSION = "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"
INSPECTION_SCHEMA_VERSION = (
    "fxstack.mtvclc_collector_continuity_inspection.gap_v5.v1"
)
GUARD_IDENTITY_FILENAME = "collector-guard.identity.gap-v5.v1.json"


def _load_private_continuity_core() -> Any:
    """Load the pinned reusable implementation without mutating its public import."""

    module_name = "_fxstack_mtvclc_continuity_core_gap_v5_v1"
    specification = importlib.util.spec_from_file_location(module_name, CORE_PATH)
    if specification is None or specification.loader is None:
        raise RuntimeError("continuity_core_private_loader_unavailable")
    module = importlib.util.module_from_spec(specification)
    # Dataclass resolution requires the private name during module execution.
    # It is deliberately distinct from tools.check_* so v1/v2 imports cannot
    # observe the v5 compatibility globals below.
    sys.modules[module_name] = module
    try:
        specification.loader.exec_module(module)
    except Exception:
        sys.modules.pop(module_name, None)
        raise
    return module


core = _load_private_continuity_core()


class _CollectorCompatibility:
    """Expose v5 adapter identity plus the reusable ledger implementation."""

    _SPECIAL: ClassVar[dict[str, Any]] = {
        # Active-journal records retain the inherited base-support slot.  The
        # v5-specific guard identity separately binds the adapter, template,
        # wrapper, and base sources.
        "PRESERVED_SUPPORT_PATH": collector_v4.BASE_SUPPORT_PATH,
        "PRESERVED_SUPPORT_SHA256": collector_v4.BASE_SUPPORT_SHA256,
        "PRESERVED_SUPPORT_SIZE_BYTES": collector_v4.BASE_SUPPORT_SIZE_BYTES,
    }

    def __getattr__(self, name: str) -> Any:
        if name in self._SPECIAL:
            return self._SPECIAL[name]
        if hasattr(collector_v4, name):
            return getattr(collector_v4, name)
        return getattr(collector_v4.support, name)


collector = _CollectorCompatibility()
ContinuityRefusal = core.ContinuityRefusal
GuardPolicy = core.GuardPolicy

# The reused validators resolve these globals when called.  This is a private
# module instance, so the canonical v1/v2 inspector module remains untouched.
core.collector = collector
core.TOOL_PATH = TOOL_PATH
core.GUARD_SCHEMA_VERSION = GUARD_SCHEMA_VERSION
core.INSPECTION_SCHEMA_VERSION = INSPECTION_SCHEMA_VERSION
core.GUARD_IDENTITY_FILENAME = GUARD_IDENTITY_FILENAME


def _sha256_file(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        raise ContinuityRefusal("continuity_source_unreadable") from exc


@dataclass(slots=True)
class _ReadOnlyLedgerView:
    """Minimum ledger surface needed by the collector's receipt validator."""

    root: Path
    entries: list[dict[str, Any]]


def _inspect_start_edge_durable_receipt(
    output_root: Path,
    *,
    binding: Any,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Run the collector's exact receipt validator without mutating capture state."""

    entries: list[dict[str, Any]] = []
    if bool(manifest["manifest_present"]):
        manifest_path = output_root / collector_v4.MANIFEST_FILENAME
        first_raw, _last_raw = core._read_manifest_edge_lines(manifest_path)
        entries.append(
            core._validate_manifest_entry(
                first_raw,
                binding=binding,
                output_root=output_root,
                require_first=True,
            )
        )
    receipt_path = output_root / collector_v4.START_EDGE_RECEIPT_FILENAME
    present = receipt_path.exists()
    if not present and not entries:
        return {
            "start_edge_durable_receipt_path": str(receipt_path),
            "start_edge_durable_receipt_present": False,
            "start_edge_durable_receipt_valid": False,
            "start_edge_durable_receipt_sha256": "",
            "start_edge_durable_receipt_artifact_sha256": "",
            "start_edge_first_cycle_completed_at_epoch": None,
            "start_edge_first_cycle_durable_at_epoch": None,
            "start_edge_durable_receipt_validation_side_effect_free": True,
        }
    if present and (
        not receipt_path.is_file()
        or receipt_path.is_symlink()
        or core._is_reparse_point(receipt_path)
    ):
        raise ContinuityRefusal("start_edge_durable_receipt_invalid")
    view = _ReadOnlyLedgerView(root=output_root, entries=entries)
    try:
        proof = collector_v4.validate_start_edge_durability_receipt(
            output_root,
            binding=binding,
            ledger=view,  # type: ignore[arg-type]
        )
    except collector_v4.CollectionRefusal as exc:
        raise ContinuityRefusal(str(exc)) from None
    return {
        "start_edge_durable_receipt_path": str(receipt_path),
        "start_edge_durable_receipt_present": True,
        "start_edge_durable_receipt_valid": proof.get("status") == "valid",
        "start_edge_durable_receipt_sha256": str(proof["receipt_sha256"]),
        "start_edge_durable_receipt_artifact_sha256": _sha256_file(receipt_path),
        "start_edge_first_cycle_completed_at_epoch": float(
            proof["first_cycle_completed_at_epoch"]
        ),
        "start_edge_first_cycle_durable_at_epoch": float(
            proof["first_cycle_durable_at_epoch"]
        ),
        "start_edge_durable_receipt_validation_side_effect_free": True,
    }


def _capture_data_artifact_present(output_root: Path) -> bool:
    known_files = (
        collector_v4.MANIFEST_FILENAME,
        collector_v4.ACTIVE_JOURNAL_FILENAME,
        collector_v4.START_EDGE_RECEIPT_FILENAME,
        collector_v4.TAIL_COMMITMENT_FILENAME,
        collector_v4.DATA_WRITER_LOCK_FILENAME,
    )
    if any((output_root / name).exists() for name in known_files):
        return True
    chunk_root = output_root / collector_v4.support.CHUNK_DIRECTORY
    if not chunk_root.exists():
        return False
    if (
        not chunk_root.is_dir()
        or chunk_root.is_symlink()
        or core._is_reparse_point(chunk_root)
    ):
        raise ContinuityRefusal("capture_chunk_root_invalid")
    try:
        next(chunk_root.iterdir())
    except StopIteration:
        return False
    except OSError as exc:
        raise ContinuityRefusal("capture_chunk_root_unreadable") from exc
    return True


def _inspect_capture_tail_commitment(
    output_root: Path,
    *,
    binding: Any,
    manifest: dict[str, Any],
    active_journal: dict[str, Any],
) -> dict[str, Any] | None:
    tail_path = output_root / collector_v4.TAIL_COMMITMENT_FILENAME
    if not tail_path.exists():
        if (
            time.time() < binding.t0_epoch
            and not _capture_data_artifact_present(output_root)
        ):
            return None
        raise ContinuityRefusal("tail_commitment_proof_required")
    try:
        proof = collector_v4.validate_tail_commitment_registry(output_root)
    except collector_v4.CollectionRefusal as exc:
        raise ContinuityRefusal(str(exc)) from None
    expected_binding = {
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
    }
    if (
        proof.get("status") != "valid"
        or proof.get("schema_version")
        != collector_v4.TAIL_COMMITMENT_SCHEMA_VERSION
        or proof.get("filename") != collector_v4.TAIL_COMMITMENT_FILENAME
        or proof.get("collector_source_sha256") != collector_v4.MODULE_SOURCE_SHA256
        or proof.get("collector_wrapper_source_sha256")
        != collector_v4.SUPPORT_SHA256
        or proof.get("collector_base_source_sha256")
        != collector_v4.BASE_SUPPORT_SHA256
        or (
            proof.get("committed_state_kind") != "genesis"
            and any(proof.get(key) != value for key, value in expected_binding.items())
        )
        or proof.get("manifest_sequence") != manifest["manifest_sequence"]
        or proof.get("manifest_entry_sha256")
        != manifest["manifest_last_entry_sha256"]
        or bool(proof.get("physical_journal_present"))
        != bool(active_journal["active_journal_present"])
        or proof.get("pending_operation") is not False
    ):
        raise ContinuityRefusal("tail_commitment_proof_identity_mismatch")
    if bool(active_journal["active_journal_present"]) and (
        proof.get("journal_manifest_sequence_target")
        != active_journal["active_journal_manifest_target"]
        or proof.get("journal_sequence")
        != active_journal["active_journal_sequence"]
        or proof.get("journal_entry_sha256")
        != active_journal["active_journal_last_entry_sha256"]
    ):
        raise ContinuityRefusal("tail_commitment_proof_journal_mismatch")
    return proof


def _gap_v5_guard_identity(
    *,
    collector_path: Path,
    preregistration_path: Path,
    output_root: Path,
    api_key_file: Path,
    bridge_ea_repository_source: Path,
    bridge_ea_deployed_source: Path,
    bridge_ea_deployed_ex4: Path,
    base_url: str,
    binding: Any,
    policy: GuardPolicy,
) -> dict[str, Any]:
    derivation = collector_v4.derivation_identity()
    identity = core._guard_identity(
        collector_path=collector_path,
        preregistration_path=preregistration_path,
        output_root=output_root,
        api_key_file=api_key_file,
        base_url=base_url,
        binding=binding,
        policy=policy,
    )
    identity.update(
        {
            "schema_version": GUARD_SCHEMA_VERSION,
            "continuity_core_source_path": str(CORE_PATH),
            "continuity_core_source_sha256": _sha256_file(CORE_PATH),
            "collector_adapter_derivation_identity": derivation,
            "collector_template_source_path": str(
                collector_v4.V3_COLLECTOR_TEMPLATE_PATH
            ),
            "collector_template_source_sha256": derivation[
                "v3_template_sha256"
            ],
            "collector_wrapper_source_path": str(collector_v4.SUPPORT_PATH),
            "collector_wrapper_source_sha256": collector_v4.SUPPORT_SHA256,
            "collector_base_source_path": str(collector_v4.BASE_SUPPORT_PATH),
            "collector_base_source_sha256": collector_v4.BASE_SUPPORT_SHA256,
            "start_edge_durable_receipt_path": str(
                output_root / collector_v4.START_EDGE_RECEIPT_FILENAME
            ),
            "capture_tail_commitment_contract": {
                "schema_version": collector_v4.TAIL_COMMITMENT_SCHEMA_VERSION,
                "path": str(output_root / collector_v4.TAIL_COMMITMENT_FILENAME),
                "validator_api": "validate_tail_commitment_registry",
                "collector_source_sha256": collector_v4.MODULE_SOURCE_SHA256,
                "tail_proof_required_after_collector_initialization": True,
                "dynamic_tail_proof_embedded_in_immutable_identity": False,
            },
            "upstream_producer_software_body_sha256": (
                binding.upstream_producer_software.body_sha256
            ),
            "bridge_ea_repository_source_path": str(bridge_ea_repository_source),
            "bridge_ea_repository_source_identity": (
                binding.upstream_producer_software.repository_source.as_dict()
            ),
            "bridge_ea_deployed_source_path": str(bridge_ea_deployed_source),
            "bridge_ea_deployed_source_identity": (
                binding.upstream_producer_software.deployed_source.as_dict()
            ),
            "bridge_ea_deployed_ex4_path": str(bridge_ea_deployed_ex4),
            "bridge_ea_deployed_ex4_identity": (
                binding.upstream_producer_software.deployed_ex4.as_dict()
            ),
            "resume_contract": {
                "same_preregistration_required": True,
                "same_output_root_required": True,
                "same_collector_source_required": True,
                "same_collector_support_source_required": True,
                "same_continuity_core_source_required": True,
                "same_upstream_producer_software_required": True,
                "same_bridge_ea_host_paths_required": True,
                "collector_revalidates_bridge_ea_files_each_cycle": True,
                "first_authenticated_finalized_observation_is_immutable": True,
                "late_unseen_epoch_is_permanent_gap": True,
                "late_unseen_epoch_is_never_backfilled": True,
                "late_gap_chain_is_embedded_in_main_journal_chunk_manifest": True,
                "independently_mutable_late_gap_sidecar_forbidden": True,
                "collector_revalidates_full_main_gap_chain_before_append": True,
                "maximum_start_edge_lag_seconds": (
                    collector_v4.MAXIMUM_START_EDGE_LAG_SECONDS
                ),
                "start_edge_readiness_retry_seconds": (
                    collector_v4.START_EDGE_READINESS_RETRY_SECONDS
                ),
                "start_edge_readiness_proof_schema_version": (
                    collector_v4.START_EDGE_READINESS_PROOF_SCHEMA_VERSION
                ),
                "pre_t0_snapshots_persisted": False,
                "same_reservation_for_every_readiness_poll": True,
                "market_source_rollover_refused": True,
                "t0_reset_forbidden": True,
                "observed_gaps_preserved": True,
                "exclusive_output_data_writer_lock_required": True,
            },
        }
    )
    return identity


def inspect_continuity(
    *,
    preregistration: str | Path,
    output_dir: str | Path,
    api_key_file: str | Path,
    bridge_ea_repository_source: str | Path,
    bridge_ea_deployed_source: str | Path,
    bridge_ea_deployed_ex4: str | Path,
    base_url: str,
    policy: GuardPolicy,
    initialize_guard: bool = False,
    require_guard: bool = False,
) -> dict[str, Any]:
    """Inspect only the bounded manifest/journal edges for one exact tuple."""

    if initialize_guard and require_guard:
        raise ContinuityRefusal("guard_mode_conflict")
    collector_path = collector_v4.TOOL_PATH.resolve(strict=True)
    expected_collector_path = (
        REPOSITORY_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"
    ).resolve()
    if collector_path != expected_collector_path:
        raise ContinuityRefusal("collector_source_path_invalid")
    preregistration_path = core._resolved_existing_file(
        preregistration, "preregistration_file_invalid"
    )
    output_root = core._resolved_existing_directory(
        output_dir, "output_root_invalid"
    )
    if output_root == Path(output_root.anchor) or core._is_within(
        output_root, REPOSITORY_ROOT
    ):
        raise ContinuityRefusal("output_root_must_be_external")
    api_key_path = core._validated_api_key_file(api_key_file)
    try:
        binding = collector_v4.load_preregistration(
            preregistration_path,
            bridge_ea_repository_source=bridge_ea_repository_source,
            bridge_ea_deployed_source=bridge_ea_deployed_source,
            bridge_ea_deployed_ex4=bridge_ea_deployed_ex4,
        )
        normalized_url = collector_v4.support._validated_loopback_base_url(base_url)
    except collector_v4.CollectionRefusal as exc:
        raise ContinuityRefusal(str(exc)) from None
    validated_policy = policy.validated()
    monitor = binding.producer_software_monitor
    producer = binding.upstream_producer_software
    if monitor is None or producer is None:
        raise ContinuityRefusal("upstream_producer_software_binding_missing")
    bridge_ea_repository_path = monitor.repository_source.path
    bridge_ea_deployed_source_path = monitor.deployed_source.path
    bridge_ea_deployed_ex4_path = monitor.deployed_ex4.path
    manifest = core.inspect_manifest_tail(output_root, binding=binding)
    active_journal = core.inspect_active_journal(
        output_root,
        binding=binding,
        manifest=manifest,
    )
    if manifest["manifest_present"] and not manifest["manifest_last_market_source_id"]:
        raise ContinuityRefusal("manifest_market_source_missing")
    receipt = _inspect_start_edge_durable_receipt(
        output_root,
        binding=binding,
        manifest=manifest,
    )
    tail_proof = _inspect_capture_tail_commitment(
        output_root,
        binding=binding,
        manifest=manifest,
        active_journal=active_journal,
    )

    identity = _gap_v5_guard_identity(
        collector_path=collector_path,
        preregistration_path=preregistration_path,
        output_root=output_root,
        api_key_file=api_key_path,
        bridge_ea_repository_source=bridge_ea_repository_path,
        bridge_ea_deployed_source=bridge_ea_deployed_source_path,
        bridge_ea_deployed_ex4=bridge_ea_deployed_ex4_path,
        base_url=normalized_url,
        binding=binding,
        policy=validated_policy,
    )
    identity_path = output_root / GUARD_IDENTITY_FILENAME
    present = core._validate_or_initialize_guard(
        identity_path,
        expected=identity,
        initialize=initialize_guard,
        require=require_guard,
    )
    activity_candidates = [
        value
        for value in (
            manifest["manifest_last_write_epoch"],
            active_journal["active_journal_last_write_epoch"],
        )
        if value is not None
    ]
    activity_write_epoch = (
        max(float(value) for value in activity_candidates)
        if activity_candidates
        else None
    )
    return {
        "schema_version": INSPECTION_SCHEMA_VERSION,
        "status": "continuity_preflight_passed",
        "collection_only": True,
        "evaluation_performed": False,
        "signal_computation_authorized": False,
        "outcome_access_authorized": False,
        "performance_computation_authorized": False,
        "success_claim_authorized": False,
        "issuer_authorized": False,
        "signature_authorized": False,
        "authority_granted": False,
        "runtime_authorized": False,
        "activation_authorized": False,
        "broker_access_authorized": False,
        "order_authorized": False,
        "immediate_market_trade_authorized": False,
        "collector_source_path": str(collector_path),
        "collector_source_sha256": identity["collector_source_sha256"],
        "collector_adapter_derivation_identity": collector_v4.derivation_identity(),
        "collector_template_source_path": str(
            collector_v4.V3_COLLECTOR_TEMPLATE_PATH
        ),
        "collector_template_source_sha256": identity[
            "collector_template_source_sha256"
        ],
        "collector_wrapper_source_sha256": collector_v4.SUPPORT_SHA256,
        "collector_base_source_sha256": collector_v4.BASE_SUPPORT_SHA256,
        "continuity_inspector_source_sha256": identity[
            "continuity_inspector_source_sha256"
        ],
        "continuity_core_source_sha256": identity[
            "continuity_core_source_sha256"
        ],
        "capture_integrity_contract_sha256": identity[
            "capture_integrity_contract_sha256"
        ],
        "policy_sha256": identity["policy_sha256"],
        "preregistration_path": str(preregistration_path),
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": (
            binding.preregistration_artifact_sha256
        ),
        "output_root": str(output_root),
        "api_key_file_path": str(api_key_path),
        "bridge_base_url": normalized_url,
        "capture_tail_commitment_proof": tail_proof,
        "upstream_producer_software_body_sha256": producer.body_sha256,
        "bridge_ea_repository_source_path": str(bridge_ea_repository_path),
        "bridge_ea_repository_source_identity": producer.repository_source.as_dict(),
        "bridge_ea_deployed_source_path": str(bridge_ea_deployed_source_path),
        "bridge_ea_deployed_source_identity": producer.deployed_source.as_dict(),
        "bridge_ea_deployed_ex4_path": str(bridge_ea_deployed_ex4_path),
        "bridge_ea_deployed_ex4_identity": producer.deployed_ex4.as_dict(),
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_t0_epoch": binding.t0_epoch,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        "prospective_end_epoch_exclusive": binding.end_epoch_exclusive,
        "policy": asdict(validated_policy),
        "guard_identity_path": str(identity_path),
        "guard_identity_present": present,
        "guard_identity_sha256": core._canonical_sha256(identity),
        "capture_tail_commitment_contract": identity[
            "capture_tail_commitment_contract"
        ],
        "collector_activity_last_write_epoch": activity_write_epoch,
        **manifest,
        **active_journal,
        **receipt,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Check gap-ledger MTVCLC collector continuity."
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--api-key-file", required=True)
    parser.add_argument("--bridge-ea-repository-source", required=True)
    parser.add_argument("--bridge-ea-deployed-source", required=True)
    parser.add_argument("--bridge-ea-deployed-ex4", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--tick-interval-secs", type=float, default=2.0)
    parser.add_argument("--bar-interval-secs", type=float, default=60.0)
    parser.add_argument("--bar-limit", type=int, default=400)
    parser.add_argument("--http-timeout-secs", type=float, default=5.0)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--initialize-guard", action="store_true")
    mode.add_argument("--require-guard", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = inspect_continuity(
            preregistration=args.preregistration,
            output_dir=args.output_dir,
            api_key_file=args.api_key_file,
            bridge_ea_repository_source=args.bridge_ea_repository_source,
            bridge_ea_deployed_source=args.bridge_ea_deployed_source,
            bridge_ea_deployed_ex4=args.bridge_ea_deployed_ex4,
            base_url=args.base_url,
            policy=GuardPolicy(
                tick_interval_secs=args.tick_interval_secs,
                bar_interval_secs=args.bar_interval_secs,
                bar_limit=args.bar_limit,
                http_timeout_secs=args.http_timeout_secs,
            ),
            initialize_guard=bool(args.initialize_guard),
            require_guard=bool(args.require_guard),
        )
    except ContinuityRefusal as exc:
        print(f"continuity refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            report,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
