"""Post-window handoff verifier for the anchored MTVCLC gap-v3 capture.

The established streaming verifier remains the row-level engine.  This
versioned boundary additionally authenticates the corrected preregistration,
the exact three-source collector identity, every embedded gap-chain anchor,
the post-fsync start-edge receipt, and the closed collector topology.  It emits
only an authority-free content-addressed inventory.
"""

from __future__ import annotations

# AGENT: ROLE: offline gap-v3 capture-to-research handoff verifier.
# AGENT: HANDSHAKE: sealed v3 declaration + closed capture -> v2 handoff inventory.
# AGENT: ISOLATION: no network, credential, outcome, issuer, runtime, or trade surface.
import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOOL_PATH = Path(__file__).resolve()
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
SEALER_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v3.py"
)
LEGACY_HANDOFF_PATH = REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff.py"


@dataclass(frozen=True, slots=True)
class ExactSourceImage:
    path: Path
    raw: bytes
    stat_identity: tuple[int, int, int, int, int]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.raw).hexdigest()

    @property
    def size_bytes(self) -> int:
        return len(self.raw)

    def identity(self) -> dict[str, Any]:
        return {
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


def _source_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _source_is_reparse(path: Path, value: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(
        int(getattr(value, "st_file_attributes", 0)) & marker
    )


def _read_exact_source(path: str | Path, *, reason: str) -> ExactSourceImage:
    candidate = Path(path).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            _source_is_reparse(candidate, before_path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > 8 * 1024 * 1024
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
        try:
            before_handle = os.fstat(descriptor)
            if not stat.S_ISREG(before_handle.st_mode):
                raise OSError(reason)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                raw = handle.read(before_handle.st_size + 1)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(reason) from exc
    identities = {
        _source_stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise RuntimeError(reason)
    return ExactSourceImage(candidate, raw, identities.pop())


@contextmanager
def _temporary_exact_modules(
    bindings: Mapping[str, ModuleType],
):  # type: ignore[no-untyped-def]
    package_names: set[str] = set()
    for name in bindings:
        parts = name.split(".")
        package_names.update(".".join(parts[:index]) for index in range(1, len(parts)))
    packages: dict[str, ModuleType] = {}
    for name in sorted(package_names, key=lambda item: item.count(".")):
        package = ModuleType(name)
        package.__package__ = name
        package.__path__ = []  # type: ignore[attr-defined]
        packages[name] = package
    installed: dict[str, ModuleType] = {**packages, **bindings}
    missing = object()
    previous: dict[str, object] = {
        name: sys.modules.get(name, missing) for name in installed
    }
    try:
        for name, module in sorted(
            installed.items(), key=lambda item: item[0].count(".")
        ):
            sys.modules[name] = module
            parent_name, _, child = name.rpartition(".")
            if parent_name:
                setattr(installed[parent_name], child, module)
        yield
    finally:
        for name in sorted(installed, key=lambda item: item.count("."), reverse=True):
            prior = previous[name]
            if prior is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior  # type: ignore[assignment]


def _execute_exact_source(
    image: ExactSourceImage,
    *,
    module_name: str,
    injected_modules: Mapping[str, ModuleType] | None = None,
) -> ModuleType:
    module = ModuleType(module_name)
    module.__file__ = str(image.path)
    module.__package__ = module_name.rpartition(".")[0]
    module.__dict__["__fxstack_exact_source_path__"] = image.path
    module.__dict__["__fxstack_exact_source_raw__"] = image.raw
    module.__dict__["__fxstack_exact_source_stat_identity__"] = image.stat_identity
    bindings = dict(injected_modules or {})
    bindings[module_name] = module
    try:
        with _temporary_exact_modules(bindings):
            exec(  # noqa: S102 - exact stable source image; workspace pyc forbidden
                compile(image.raw, str(image.path), "exec", dont_inherit=True),
                module.__dict__,
            )
    except Exception as exc:
        raise RuntimeError(f"exact_source_import_invalid:{image.path.name}") from exc
    return module


def _self_exact_source_image() -> ExactSourceImage:
    bound_path = globals().get("__fxstack_exact_source_path__")
    bound_raw = globals().get("__fxstack_exact_source_raw__")
    bound_identity = globals().get("__fxstack_exact_source_stat_identity__")
    if (
        isinstance(bound_path, Path)
        and bound_path == TOOL_PATH
        and isinstance(bound_raw, bytes)
        and isinstance(bound_identity, tuple)
        and len(bound_identity) == 5
        and all(isinstance(value, int) for value in bound_identity)
    ):
        return ExactSourceImage(TOOL_PATH, bound_raw, bound_identity)
    return _read_exact_source(TOOL_PATH, reason="handoff_source_import_invalid")


COLLECTOR_SOURCE_IMAGE = _read_exact_source(
    COLLECTOR_PATH, reason="collector_source_import_invalid"
)
collector = _execute_exact_source(
    COLLECTOR_SOURCE_IMAGE,
    module_name="_fxstack_mtvclc_gap_v3_handoff_collector",
)
if (
    collector.MODULE_SOURCE_SHA256 != COLLECTOR_SOURCE_IMAGE.sha256
    or collector.MODULE_SOURCE_SIZE_BYTES != COLLECTOR_SOURCE_IMAGE.size_bytes
):
    raise RuntimeError("collector_executed_source_identity_mismatch")
SEALER_SOURCE_IMAGE = _read_exact_source(
    SEALER_PATH, reason="sealer_source_import_invalid"
)
sealer = _execute_exact_source(
    SEALER_SOURCE_IMAGE,
    module_name="_fxstack_mtvclc_gap_v3_handoff_sealer",
)
HANDOFF_SOURCE_IMAGE = _self_exact_source_image()
LEGACY_HANDOFF_SOURCE_IMAGE = _read_exact_source(
    LEGACY_HANDOFF_PATH, reason="legacy_handoff_source_import_invalid"
)
HANDOFF_SCHEMA = "fxstack.scalp.mtvclc_capture_handoff.v2"
PROFILE_AUTO = "auto"
PROFILE_GAP_V3 = "gap_v3_anchored"
PROFILE_REPLACEMENT = PROFILE_GAP_V3
PROFILE_BASE = "unsupported_base"

PREREGISTRATION_SCHEMA = collector.PREREGISTRATION_SCHEMA_VERSION
COLLECTOR_SCHEMA = collector.COLLECTOR_SCHEMA_VERSION
REPLACEMENT_COLLECTOR_SCHEMA = collector.COLLECTOR_SCHEMA_VERSION
CHUNK_SCHEMA = collector.CHUNK_SCHEMA_VERSION
MANIFEST_SCHEMA = collector.MANIFEST_SCHEMA_VERSION
STRATEGY_ID = sealer.screen.STRATEGY_ID
STRATEGY_VERSION = sealer.screen.STRATEGY_VERSION
CONFIG_ID = sealer.screen.CONFIG_ID
SOURCE_CONTRACT_ID = collector.SOURCE_CONTRACT_ID
ACTIVITY_METRIC_ID = collector.ACTIVITY_METRIC_ID
SCOPE_VERSION = collector.SCOPE_VERSION
VENUE_ID = collector.VENUE_ID
TIMEFRAME = collector.TIMEFRAME
SYMBOLS = tuple(collector.SYMBOLS)
SYMBOL_SET = frozenset(SYMBOLS)
MINIMUM_M1_BARS = collector.MINIMUM_M1_BARS
PROSPECTIVE_WINDOW_DAYS = collector.PROSPECTIVE_WINDOW_DAYS
MANIFEST_FILENAME = collector.MANIFEST_FILENAME
ACTIVE_HOUR_JOURNAL_FILENAME = collector.ACTIVE_JOURNAL_FILENAME
DATA_WRITER_LOCK_FILENAME = collector.DATA_WRITER_LOCK_FILENAME
START_EDGE_RECEIPT_FILENAME = collector.START_EDGE_RECEIPT_FILENAME
TAIL_COMMITMENT_FILENAME = collector.TAIL_COMMITMENT_FILENAME
POST_WINDOW_FINALIZATION_FILENAME = collector.POST_WINDOW_FINALIZATION_FILENAME
CHUNK_DIRECTORY = "chunks"
GUARD_IDENTITY_FILENAME = "collector-guard.identity.gap-v3.v1.json"
SUPERVISION_DIRECTORY = "supervision-gap-v3"
SUPERVISOR_LOCK_FILENAME = "collector-writer.lock"
MAXIMUM_PREREGISTRATION_BYTES = collector.MAXIMUM_PREREGISTRATION_BYTES
MAXIMUM_MANIFEST_LINE_BYTES = collector.MAXIMUM_MANIFEST_LINE_BYTES
MAXIMUM_CHUNK_BYTES = collector.MAXIMUM_CHUNK_BYTES
MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES = (
    collector.MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES
)
ZERO_SHA256 = "0" * 64

FALSE_AUTHORITY = dict(collector.COLLECTION_AUTHORITY)
MANIFEST_FIELDS = frozenset(collector.support._MANIFEST_FIELDS)
CHUNK_FIELDS = frozenset(collector.support._PORTABLE_CHUNK_FIELDS)
HANDOFF_FIELDS = frozenset(
    {
        "schema_version",
        "strategy_id",
        "strategy_version",
        "config_id",
        "venue_id",
        "scope_version",
        "symbol_scope",
        "source_contract_id",
        "activity_metric_id",
        "capture_inventory",
        "capture_inventory_sha256",
        "window_closed",
        "manifest_and_chunks_verified",
        "outcome_evaluation_performed",
        "performance_statistics_computed",
        "research_only",
        "authority",
        "handoff_body_sha256",
    }
)
_GAP_INVENTORY_FIELDS = frozenset(
    {
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "gap_chain_schema_version",
        "gap_chain_count",
        "gap_event_count",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "start_edge_receipt_sha256",
        "post_window_finalization_receipt_artifact_sha256",
        "post_window_finalization_receipt_sha256",
        "first_cycle_durable_at_epoch",
        "post_window_finalized_after_end_observed_at_epoch",
        "upstream_producer_software_body_sha256",
        "bridge_ea_repository_source_identity",
        "bridge_ea_deployed_source_identity",
        "bridge_ea_deployed_ex4_identity",
        "capture_tail_commitment_proof",
    }
)
TAIL_COMMITMENT_PROOF_FIELDS = frozenset(
    {
        "status",
        "schema_version",
        "filename",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "artifact_sha256",
        "artifact_size_bytes",
        "record_count",
        "registry_sequence",
        "genesis_entry_sha256",
        "tail_entry_sha256",
        "committed_state_sha256",
        "committed_state_kind",
        "manifest_sequence",
        "manifest_entry_sha256",
        "manifest_size_bytes",
        "journal_manifest_sequence_target",
        "journal_sequence",
        "journal_entry_sha256",
        "journal_size_bytes",
        "covered_journal_manifest_sequence_target",
        "covered_journal_sequence",
        "covered_journal_entry_sha256",
        "covered_journal_size_bytes",
        "latest_chunk_path",
        "latest_chunk_sha256",
        "latest_chunk_size_bytes",
        "cycle_reservation_sequence",
        "gap_chain_count",
        "gap_event_count",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "last_cycle_reservation_sha256",
        "unresolved_cycle_reservation_sha256",
        "attempt_failure_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "physical_journal_present",
        "pending_operation",
    }
)
_TAIL_STATE_PROOF_FIELDS = (
    ("state_kind", "committed_state_kind"),
    ("manifest_sequence", "manifest_sequence"),
    ("manifest_entry_sha256", "manifest_entry_sha256"),
    ("manifest_size_bytes", "manifest_size_bytes"),
    ("journal_manifest_sequence_target", "journal_manifest_sequence_target"),
    ("journal_sequence", "journal_sequence"),
    ("journal_entry_sha256", "journal_entry_sha256"),
    ("journal_size_bytes", "journal_size_bytes"),
    (
        "covered_journal_manifest_sequence_target",
        "covered_journal_manifest_sequence_target",
    ),
    ("covered_journal_sequence", "covered_journal_sequence"),
    ("covered_journal_entry_sha256", "covered_journal_entry_sha256"),
    ("covered_journal_size_bytes", "covered_journal_size_bytes"),
    ("latest_chunk_path", "latest_chunk_path"),
    ("latest_chunk_sha256", "latest_chunk_sha256"),
    ("latest_chunk_size_bytes", "latest_chunk_size_bytes"),
    ("gap_chain_count", "gap_chain_count"),
    ("gap_event_count", "gap_event_count"),
    ("gap_root_sha256", "gap_root_sha256"),
    ("gap_tail_sha256", "gap_tail_sha256"),
    ("gap_source_id", "gap_source_id"),
    ("preregistration_body_sha256", "preregistration_body_sha256"),
    ("preregistration_artifact_sha256", "preregistration_artifact_sha256"),
    ("prospective_t0_utc_inclusive", "prospective_t0_utc_inclusive"),
    ("prospective_end_utc_exclusive", "prospective_end_utc_exclusive"),
    ("cycle_reservation_sequence", "cycle_reservation_sequence"),
    ("last_cycle_reservation_sha256", "last_cycle_reservation_sha256"),
    (
        "unresolved_cycle_reservation_sha256",
        "unresolved_cycle_reservation_sha256",
    ),
    ("attempt_failure_sha256", "attempt_failure_sha256"),
)
BASE_INVENTORY_FIELDS = frozenset(
    {
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "manifest_sha256",
        "manifest_head_sha256",
        "manifest_entries",
        "market_source_id",
        "segment_count",
        "bar_rows",
        "quote_rows",
        "bar_rows_by_symbol",
        "quote_rows_by_symbol",
        "first_bar_epoch_by_symbol",
        "first_quote_epoch_by_symbol",
        "last_quote_epoch_by_symbol",
        "last_bar_epoch_by_symbol",
        "maximum_transport_gap_seconds_by_symbol",
        "transport_gap_count_over_five_seconds_by_symbol",
        "referenced_chunk_files",
        "orphan_chunk_files",
        "guard_identity_sha256",
    }
)
INVENTORY_FIELDS = BASE_INVENTORY_FIELDS | _GAP_INVENTORY_FIELDS


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _positive_int(value: Any) -> int | None:
    parsed = _nonnegative_int(value)
    return parsed if parsed is not None and parsed > 0 else None


def _symbol_map_valid(
    value: Any,
    *,
    parser: Any,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != set(SYMBOLS):
        return False
    return all(parser(item) is not None for item in value.values())


def _nonnegative_finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return parsed if math.isfinite(parsed) and parsed >= 0.0 else None


def _tail_commitment_proof_valid(
    value: Any,
    *,
    binding: ProspectiveBinding,
    inventory: Mapping[str, Any] | None = None,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != TAIL_COMMITMENT_PROOF_FIELDS:
        return False
    positive_integer_fields = (
        "artifact_size_bytes",
        "record_count",
        "registry_sequence",
        "manifest_sequence",
        "manifest_size_bytes",
        "covered_journal_manifest_sequence_target",
        "covered_journal_sequence",
        "covered_journal_size_bytes",
        "latest_chunk_size_bytes",
        "cycle_reservation_sequence",
    )
    nonnegative_integer_fields = (
        "journal_manifest_sequence_target",
        "journal_sequence",
        "journal_size_bytes",
        "gap_chain_count",
        "gap_event_count",
    )
    if any(
        _positive_int(value.get(field)) is None for field in positive_integer_fields
    ) or any(
        _nonnegative_int(value.get(field)) is None
        for field in nonnegative_integer_fields
    ):
        return False
    hash_fields = (
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "artifact_sha256",
        "genesis_entry_sha256",
        "tail_entry_sha256",
        "committed_state_sha256",
        "manifest_entry_sha256",
        "journal_entry_sha256",
        "covered_journal_entry_sha256",
        "latest_chunk_sha256",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "last_cycle_reservation_sha256",
        "unresolved_cycle_reservation_sha256",
        "attempt_failure_sha256",
    )
    if any(not _is_sha256(value.get(field)) for field in hash_fields):
        return False
    relative = str(value.get("latest_chunk_path") or "")
    pure = PurePosixPath(relative)
    if (
        not relative
        or pure.is_absolute()
        or ".." in pure.parts
        or not pure.parts
        or pure.parts[0] != CHUNK_DIRECTORY
        or pure.as_posix() != relative
    ):
        return False
    record_count = int(value["record_count"])
    reservation_count = int(value["cycle_reservation_sequence"])
    gap_count = int(value["gap_chain_count"])
    gap_events = int(value["gap_event_count"])
    state = {
        state_field: value[proof_field]
        for state_field, proof_field in _TAIL_STATE_PROOF_FIELDS
    }
    if (
        value.get("status") != "valid"
        or value.get("schema_version") != collector.TAIL_COMMITMENT_SCHEMA_VERSION
        or value.get("filename") != TAIL_COMMITMENT_FILENAME
        or value.get("capture_integrity_contract_sha256")
        != binding.capture_integrity_contract_sha256
        or value.get("collector_source_sha256") != collector.MODULE_SOURCE_SHA256
        or value.get("collector_wrapper_source_sha256") != collector.SUPPORT_SHA256
        or value.get("collector_base_source_sha256")
        != collector.BASE_SUPPORT_SHA256
        or value.get("registry_sequence") != record_count
        or record_count < reservation_count + 5
        or value.get("committed_state_kind") != "manifest"
        or value.get("journal_manifest_sequence_target") != 0
        or value.get("journal_sequence") != 0
        or value.get("journal_entry_sha256") != ZERO_SHA256
        or value.get("journal_size_bytes") != 0
        or value.get("covered_journal_manifest_sequence_target")
        != value.get("manifest_sequence")
        or value.get("covered_journal_entry_sha256") == ZERO_SHA256
        or value.get("latest_chunk_sha256") == ZERO_SHA256
        or value.get("physical_journal_present") is not False
        or value.get("pending_operation") is not False
        or value.get("last_cycle_reservation_sha256") == ZERO_SHA256
        or value.get("unresolved_cycle_reservation_sha256") != ZERO_SHA256
        or value.get("attempt_failure_sha256") != ZERO_SHA256
        or value.get("preregistration_body_sha256")
        != binding.preregistration_body_sha256
        or value.get("preregistration_artifact_sha256")
        != binding.preregistration_artifact_sha256
        or value.get("prospective_t0_utc_inclusive") != binding.t0_utc
        or value.get("prospective_end_utc_exclusive")
        != binding.end_utc_exclusive
        or value.get("committed_state_sha256") != canonical_sha256(state)
        or gap_events < gap_count
        or (
            gap_count == 0
            and (
                gap_events != 0
                or value.get("gap_root_sha256") != ZERO_SHA256
                or value.get("gap_tail_sha256") != ZERO_SHA256
            )
        )
        or (
            gap_count > 0
            and (
                gap_events <= 0
                or value.get("gap_root_sha256") == ZERO_SHA256
                or value.get("gap_tail_sha256") == ZERO_SHA256
            )
        )
    ):
        return False
    return bool(
        inventory is None
        or (
            value.get("manifest_sequence") == inventory.get("manifest_entries")
            and value.get("manifest_entry_sha256")
            == inventory.get("manifest_head_sha256")
            and value.get("gap_chain_count") == inventory.get("gap_chain_count")
            and value.get("gap_event_count") == inventory.get("gap_event_count")
            and value.get("gap_root_sha256") == inventory.get("gap_root_sha256")
            and value.get("gap_tail_sha256") == inventory.get("gap_tail_sha256")
            and value.get("gap_source_id") == inventory.get("gap_source_id")
        )
    )


def _inventory_valid(
    value: Any,
    *,
    binding: ProspectiveBinding,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != INVENTORY_FIELDS:
        return False
    positive_fields = (
        "manifest_entries",
        "bar_rows",
        "quote_rows",
        "referenced_chunk_files",
    )
    nonnegative_fields = (
        "orphan_chunk_files",
        "gap_chain_count",
        "gap_event_count",
    )
    if (
        any(_positive_int(value.get(field)) is None for field in positive_fields)
        or any(
            _nonnegative_int(value.get(field)) is None for field in nonnegative_fields
        )
        or value.get("segment_count") != 1
        or value.get("orphan_chunk_files") != 0
        or value.get("referenced_chunk_files") != value.get("manifest_entries")
        or not _tail_commitment_proof_valid(
            value.get("capture_tail_commitment_proof"),
            binding=binding,
            inventory=value,
        )
    ):
        return False
    hash_fields = (
        "manifest_sha256",
        "manifest_head_sha256",
        "market_source_id",
        "guard_identity_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "start_edge_receipt_sha256",
        "post_window_finalization_receipt_artifact_sha256",
        "post_window_finalization_receipt_sha256",
    )
    if any(not _is_sha256(value.get(field)) for field in hash_fields):
        return False
    if (
        value.get("collector_source_sha256") != collector.MODULE_SOURCE_SHA256
        or value.get("collector_wrapper_source_sha256") != collector.SUPPORT_SHA256
        or value.get("collector_base_source_sha256") != collector.BASE_SUPPORT_SHA256
        or value.get("gap_chain_schema_version")
        != collector.LATE_GAP_RECORD_SCHEMA_VERSION
        or value.get("gap_source_id") != value.get("market_source_id")
        or any(
            value.get(field) != expected
            for field, expected in binding.producer_receipt_fields().items()
        )
    ):
        return False
    gap_count = int(value["gap_chain_count"])
    gap_events = int(value["gap_event_count"])
    if (
        (gap_count == 0)
        != (
            gap_events == 0
            and value.get("gap_root_sha256") == ZERO_SHA256
            and value.get("gap_tail_sha256") == ZERO_SHA256
        )
        or (gap_count > 0 and gap_events <= 0)
    ):
        return False
    int_maps = (
        "bar_rows_by_symbol",
        "quote_rows_by_symbol",
        "first_bar_epoch_by_symbol",
        "last_bar_epoch_by_symbol",
        "transport_gap_count_over_five_seconds_by_symbol",
    )
    float_maps = (
        "first_quote_epoch_by_symbol",
        "last_quote_epoch_by_symbol",
        "maximum_transport_gap_seconds_by_symbol",
    )
    if any(
        not _symbol_map_valid(value.get(field), parser=_nonnegative_int)
        for field in int_maps
    ) or any(
        not _symbol_map_valid(value.get(field), parser=_nonnegative_finite)
        for field in float_maps
    ):
        return False
    if (
        sum(value["bar_rows_by_symbol"].values()) != value.get("bar_rows")
        or sum(value["quote_rows_by_symbol"].values()) != value.get("quote_rows")
        or any(
            value["first_bar_epoch_by_symbol"][symbol]
            > value["last_bar_epoch_by_symbol"][symbol]
            for symbol in SYMBOLS
        )
        or any(
            float(value["first_quote_epoch_by_symbol"][symbol])
            > float(value["last_quote_epoch_by_symbol"][symbol])
            for symbol in SYMBOLS
        )
    ):
        return False
    durable = _nonnegative_finite(value.get("first_cycle_durable_at_epoch"))
    finalized = _nonnegative_finite(
        value.get("post_window_finalized_after_end_observed_at_epoch")
    )
    return bool(
        durable is not None
        and finalized is not None
        and binding.t0_epoch
        <= durable
        <= binding.t0_epoch + collector.MAXIMUM_START_EDGE_LAG_SECONDS
        and finalized >= binding.end_epoch_exclusive
    )


def _load_private_core() -> Any:
    name = "_fxstack_mtvclc_gap_v3_handoff_core"
    return _execute_exact_source(
        LEGACY_HANDOFF_SOURCE_IMAGE,
        module_name=name,
    )


_core = _load_private_core()
HandoffRefusal = _core.HandoffRefusal
canonical_json_bytes = _core.canonical_json_bytes
canonical_sha256 = _core.canonical_sha256
_strict_json_object = _core._strict_json_object
_is_sha256 = _core._is_sha256
_is_reparse_point = _core._is_reparse_point
_read_regular_file = _core._read_regular_file
_safe_chunk_path = _core._safe_chunk_path
_binding_tuple = _core._binding_tuple

_core.HANDOFF_SCHEMA = HANDOFF_SCHEMA
_core.PROFILE_REPLACEMENT = PROFILE_GAP_V3
_core.REPLACEMENT_COLLECTOR_SCHEMA = COLLECTOR_SCHEMA
_core.CHUNK_SCHEMA = CHUNK_SCHEMA
_core.MANIFEST_SCHEMA = MANIFEST_SCHEMA
_core.MANIFEST_FIELDS = MANIFEST_FIELDS
_core.CHUNK_FIELDS = CHUNK_FIELDS
_core.ACTIVE_HOUR_JOURNAL_FILENAME = ACTIVE_HOUR_JOURNAL_FILENAME
_core.GUARD_IDENTITY_FILENAME = GUARD_IDENTITY_FILENAME
_core.SUPERVISION_DIRECTORY = SUPERVISION_DIRECTORY
_core.FALSE_AUTHORITY = dict(FALSE_AUTHORITY)


def executed_source_identities() -> dict[str, dict[str, Any]]:
    """Return exact local source images executed by this handoff boundary."""

    identities = {
        **sealer.executed_source_identities(),
        "collector_source": COLLECTOR_SOURCE_IMAGE.identity(),
        "collector_support_source": {
            "filename": collector.SUPPORT_PATH.name,
            "sha256": collector.SUPPORT_SHA256,
            "size_bytes": collector.SUPPORT_SIZE_BYTES,
        },
        "collector_base_source": {
            "filename": collector.BASE_SUPPORT_PATH.name,
            "sha256": collector.BASE_SUPPORT_SHA256,
            "size_bytes": collector.BASE_SUPPORT_SIZE_BYTES,
        },
        "sealer_source": SEALER_SOURCE_IMAGE.identity(),
        "handoff_verifier_source": HANDOFF_SOURCE_IMAGE.identity(),
        "legacy_handoff_support_source": LEGACY_HANDOFF_SOURCE_IMAGE.identity(),
    }
    collector_identities = getattr(collector, "executed_source_identities", None)
    if callable(collector_identities):
        identities.update(collector_identities())
    return identities


def _identity_matches_image(value: Any, image: ExactSourceImage) -> bool:
    return isinstance(value, Mapping) and dict(value) == image.identity()


def _assert_executed_sources_unchanged() -> None:
    paths = sealer._source_paths()
    for label, identity in executed_source_identities().items():
        path = paths.get(label)
        if path is None:
            raise HandoffRefusal(f"executed_source_path_missing:{label}")
        try:
            current = _read_exact_source(
                path,
                reason=f"executed_source_changed:{label}",
            )
        except RuntimeError as exc:
            raise HandoffRefusal(f"executed_source_changed:{label}") from exc
        if current.identity() != identity:
            raise HandoffRefusal(f"executed_source_changed:{label}")


def _validate_executable_source_identities(identities: Mapping[str, Any]) -> None:
    required = set(sealer.REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS)
    source_paths = sealer._source_paths()
    if not required.issubset(identities) or not required.issubset(source_paths):
        raise HandoffRefusal("preregistration_executable_identity_incomplete")
    for label in sorted(required):
        try:
            current = _read_exact_source(
                source_paths[label],
                reason=f"executable_source_changed:{label}",
            )
        except RuntimeError as exc:
            raise HandoffRefusal(f"executable_source_changed:{label}") from exc
        if not _identity_matches_image(identities.get(label), current):
            raise HandoffRefusal(f"executable_source_identity_mismatch:{label}")

    executed = executed_source_identities()
    for label, identity in executed.items():
        if dict(identities.get(label) or {}) != dict(identity):
            raise HandoffRefusal(f"executed_source_identity_mismatch:{label}")


@dataclass(frozen=True, slots=True)
class ProspectiveBinding:
    preregistration_path: Path
    preregistration_body_sha256: str
    preregistration_artifact_sha256: str
    t0_utc: str
    end_utc_exclusive: str
    t0_epoch: float
    end_epoch_exclusive: float
    upstream_producer_software_body_sha256: str
    bridge_ea_repository_source_identity: dict[str, Any]
    bridge_ea_deployed_source_identity: dict[str, Any]
    bridge_ea_deployed_ex4_identity: dict[str, Any]
    collector_binding: Any
    profile: str = PROFILE_GAP_V3
    collector_schema_version: str = COLLECTOR_SCHEMA
    collector_source_sha256: str = ""
    collector_support_source_sha256: str = ""
    capture_integrity_contract_sha256: str = ""

    @property
    def tuple(self) -> tuple[str, str, str, str]:
        return (
            self.preregistration_body_sha256,
            self.preregistration_artifact_sha256,
            self.t0_utc,
            self.end_utc_exclusive,
        )

    def producer_receipt_fields(self) -> dict[str, Any]:
        return {
            "upstream_producer_software_body_sha256": (
                self.upstream_producer_software_body_sha256
            ),
            "bridge_ea_repository_source_identity": dict(
                self.bridge_ea_repository_source_identity
            ),
            "bridge_ea_deployed_source_identity": dict(
                self.bridge_ea_deployed_source_identity
            ),
            "bridge_ea_deployed_ex4_identity": dict(
                self.bridge_ea_deployed_ex4_identity
            ),
        }


def load_preregistration(
    path: str | Path,
    *,
    profile: str = PROFILE_AUTO,
) -> ProspectiveBinding:
    if profile not in {PROFILE_AUTO, PROFILE_GAP_V3}:
        raise HandoffRefusal("preregistration_profile_invalid")
    target, raw = _read_regular_file(
        path,
        reason="preregistration_file_invalid",
        maximum_bytes=MAXIMUM_PREREGISTRATION_BYTES,
    )
    payload = _strict_json_object(raw, reason="preregistration_json_invalid")
    if not sealer.validate_preregistration(payload):
        raise HandoffRefusal("preregistration_contract_invalid")
    claimed = str(payload.get("preregistration_body_sha256") or "").lower()
    if target.name != f"mtvclc_gap_v3_preregistration_{claimed}.json":
        raise HandoffRefusal("preregistration_content_addressed_name_invalid")
    identities = payload.get("source_identities")
    integrity = payload.get("capture_integrity_contract")
    if not isinstance(identities, Mapping) or not isinstance(integrity, Mapping):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    _validate_executable_source_identities(identities)
    try:
        loaded = collector.load_preregistration(target)
    except collector.CollectionRefusal as exc:
        raise HandoffRefusal("collector_preregistration_handshake_invalid") from exc
    collector_identity = identities.get("collector_source")
    wrapper_identity = identities.get("collector_support_source")
    base_identity = identities.get("collector_base_source")
    if (
        not isinstance(collector_identity, Mapping)
        or not isinstance(wrapper_identity, Mapping)
        or not isinstance(base_identity, Mapping)
        or collector_identity.get("sha256") != collector.MODULE_SOURCE_SHA256
        or collector_identity.get("size_bytes") != collector.MODULE_SOURCE_SIZE_BYTES
        or wrapper_identity.get("sha256") != collector.SUPPORT_SHA256
        or wrapper_identity.get("size_bytes") != collector.SUPPORT_SIZE_BYTES
        or base_identity.get("sha256") != collector.BASE_SUPPORT_SHA256
        or base_identity.get("size_bytes") != collector.BASE_SUPPORT_SIZE_BYTES
    ):
        raise HandoffRefusal("preregistration_identity_contract_invalid")
    try:
        producer_fields = loaded.producer_receipt_fields()
    except (AttributeError, KeyError, TypeError, collector.CollectionRefusal) as exc:
        raise HandoffRefusal("preregistration_producer_contract_invalid") from exc
    return ProspectiveBinding(
        preregistration_path=target,
        preregistration_body_sha256=loaded.preregistration_body_sha256,
        preregistration_artifact_sha256=loaded.preregistration_artifact_sha256,
        t0_utc=loaded.t0_utc,
        end_utc_exclusive=loaded.end_utc_exclusive,
        t0_epoch=loaded.t0_epoch,
        end_epoch_exclusive=loaded.end_epoch_exclusive,
        upstream_producer_software_body_sha256=str(
            producer_fields["upstream_producer_software_body_sha256"]
        ),
        bridge_ea_repository_source_identity=dict(
            producer_fields["bridge_ea_repository_source_identity"]
        ),
        bridge_ea_deployed_source_identity=dict(
            producer_fields["bridge_ea_deployed_source_identity"]
        ),
        bridge_ea_deployed_ex4_identity=dict(
            producer_fields["bridge_ea_deployed_ex4_identity"]
        ),
        collector_binding=loaded,
        collector_source_sha256=str(collector_identity["sha256"]),
        collector_support_source_sha256=str(wrapper_identity["sha256"]),
        capture_integrity_contract_sha256=canonical_sha256(integrity),
    )


def _regular_file_sha256(path: Path, *, reason: str, limit: int) -> tuple[bytes, str]:
    _resolved, raw = _read_regular_file(path, reason=reason, maximum_bytes=limit)
    return raw, hashlib.sha256(raw).hexdigest()


def _validate_guard_identity(path: Path, *, binding: ProspectiveBinding) -> str:
    raw, digest = _regular_file_sha256(
        path,
        reason="capture_guard_identity_invalid",
        limit=MAXIMUM_PREREGISTRATION_BYTES,
    )
    if not raw.endswith(b"\n"):
        raise HandoffRefusal("capture_guard_identity_not_canonical")
    payload = _strict_json_object(raw[:-1], reason="capture_guard_identity_invalid")
    if raw != canonical_json_bytes(payload) + b"\n":
        raise HandoffRefusal("capture_guard_identity_not_canonical")
    required = {
        "collector_source_sha256": binding.collector_source_sha256,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        **binding.producer_receipt_fields(),
    }
    if any(payload.get(key) != value for key, value in required.items()):
        raise HandoffRefusal("capture_guard_identity_contract_invalid")
    for key, value in payload.items():
        lowered = key.lower()
        if (
            any(token in lowered for token in ("authorized", "authority_granted"))
            and key != "collection_only"
            and value is not False
        ):
            raise HandoffRefusal("capture_guard_authority_invalid")
    if payload.get("collection_only") is not True:
        raise HandoffRefusal("capture_guard_identity_contract_invalid")
    return digest


def _validate_start_receipt(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> tuple[str, float]:
    receipt_path = root / START_EDGE_RECEIPT_FILENAME
    raw, digest = _regular_file_sha256(
        receipt_path,
        reason="start_edge_durable_receipt_invalid",
        limit=MAXIMUM_PREREGISTRATION_BYTES,
    )
    if not raw.endswith(b"\n"):
        raise HandoffRefusal("start_edge_durable_receipt_not_canonical")
    value = _strict_json_object(raw[:-1], reason="start_edge_durable_receipt_invalid")
    if raw != canonical_json_bytes(value) + b"\n":
        raise HandoffRefusal("start_edge_durable_receipt_not_canonical")
    body = dict(value)
    claimed = str(body.pop("receipt_sha256", "")).lower()
    manifest_path = root / MANIFEST_FILENAME
    try:
        with manifest_path.open("rb") as handle:
            first_line = handle.readline(MAXIMUM_MANIFEST_LINE_BYTES + 1)
    except (OSError, StopIteration) as exc:
        raise HandoffRefusal("start_edge_full_scope_proof_missing") from exc
    if (
        not first_line.endswith(b"\n")
        or len(first_line) > MAXIMUM_MANIFEST_LINE_BYTES
    ):
        raise HandoffRefusal("start_edge_full_scope_proof_missing")
    first = _strict_json_object(first_line[:-1], reason="capture_manifest_invalid")
    if first_line != canonical_json_bytes(first) + b"\n":
        raise HandoffRefusal("start_edge_full_scope_proof_missing")
    relative = str(first.get("chunk_path") or "")
    chunk_path = _safe_chunk_path(root, relative, expected=relative)
    chunk_raw, chunk_sha = _regular_file_sha256(
        chunk_path,
        reason="start_edge_full_scope_proof_missing",
        limit=MAXIMUM_CHUNK_BYTES,
    )
    chunk = _strict_json_object(chunk_raw, reason="start_edge_full_scope_proof_invalid")
    bars = chunk.get("bars")
    quotes = chunk.get("quotes")
    if not isinstance(bars, list) or not isinstance(quotes, list):
        raise HandoffRefusal("start_edge_full_scope_proof_invalid")
    bar_counts = {symbol: 0 for symbol in SYMBOLS}
    for row in bars:
        symbol = str(row.get("symbol") or "") if isinstance(row, Mapping) else ""
        if symbol not in bar_counts:
            raise HandoffRefusal("start_edge_full_scope_proof_invalid")
        bar_counts[symbol] += 1
    quote_symbols = {
        str(row.get("symbol") or "") for row in quotes if isinstance(row, Mapping)
    }
    durable = _core._positive(
        value.get("first_cycle_durable_at_epoch"),
        "start_edge_durable_receipt_invalid",
    )
    expected = {
        "schema_version": collector.START_EDGE_RECEIPT_SCHEMA_VERSION,
        "capture_integrity_contract_sha256": binding.capture_integrity_contract_sha256,
        "collector_source_sha256": collector.MODULE_SOURCE_SHA256,
        "collector_wrapper_source_sha256": collector.SUPPORT_SHA256,
        "collector_base_source_sha256": collector.BASE_SUPPORT_SHA256,
        "preregistration_body_sha256": binding.preregistration_body_sha256,
        "preregistration_artifact_sha256": binding.preregistration_artifact_sha256,
        "prospective_t0_utc_inclusive": binding.t0_utc,
        "prospective_end_utc_exclusive": binding.end_utc_exclusive,
        **binding.producer_receipt_fields(),
        "first_manifest_entry_sha256": first.get("manifest_entry_sha256"),
        "first_chunk_sha256": chunk_sha,
        "first_cycle_durable_at_epoch": durable,
    }
    if (
        body != expected
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
        or any(count < MINIMUM_M1_BARS for count in bar_counts.values())
        or quote_symbols != SYMBOL_SET
        or durable < binding.t0_epoch
        or durable > binding.t0_epoch + collector.MAXIMUM_START_EDGE_LAG_SECONDS
    ):
        raise HandoffRefusal("start_edge_durable_receipt_invalid")
    return digest, durable


def _producer_monitor_proof_valid(
    value: Any,
    *,
    binding: ProspectiveBinding,
) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "producer_software_body_sha256",
        "repository_source",
        "deployed_source",
        "deployed_ex4",
        "runtime_or_broker_authority_derived",
        "monitor_proof_sha256",
    }:
        return False
    expected_rows = {
        "repository_source": binding.bridge_ea_repository_source_identity,
        "deployed_source": binding.bridge_ea_deployed_source_identity,
        "deployed_ex4": binding.bridge_ea_deployed_ex4_identity,
    }
    observed_paths: list[str] = []
    for label, expected in expected_rows.items():
        row = value.get(label)
        if not isinstance(row, Mapping) or set(row) != {
            "path",
            "stat_identity",
            "sha256",
            "size_bytes",
        }:
            return False
        path = str(row.get("path") or "")
        stat_identity = row.get("stat_identity")
        if (
            not path
            or not Path(path).is_absolute()
            or not isinstance(stat_identity, list)
            or len(stat_identity) != 5
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in stat_identity
            )
            or row.get("sha256") != expected.get("sha256")
            or row.get("size_bytes") != expected.get("size_bytes")
            or Path(path).name != expected.get("filename")
        ):
            return False
        observed_paths.append(os.path.normcase(path))
    body = dict(value)
    claimed = str(body.pop("monitor_proof_sha256", "")).lower()
    return bool(
        len(set(observed_paths)) == len(observed_paths)
        and value.get("schema_version")
        == "fxstack.scalp.mtvclc_producer_monitor_proof.v1"
        and value.get("producer_software_body_sha256")
        == binding.upstream_producer_software_body_sha256
        and value.get("runtime_or_broker_authority_derived") is False
        and _is_sha256(claimed)
        and canonical_sha256(body) == claimed
    )


def _validate_post_window_finalization(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> dict[str, Any]:
    path = root / POST_WINDOW_FINALIZATION_FILENAME
    raw, artifact_sha256 = _regular_file_sha256(
        path,
        reason="post_window_finalization_receipt_invalid",
        limit=MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES,
    )
    if not raw.endswith(b"\n"):
        raise HandoffRefusal("post_window_finalization_receipt_not_canonical")
    receipt = _strict_json_object(
        raw[:-1], reason="post_window_finalization_receipt_invalid"
    )
    if (
        raw != canonical_json_bytes(receipt) + b"\n"
        or not _producer_monitor_proof_valid(
            receipt.get("producer_monitor_proof"),
            binding=binding,
        )
    ):
        raise HandoffRefusal("post_window_finalization_receipt_invalid")
    try:
        view = collector._stream_tail_commitment_registry(
            root / TAIL_COMMITMENT_FILENAME
        )
        manifest = collector._stream_manifest_projection(root)
        start_raw, _start_sha256 = _regular_file_sha256(
            root / START_EDGE_RECEIPT_FILENAME,
            reason="start_edge_durable_receipt_invalid",
            limit=MAXIMUM_PREREGISTRATION_BYTES,
        )
        validated = collector._validated_post_window_finalization_receipt(
            receipt,
            binding=binding.collector_binding,
            view=view,
            manifest=manifest,
            start_receipt_artifact_sha256=hashlib.sha256(start_raw).hexdigest(),
            producer_monitor_proof=receipt["producer_monitor_proof"],
        )
    except (collector.CollectionRefusal, KeyError, TypeError, ValueError) as exc:
        raise HandoffRefusal("post_window_finalization_receipt_invalid") from exc
    state = view.committed_state
    if (
        view.pending_record is not None
        or state.get("unresolved_cycle_reservation_sha256") != ZERO_SHA256
        or state.get("attempt_failure_sha256") != ZERO_SHA256
    ):
        raise HandoffRefusal("post_window_finalization_tail_unresolved")
    return {
        "post_window_finalization_receipt_artifact_sha256": artifact_sha256,
        "post_window_finalization_receipt_sha256": str(
            validated["receipt_sha256"]
        ).lower(),
        "post_window_finalized_after_end_observed_at_epoch": float(
            validated["finalized_after_end_observed_at_epoch"]
        ),
    }


def _validate_tail_commitment(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> dict[str, Any]:
    try:
        proof = collector.validate_tail_commitment_registry(root)
    except collector.CollectionRefusal as exc:
        raise HandoffRefusal("capture_tail_commitment_invalid") from exc
    if not _tail_commitment_proof_valid(proof, binding=binding):
        raise HandoffRefusal("capture_tail_commitment_contract_invalid")
    return dict(proof)


def _tail_proof_matches_gap_anchor(
    proof: Mapping[str, Any],
    anchor: collector.GapAnchor,
) -> bool:
    return bool(
        proof.get("gap_chain_count") == anchor.count
        and proof.get("gap_event_count") == anchor.event_count
        and proof.get("gap_root_sha256") == anchor.root_hash
        and proof.get("gap_tail_sha256") == anchor.tail_hash
        and proof.get("gap_source_id") == anchor.source_id
    )


def _validate_final_state(root: Path, *, binding: ProspectiveBinding) -> str:
    active = root / ACTIVE_HOUR_JOURNAL_FILENAME
    if active.exists() or active.is_symlink():
        raise HandoffRefusal("capture_active_hour_journal_present")
    required = {
        MANIFEST_FILENAME,
        CHUNK_DIRECTORY,
        DATA_WRITER_LOCK_FILENAME,
        GUARD_IDENTITY_FILENAME,
        START_EDGE_RECEIPT_FILENAME,
        TAIL_COMMITMENT_FILENAME,
        POST_WINDOW_FINALIZATION_FILENAME,
    }
    allowed = required | {SUPERVISION_DIRECTORY}
    try:
        children = {child.name: child for child in root.iterdir()}
    except OSError as exc:
        raise HandoffRefusal("capture_root_unreadable") from exc
    if not required.issubset(children) or not set(children).issubset(allowed):
        raise HandoffRefusal("capture_final_topology_invalid")
    data_lock = _core._regular_metadata_path(
        children[DATA_WRITER_LOCK_FILENAME], reason="capture_data_writer_lock_invalid"
    )
    _core._probe_byte_lock_available(data_lock)
    try:
        if data_lock.read_bytes() != b"\0":
            raise HandoffRefusal("capture_data_writer_lock_invalid")
    except OSError as exc:
        raise HandoffRefusal("capture_data_writer_lock_invalid") from exc
    _validate_tail_commitment(root, binding=binding)
    guard_sha = _validate_guard_identity(
        children[GUARD_IDENTITY_FILENAME], binding=binding
    )
    supervision = children.get(SUPERVISION_DIRECTORY)
    if supervision is not None:
        if (
            not supervision.is_dir()
            or supervision.is_symlink()
            or _is_reparse_point(supervision)
        ):
            raise HandoffRefusal("capture_supervision_topology_invalid")
        lock = supervision / SUPERVISOR_LOCK_FILENAME
        if lock.exists():
            _core._probe_supervisor_lock_available(lock)
    _validate_start_receipt(root, binding=binding)
    _validate_post_window_finalization(root, binding=binding)
    return guard_sha


def _validate_gap_chain_snapshot(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> tuple[collector.GapAnchor, str]:
    anchor = collector.GapAnchor()
    bar_keys: set[tuple[str, int]] = set()
    gap_keys: set[tuple[str, int]] = set()
    capture_digest = hashlib.sha256()
    manifest_entries = 0
    manifest_path = root / MANIFEST_FILENAME
    try:
        handle = manifest_path.open("rb")
    except OSError as exc:
        raise HandoffRefusal("capture_manifest_unreadable") from exc
    with handle:
        for raw_line in handle:
            if (
                not raw_line.endswith(b"\n")
                or len(raw_line) > MAXIMUM_MANIFEST_LINE_BYTES
            ):
                raise HandoffRefusal("capture_manifest_invalid")
            manifest_entries += 1
            capture_digest.update(len(raw_line).to_bytes(8, "big"))
            capture_digest.update(raw_line)
            entry = _strict_json_object(
                raw_line[:-1], reason="capture_manifest_invalid"
            )
            relative = str(entry.get("chunk_path") or "")
            chunk_path = _safe_chunk_path(root, relative, expected=relative)
            chunk_raw, _chunk_sha = _regular_file_sha256(
                chunk_path,
                reason="manifest_chunk_unreadable",
                limit=MAXIMUM_CHUNK_BYTES,
            )
            relative_raw = relative.encode("utf-8")
            capture_digest.update(len(relative_raw).to_bytes(8, "big"))
            capture_digest.update(relative_raw)
            capture_digest.update(len(chunk_raw).to_bytes(8, "big"))
            capture_digest.update(chunk_raw)
            chunk = _strict_json_object(chunk_raw, reason="manifest_chunk_json_invalid")
            for row in chunk.get("bars") or []:
                if not isinstance(row, Mapping):
                    raise HandoffRefusal("capture_chunk_rows_invalid")
                bar_keys.add(
                    (
                        str(row.get("symbol") or ""),
                        int(row.get("minute_epoch") or 0),
                    )
                )
            records = chunk.get("late_gap_records")
            if not isinstance(records, list):
                raise HandoffRefusal("gap_cycle_invalid")
            try:
                anchor = collector._advance_gap_anchor(
                    anchor,
                    records,
                    expected_source_id=str(entry.get("market_source_id") or ""),
                    binding=binding.tuple,
                    event_validator=lambda event: gap_keys.add(
                        (
                            str(event.get("symbol") or ""),
                            int(event.get("minute_epoch") or 0),
                        )
                    ),
                )
                chunk_anchor = collector._anchor_from_mapping(
                    chunk, reason="gap_cycle_anchor_invalid"
                )
                entry_anchor = collector._anchor_from_mapping(
                    entry, reason="gap_manifest_anchor_invalid"
                )
            except collector.CollectionRefusal as exc:
                raise HandoffRefusal("gap_chain_invalid") from exc
            if chunk_anchor != anchor or entry_anchor != anchor:
                raise HandoffRefusal("gap_chain_rollback_or_truncation_detected")
    if manifest_entries <= 0:
        raise HandoffRefusal("capture_manifest_empty")
    if _core._count_exact_chunk_tree(root) != manifest_entries:
        raise HandoffRefusal("capture_orphan_or_invalid_chunk_detected")
    if len(gap_keys) != anchor.event_count or bar_keys & gap_keys:
        raise HandoffRefusal("gap_event_backfilled_or_duplicated")
    return anchor, capture_digest.hexdigest()


def _validate_gap_chain(
    root: Path,
    *,
    binding: ProspectiveBinding,
) -> collector.GapAnchor:
    anchor, _capture_sha256 = _validate_gap_chain_snapshot(root, binding=binding)
    return anchor


_core.load_preregistration = load_preregistration
_core._validate_replacement_capture_final_state = _validate_final_state


def verify_capture_handoff(
    *,
    preregistration_path: str | Path,
    capture_root: str | Path,
    now_epoch: float | None = None,
    profile: str = PROFILE_AUTO,
) -> dict[str, Any]:
    if profile not in {PROFILE_AUTO, PROFILE_GAP_V3}:
        raise HandoffRefusal("preregistration_profile_invalid")
    binding = load_preregistration(preregistration_path, profile=profile)
    candidate = Path(capture_root).expanduser()
    if candidate.is_symlink() or _is_reparse_point(candidate):
        raise HandoffRefusal("capture_root_invalid")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise HandoffRefusal("capture_root_invalid") from exc
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
        raise HandoffRefusal("capture_root_invalid")
    first_guard = _validate_final_state(root, binding=binding)
    first_tail_proof = _validate_tail_commitment(root, binding=binding)
    first_receipt, durable_at = _validate_start_receipt(root, binding=binding)
    first_finalization = _validate_post_window_finalization(root, binding=binding)
    first_anchor, first_capture_sha256 = _validate_gap_chain_snapshot(
        root, binding=binding
    )
    if not _tail_proof_matches_gap_anchor(first_tail_proof, first_anchor):
        raise HandoffRefusal("capture_tail_gap_anchor_mismatch")
    result = _core.verify_capture_handoff(
        preregistration_path=preregistration_path,
        capture_root=capture_root,
        now_epoch=now_epoch,
        profile=PROFILE_GAP_V3,
    )
    second_anchor, second_capture_sha256 = _validate_gap_chain_snapshot(
        root, binding=binding
    )
    second_receipt, second_durable = _validate_start_receipt(root, binding=binding)
    second_tail_proof = _validate_tail_commitment(root, binding=binding)
    second_finalization = _validate_post_window_finalization(root, binding=binding)
    second_guard = _validate_final_state(root, binding=binding)
    if (
        first_anchor != second_anchor
        or first_capture_sha256 != second_capture_sha256
        or first_receipt != second_receipt
        or durable_at != second_durable
        or first_guard != second_guard
        or first_tail_proof != second_tail_proof
        or first_finalization != second_finalization
        or not _tail_proof_matches_gap_anchor(second_tail_proof, second_anchor)
    ):
        raise HandoffRefusal("capture_changed_during_handoff")
    inventory = dict(result["capture_inventory"])
    inventory.update(
        {
            "collector_source_sha256": collector.MODULE_SOURCE_SHA256,
            "collector_wrapper_source_sha256": collector.SUPPORT_SHA256,
            "collector_base_source_sha256": collector.BASE_SUPPORT_SHA256,
            "gap_chain_schema_version": collector.LATE_GAP_RECORD_SCHEMA_VERSION,
            "gap_chain_count": first_anchor.count,
            "gap_event_count": first_anchor.event_count,
            "gap_root_sha256": first_anchor.root_hash,
            "gap_tail_sha256": first_anchor.tail_hash,
            "gap_source_id": first_anchor.source_id,
            "start_edge_receipt_sha256": first_receipt,
            "first_cycle_durable_at_epoch": durable_at,
            **first_finalization,
            **binding.producer_receipt_fields(),
            "capture_tail_commitment_proof": first_tail_proof,
        }
    )
    result["schema_version"] = HANDOFF_SCHEMA
    result["capture_inventory"] = inventory
    result["capture_inventory_sha256"] = canonical_sha256(inventory)
    result.pop("handoff_body_sha256", None)
    result["handoff_body_sha256"] = canonical_sha256(result)
    return result


def load_handoff_artifact(
    path: str | Path,
    *,
    binding: ProspectiveBinding,
) -> tuple[dict[str, Any], str]:
    target, raw = _read_regular_file(
        path, reason="capture_handoff_invalid", maximum_bytes=4 * 1024 * 1024
    )
    payload = _strict_json_object(raw, reason="capture_handoff_invalid")
    body = dict(payload)
    claimed = str(body.pop("handoff_body_sha256", "")).lower()
    inventory = payload.get("capture_inventory")
    if (
        set(payload) != HANDOFF_FIELDS
        or payload.get("schema_version") != HANDOFF_SCHEMA
        or payload.get("strategy_id") != STRATEGY_ID
        or payload.get("strategy_version") != STRATEGY_VERSION
        or payload.get("config_id") != CONFIG_ID
        or payload.get("venue_id") != VENUE_ID
        or payload.get("scope_version") != SCOPE_VERSION
        or payload.get("symbol_scope") != list(SYMBOLS)
        or payload.get("source_contract_id") != SOURCE_CONTRACT_ID
        or payload.get("activity_metric_id") != ACTIVITY_METRIC_ID
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
        or target.name != f"mtvclc_capture_handoff_v2_{claimed}.json"
        or raw != canonical_json_bytes(payload) + b"\n"
        or not isinstance(inventory, Mapping)
        or set(inventory) != INVENTORY_FIELDS
        or payload.get("capture_inventory_sha256") != canonical_sha256(inventory)
        or _binding_tuple(inventory, "capture_handoff_binding_invalid")
        != binding.tuple
        or payload.get("authority") != FALSE_AUTHORITY
        or payload.get("manifest_and_chunks_verified") is not True
        or payload.get("window_closed") is not True
        or payload.get("outcome_evaluation_performed") is not False
        or payload.get("performance_statistics_computed") is not False
        or payload.get("research_only") is not True
        or not _inventory_valid(inventory, binding=binding)
    ):
        raise HandoffRefusal("capture_handoff_contract_invalid")
    return payload, hashlib.sha256(raw).hexdigest()


def publish_handoff(*, output_root: str | Path, handoff: Mapping[str, Any]) -> Path:
    _assert_executed_sources_unchanged()
    if not isinstance(handoff, Mapping) or set(handoff) != HANDOFF_FIELDS:
        raise HandoffRefusal("handoff_contract_invalid")
    claimed = str(handoff.get("handoff_body_sha256") or "").lower()
    body = dict(handoff)
    body.pop("handoff_body_sha256", None)
    if not _is_sha256(claimed) or canonical_sha256(body) != claimed:
        raise HandoffRefusal("handoff_contract_invalid")
    root = Path(output_root).expanduser().resolve(strict=True)
    if not root.is_dir() or root.is_symlink() or _is_reparse_point(root):
        raise HandoffRefusal("handoff_output_root_invalid")
    target = root / f"mtvclc_capture_handoff_v2_{claimed}.json"
    if target.exists() or target.is_symlink():
        raise HandoffRefusal("handoff_output_exists")
    encoded = canonical_json_bytes(handoff) + b"\n"
    temp = root / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        descriptor = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temp, target)
        temp.unlink()
        os.chmod(target, 0o400)
    except OSError as exc:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise HandoffRefusal("handoff_atomic_publish_failed") from exc
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify a completed anchored gap-v3 MTVCLC capture offline."
    )
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--capture-root", required=True)
    parser.add_argument("--output-root", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        value = verify_capture_handoff(
            preregistration_path=args.preregistration,
            capture_root=args.capture_root,
        )
        output = publish_handoff(output_root=args.output_root, handoff=value)
    except (HandoffRefusal, OSError) as exc:
        print(f"gap-v3 handoff refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "handoff_body_sha256": value["handoff_body_sha256"],
                "research_only": True,
                "authority_granted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
