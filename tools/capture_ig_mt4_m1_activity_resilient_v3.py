"""Collect prospective IG-MT4 M1 inputs with one anchored durability chain.

This revision removes the independently mutable late-gap sidecar.  Every
permanent late-gap declaration and its compact chain anchor is part of the
authenticated cycle, the fsynced active journal, the immutable hourly chunk,
and the manifest entry that hashes that chunk.  An independent append-only
tail WAL detects rollback of that main chain relative to its committed state
and makes each declared crash window exactly recoverable.

The module is collection-only and GET-only.  It has no signal, outcome,
performance, signing, activation-registry, runtime, command, or trade path.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import time
import zlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

# AGENT: ROLE: versioned GET-only MTVCLC prospective input collector.
# AGENT: HANDSHAKE: exact preregistration -> authenticated GETs -> anchored journal/chunk/manifest.
# AGENT: ISOLATION: no signal, evaluation, issuer, activation, runtime, command, or trade authority.


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

SUPPORT_PATH = (
    REPOSITORY_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient.py"
).resolve()
BASE_SUPPORT_PATH = (
    REPOSITORY_ROOT / "tools" / "capture_ig_mt4_m1_activity.py"
).resolve()
PINNED_SUPPORT_SHA256 = (
    "b38e69933e4910956dc92477252d33f4d5a92458e70036520654bc53102357f5"
)
PINNED_SUPPORT_SIZE_BYTES = 77_496
PINNED_BASE_SUPPORT_SHA256 = (
    "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d"
)
PINNED_BASE_SUPPORT_SIZE_BYTES = 80_405
MAXIMUM_COLLECTOR_SOURCE_BYTES = 4 * 1024 * 1024


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _early_reparse_or_symlink(path: Path, value: os.stat_result) -> bool:
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return stat.S_ISLNK(value.st_mode) or bool(attributes & reparse_flag)


def _read_bounded_source_descriptor(
    path: Path,
    *,
    maximum_bytes: int,
    expected_size: int | None,
) -> tuple[bytes, tuple[int, int, int, int]]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    before_path = os.lstat(path)
    if (
        _early_reparse_or_symlink(path, before_path)
        or not stat.S_ISREG(before_path.st_mode)
        or before_path.st_size <= 0
        or before_path.st_size > maximum_bytes
        or (expected_size is not None and before_path.st_size != expected_size)
    ):
        raise OSError("source file identity invalid")
    descriptor = os.open(path, flags)
    try:
        before_handle = os.fstat(descriptor)
        if _stat_identity(before_path) != _stat_identity(before_handle):
            raise OSError("source file identity changed")
        payload = bytearray()
        remaining = int(before_handle.st_size)
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                raise OSError("source file short read")
            payload.extend(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise OSError("source file grew while reading")
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    after_path = os.lstat(path)
    identities = {
        _stat_identity(before_path),
        _stat_identity(before_handle),
        _stat_identity(after_handle),
        _stat_identity(after_path),
    }
    if len(identities) != 1 or len(payload) != before_handle.st_size:
        raise OSError("source file identity changed")
    return bytes(payload), identities.pop()


def _read_stable_source(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    reason: str,
) -> tuple[bytes, tuple[int, int, int, int]]:
    try:
        raw, identity = _read_bounded_source_descriptor(
            path,
            maximum_bytes=expected_size,
            expected_size=expected_size,
        )
    except OSError as exc:  # pragma: no cover - import cannot proceed safely
        raise RuntimeError(reason) from exc
    if (
        len(raw) != expected_size
        or identity[2] != expected_size
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):  # pragma: no cover - exact import-time source boundary
        raise RuntimeError(reason)
    return raw, identity


_base_bytes, BASE_SUPPORT_STAT_IDENTITY = _read_stable_source(
    BASE_SUPPORT_PATH,
    expected_sha256=PINNED_BASE_SUPPORT_SHA256,
    expected_size=PINNED_BASE_SUPPORT_SIZE_BYTES,
    reason="collector_base_source_identity_mismatch",
)
_support_bytes, SUPPORT_STAT_IDENTITY = _read_stable_source(
    SUPPORT_PATH,
    expected_sha256=PINNED_SUPPORT_SHA256,
    expected_size=PINNED_SUPPORT_SIZE_BYTES,
    reason="collector_wrapper_source_identity_mismatch",
)
_support_module_name = (
    "_fxstack_mtvclc_resilient_v3_support_"
    + hashlib.sha256(_support_bytes).hexdigest()[:16]
)
_base_module_name = "tools.capture_ig_mt4_m1_activity"
_base_spec = importlib.util.spec_from_file_location(_base_module_name, BASE_SUPPORT_PATH)
_support_spec = importlib.util.spec_from_file_location(_support_module_name, SUPPORT_PATH)
if (
    _base_spec is None
    or _base_spec.loader is None
    or _support_spec is None
    or _support_spec.loader is None
):  # pragma: no cover
    raise RuntimeError("collector_wrapper_source_import_invalid")
_base_support = importlib.util.module_from_spec(_base_spec)
support = importlib.util.module_from_spec(_support_spec)
_tools_package = __import__("tools", fromlist=["capture_ig_mt4_m1_activity"])
_prior_support_module_bound = _support_module_name in sys.modules
_prior_support_module = sys.modules.get(_support_module_name)
_prior_base_module = sys.modules.get(_base_module_name)
_prior_base_attribute = getattr(_tools_package, "capture_ig_mt4_m1_activity", None)
_original_path_read_bytes = Path.read_bytes


def _bounded_dependency_read_bytes(path: Path) -> bytes:
    """Bound the two pinned modules' own import-time source reads."""

    absolute = Path(os.path.abspath(path))
    for expected_path, expected_size in (
        (BASE_SUPPORT_PATH, PINNED_BASE_SUPPORT_SIZE_BYTES),
        (SUPPORT_PATH, PINNED_SUPPORT_SIZE_BYTES),
    ):
        if os.path.normcase(str(absolute)) == os.path.normcase(str(expected_path)):
            payload, _identity = _read_bounded_source_descriptor(
                expected_path,
                maximum_bytes=expected_size,
                expected_size=expected_size,
            )
            return payload
    return _original_path_read_bytes(path)


sys.modules[_base_module_name] = _base_support
setattr(_tools_package, "capture_ig_mt4_m1_activity", _base_support)
sys.modules[_support_module_name] = support
try:
    # Compile and execute the bytes already pinned above.  This deliberately
    # bypasses timestamp pyc loading, so executable code and sealed source are
    # the same object.  The temporary read hook also bounds each module's own
    # import-time self-verification before allocation.
    Path.read_bytes = _bounded_dependency_read_bytes
    exec(
        compile(_base_bytes, str(BASE_SUPPORT_PATH), "exec", dont_inherit=True),
        _base_support.__dict__,
    )
    exec(
        compile(_support_bytes, str(SUPPORT_PATH), "exec", dont_inherit=True),
        support.__dict__,
    )
except Exception:
    if _prior_support_module_bound:
        sys.modules[_support_module_name] = _prior_support_module
    else:
        sys.modules.pop(_support_module_name, None)
    if _prior_base_module is None:
        sys.modules.pop(_base_module_name, None)
    else:
        sys.modules[_base_module_name] = _prior_base_module
    if _prior_base_attribute is None:
        try:
            delattr(_tools_package, "capture_ig_mt4_m1_activity")
        except AttributeError:
            pass
    else:
        setattr(
            _tools_package,
            "capture_ig_mt4_m1_activity",
            _prior_base_attribute,
        )
    raise
finally:
    Path.read_bytes = _original_path_read_bytes
    # The private support module retains its direct ``preserved`` reference.
    # Restore every temporary dependency binding so importing this collector
    # does not replace or leak unrelated process-global module state.
    if _prior_support_module_bound:
        sys.modules[_support_module_name] = _prior_support_module
    else:
        sys.modules.pop(_support_module_name, None)
    if _prior_base_module is None:
        sys.modules.pop(_base_module_name, None)
    else:
        sys.modules[_base_module_name] = _prior_base_module
    if _prior_base_attribute is None:
        try:
            delattr(_tools_package, "capture_ig_mt4_m1_activity")
        except AttributeError:
            pass
    else:
        setattr(
            _tools_package,
            "capture_ig_mt4_m1_activity",
            _prior_base_attribute,
        )
_support_after, _support_after_identity = _read_stable_source(
    SUPPORT_PATH,
    expected_sha256=PINNED_SUPPORT_SHA256,
    expected_size=PINNED_SUPPORT_SIZE_BYTES,
    reason="collector_wrapper_source_changed_during_module_start",
)
_base_after, _base_after_identity = _read_stable_source(
    BASE_SUPPORT_PATH,
    expected_sha256=PINNED_BASE_SUPPORT_SHA256,
    expected_size=PINNED_BASE_SUPPORT_SIZE_BYTES,
    reason="collector_base_source_changed_during_module_start",
)
if (
    _support_after_identity != SUPPORT_STAT_IDENTITY
    or _base_after_identity != BASE_SUPPORT_STAT_IDENTITY
    or _support_after != _support_bytes
    or _base_after != _base_bytes
):  # pragma: no cover - concurrent source replacement during import
    raise RuntimeError("collector_dependency_source_changed_during_module_start")

TOOL_PATH = Path(__file__).resolve()
BRIDGE_EA_REPOSITORY_SOURCE_PATH = (
    REPOSITORY_ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"
).resolve()
SUPPORT_SHA256 = PINNED_SUPPORT_SHA256
SUPPORT_SIZE_BYTES = PINNED_SUPPORT_SIZE_BYTES
BASE_SUPPORT_SHA256 = PINNED_BASE_SUPPORT_SHA256
BASE_SUPPORT_SIZE_BYTES = PINNED_BASE_SUPPORT_SIZE_BYTES
del _base_after
del _base_bytes
del _support_after
del _support_bytes

try:
    _source_bytes, MODULE_SOURCE_STAT_IDENTITY = _read_bounded_source_descriptor(
        TOOL_PATH,
        maximum_bytes=MAXIMUM_COLLECTOR_SOURCE_BYTES,
        expected_size=None,
    )
except OSError as exc:  # pragma: no cover - import cannot proceed safely
    raise RuntimeError("collector_source_unreadable_at_module_start") from exc
MODULE_SOURCE_SHA256 = hashlib.sha256(_source_bytes).hexdigest()
MODULE_SOURCE_SIZE_BYTES = len(_source_bytes)
del _source_bytes


COLLECTOR_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_activity_resilient_collector.v5"
CAPTURE_INTEGRITY_SCHEMA_VERSION = "fxstack.scalp.mtvclc_capture_integrity_contract.v5"
CAPTURE_INTEGRITY_POLICY_ID = (
    "first_authenticated_finalized_observation_or_declared_absence_wins_"
    "with_main_chain_anchors_and_pre_get_reservations.v5"
)
LATE_GAP_RECORD_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_late_gap_record.v2"
START_EDGE_RECEIPT_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_start_edge_durable_receipt.v1"
)
START_EDGE_RECEIPT_FILENAME = "start-edge-durable-receipt.v1.json"
MAXIMUM_START_EDGE_LAG_SECONDS = 30.0
MAXIMUM_START_EDGE_RECEIPT_BYTES = 1024 * 1024
POST_WINDOW_FINALIZATION_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_post_window_finalization.v1"
)
POST_WINDOW_FINALIZATION_FILENAME = "post-window-finalization-receipt.v1.json"
MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES = 1024 * 1024
ZERO_SHA256 = "0" * 64
DECLARATION_REVISION = "fxstack.scalp.mtvclc_gap_v3_preregistration.v1"
CAPTURE_PROFILE_ID = "gap_v3_source_pinned"
UPSTREAM_PRODUCER_SOFTWARE_SCHEMA_VERSION = (
    "fxstack.scalp.mtvclc_upstream_producer_software.v1"
)
PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_786
CURRENT_ATTEMPTED_CELLS = 44
CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_830
MINIMUM_PUBLICATION_LEAD_SECONDS = 600.0
STRATEGY_ID = "ig_mt4_tick_volume_close_location_continuation"
STRATEGY_VERSION = "mtvclc.v1"
CONFIG_ID = "mtvclc_v1_vq90_cl80_b1k_h30_t4k_s8k"
BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD = 4.648050309953223
MAXIMUM_PRODUCER_SOFTWARE_BYTES = 64 * 1024 * 1024
TAIL_COMMITMENT_SCHEMA_VERSION = "fxstack.external_ig_mt4_m1_tail_commitment.v2"
TAIL_COMMITMENT_FILENAME = "capture-tail-commitments.v2.jsonl"
CYCLE_RESERVATION_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_cycle_reservation.v1"
)
CYCLE_RESOLUTION_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_cycle_reservation_resolution.v1"
)
CYCLE_ATTEMPT_FAILURE_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_cycle_attempt_failure.v1"
)
INTERRUPTED_CYCLE_GAP_SCHEMA_VERSION = (
    "fxstack.external_ig_mt4_m1_interrupted_cycle_gap.v1"
)
MAXIMUM_MANIFEST_BYTES = 64 * 1024 * 1024
MAXIMUM_MANIFEST_ENTRIES = 8_192
MAXIMUM_MANIFEST_LINE_BYTES = 128 * 1024
MAXIMUM_ACTIVE_JOURNAL_BYTES = 64 * 1024 * 1024
MAXIMUM_ACTIVE_JOURNAL_RECORDS = 4_096
MAXIMUM_CHUNK_BYTES = 64 * 1024 * 1024
MAXIMUM_CHUNK_FILES = 8_192
MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS = 2.0
MAXIMUM_TAIL_COMMITMENT_LINE_BYTES = 48 * 1024 * 1024
SEALED_MAXIMUM_CYCLE_RESERVATIONS = (
    support.PROSPECTIVE_WINDOW_DAYS
    * 86_400
    // int(MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS)
)
TAIL_RECORDS_PER_RESERVED_CYCLE = 7
MAXIMUM_WINDOW_HOURS = support.PROSPECTIVE_WINDOW_DAYS * 24
TAIL_COMMITMENT_RECORD_MARGIN = 100_000
MAXIMUM_TAIL_COMMITMENT_RECORDS = (
    1
    + SEALED_MAXIMUM_CYCLE_RESERVATIONS * TAIL_RECORDS_PER_RESERVED_CYCLE
    + (MAXIMUM_WINDOW_HOURS + 2) * 2
    + TAIL_COMMITMENT_RECORD_MARGIN
)
MAXIMUM_WINDOW_BAR_CYCLES = (
    support.PROSPECTIVE_WINDOW_DAYS * 24 * 60 + 1
)
MAXIMUM_BASE_TAIL_BYTES_PER_RESERVED_CYCLE = 32 * 1024
MAXIMUM_INCREMENTAL_BAR_TAIL_BYTES_PER_BAR_CYCLE = 512 * 1024
TAIL_BOOTSTRAP_AND_FIXED_BYTE_MARGIN = 2 * 1024 * 1024 * 1024
MAXIMUM_TAIL_COMMITMENT_BYTES = (
    SEALED_MAXIMUM_CYCLE_RESERVATIONS
    * MAXIMUM_BASE_TAIL_BYTES_PER_RESERVED_CYCLE
    + MAXIMUM_WINDOW_BAR_CYCLES
    * MAXIMUM_INCREMENTAL_BAR_TAIL_BYTES_PER_BAR_CYCLE
    + TAIL_BOOTSTRAP_AND_FIXED_BYTE_MARGIN
)
MOVEFILE_REPLACE_EXISTING = 0x00000001
MOVEFILE_WRITE_THROUGH = 0x00000008

ATTEMPT_ACCOUNTING: dict[str, int] = {
    "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
    "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
    "cumulative_attempted_cells_lower_bound": CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND,
}
INHERITED_ZERO_CELL_ABANDONED_ATTEMPTS: tuple[dict[str, Any], ...] = (
    {
        "preregistration_body_sha256": (
            "5ff534bff7013d5836ab0034d2ef13d0739c56046d4d2637a5fd73df917c97d7"
        ),
        "artifact_file_sha256": (
            "fd32199fe13f6989743e7ad1b607ef39c2129bb3a7d20d24661eb13793347f9b"
        ),
        "reason": "atomic_publish_temp_hardlink_retained",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    {
        "preregistration_body_sha256": (
            "0511ee9c98204edc6dfd5166abade98eb47ec4f10c6364c36f504c7151853d96"
        ),
        "artifact_file_sha256": (
            "3cf1b36e4cd311425515ba8fe2268626f0c8e84ed750264e9793dd4e83c283a3"
        ),
        "reason": "first_cycle_refused_opaque_broker_token_misclassified_as_utc",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
)
D80_FAILED_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "d80e6cc9f05726ff2d2e851890ec06ca1b2df8e08efb4066bc83e31c216f17f3"
    ),
    "artifact_file_sha256": (
        "566c85789fdf1f8639a4c010421d27f3b02e6dd2f9a1912968ca8f9b500c51b3"
    ),
    "reason": "authenticated_mt4_restart_revised_completed_bar_history",
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 1_448,
    "manifest_file_sha256": (
        "86a28670961de97980a09018ea69a9190223459581ba5bfdf00a01aef0b22a6e"
    ),
    "final_manifest_entry_sha256": (
        "ec8f23c7b35a79ccce1cfb269ccd287301800c7e3c845710b775001147f1b163"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}
SEVEN_B_FAILED_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "07b78ce6d697a61db308560c325f547e90984143f2b71c1613bb8ee12b9f879c"
    ),
    "artifact_file_sha256": (
        "127d3647ab6378962c8f938e84522b16e3f82dbf6057ef553b7a087e6a4f4cba"
    ),
    "reason": "same_source_restart_late_unseen_completed_bar_backfill",
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 3,
    "manifest_file_sha256": (
        "7d3e925f819e8e49c071e088a062263dba9354467308c44e536e75b6382d737a"
    ),
    "final_manifest_entry_sha256": (
        "27feebe74d2cb172e68f70278dddd25c6ada8f5b9c1166483f8e8bbc3ccad7a9"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}
FAILED_CROSSED_T0_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "95306e016d8af3128a26228857e6953691c0ac88e881f619e074db101c95d4a1"
    ),
    "artifact_file_sha256": (
        "21d2f17464067a7e8145c051c557f0c717a2ca123bec0a11d3de26528e0d6dee"
    ),
    "reason": (
        "late_capture_then_upstream_bridge_ea_software_changed_after_eligible_"
        "observations"
    ),
    "eligible_observations_emitted": True,
    "manifest_entries_emitted": 1,
    "manifest_file_size_bytes": 4_105,
    "manifest_file_sha256": (
        "ca3ca02d0053096f41991a45d2e10260c5ddd07cdf968a8eaf5467c3ffaa5fa0"
    ),
    "active_journal_records_emitted": 363,
    "active_journal_file_size_bytes": 5_857_990,
    "active_journal_file_sha256": (
        "30df9f8116655d74e0e367cbc88f0303d821936609b89a3c932653d7521351b3"
    ),
    "start_edge_durable_receipt_emitted": False,
    "capture_finalization_state": "stopped_active_journal_preserved_unfinalized",
    "upstream_producer_software_changed_after_eligible_observations": True,
    "attempted_cells_increment": CURRENT_ATTEMPTED_CELLS,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}
AUTHORITATIVE_ABANDONED_PREREGISTRATION_LINEAGE: tuple[
    dict[str, Any], ...
] = (
    *INHERITED_ZERO_CELL_ABANDONED_ATTEMPTS,
    D80_FAILED_ATTEMPT,
    SEVEN_B_FAILED_ATTEMPT,
    FAILED_CROSSED_T0_ATTEMPT,
)


def expected_abandoned_preregistrations() -> list[dict[str, Any]]:
    """Return the exact five-record lineage emitted by the v3 sealer."""

    return [dict(row) for row in AUTHORITATIVE_ABANDONED_PREREGISTRATION_LINEAGE]


REPLACEMENT_LINEAGE: dict[str, Any] = {
    "replaces_preregistration_body_sha256": FAILED_CROSSED_T0_ATTEMPT[
        "preregistration_body_sha256"
    ],
    "replacement_reason": (
        "new_source_pinned_upstream_producer_software_and_distinct_gap_v3_window"
    ),
    "old_window_restart_or_extension": False,
    "new_independent_window_required": True,
    "old_capture_used_for_signal_outcome_or_performance_selection": False,
    "old_attempt_counted_in_multiplicity_family": True,
}

CHUNK_SCHEMA_VERSION = support.CHUNK_SCHEMA_VERSION
MANIFEST_SCHEMA_VERSION = support.MANIFEST_SCHEMA_VERSION
PREREGISTRATION_SCHEMA_VERSION = support.PREREGISTRATION_SCHEMA_VERSION
SOURCE_CONTRACT_ID = support.SOURCE_CONTRACT_ID
ACTIVITY_METRIC_ID = support.ACTIVITY_METRIC_ID
SCOPE_VERSION = support.SCOPE_VERSION
VENUE_ID = support.VENUE_ID
TIMEFRAME = support.TIMEFRAME
SYMBOLS = support.SYMBOLS
SYMBOL_SET = frozenset(SYMBOLS)
MINIMUM_M1_BARS = support.MINIMUM_M1_BARS
DEFAULT_BAR_LIMIT = support.DEFAULT_BAR_LIMIT
DEFAULT_TICK_INTERVAL_SECS = support.DEFAULT_TICK_INTERVAL_SECS
DEFAULT_BAR_INTERVAL_SECS = support.DEFAULT_BAR_INTERVAL_SECS
DEFAULT_HTTP_TIMEOUT_SECS = support.DEFAULT_HTTP_TIMEOUT_SECS
MAXIMUM_TICK_INTERVAL_SECS = support.MAXIMUM_TICK_INTERVAL_SECS
MAXIMUM_PREREGISTRATION_BYTES = support.MAXIMUM_PREREGISTRATION_BYTES
PROSPECTIVE_WINDOW_DAYS = support.PROSPECTIVE_WINDOW_DAYS
MANIFEST_FILENAME = support.MANIFEST_FILENAME
ACTIVE_JOURNAL_FILENAME = support.ACTIVE_JOURNAL_FILENAME
DATA_WRITER_LOCK_FILENAME = support.DATA_WRITER_LOCK_FILENAME
COLLECTION_AUTHORITY = dict(support._FIXED_FALSE_AUTHORITY)

CollectionRefusal = support.CollectionRefusal
BaseProspectiveBinding = support.ProspectiveBinding
BridgeReadClient = support.BridgeReadClient
SourceIdentity = support.SourceIdentity
TickObservation = support.TickObservation
M1ActivityBar = support.M1ActivityBar
BaseExclusiveDataWriterLock = support.ExclusiveDataWriterLock
CollectionPolicy = support.CollectionPolicy
canonical_json_bytes = support.canonical_json_bytes
canonical_sha256 = support.canonical_sha256
source_from_state = support.source_from_state
validate_tick_snapshot = support.validate_tick_snapshot
validate_m1_bar_response = support.validate_m1_bar_response
read_api_key_file = support.read_api_key_file
_positive_float = support._positive_float
_strict_nonnegative_int = support._strict_nonnegative_int
_strict_positive_int = support._strict_positive_int
_strict_json_object = support._strict_json_object
_is_sha256 = support._is_sha256

def _assert_exact_runtime_source(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_stat: tuple[int, int, int, int],
    reason: str,
) -> None:
    try:
        raw, observed_stat = _read_bounded_source_descriptor(
            path,
            maximum_bytes=expected_size,
            expected_size=expected_size,
        )
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    if (
        observed_stat != expected_stat
        or len(raw) != expected_size
        or hashlib.sha256(raw).hexdigest() != expected_sha256
    ):
        raise CollectionRefusal(reason)


def _assert_collector_source_unchanged() -> None:
    _assert_exact_runtime_source(
        TOOL_PATH,
        expected_sha256=MODULE_SOURCE_SHA256,
        expected_size=MODULE_SOURCE_SIZE_BYTES,
        expected_stat=MODULE_SOURCE_STAT_IDENTITY,
        reason="collector_source_path_drift",
    )
    _assert_exact_runtime_source(
        SUPPORT_PATH,
        expected_sha256=SUPPORT_SHA256,
        expected_size=SUPPORT_SIZE_BYTES,
        expected_stat=SUPPORT_STAT_IDENTITY,
        reason="collector_wrapper_source_path_drift",
    )
    _assert_exact_runtime_source(
        BASE_SUPPORT_PATH,
        expected_sha256=BASE_SUPPORT_SHA256,
        expected_size=BASE_SUPPORT_SIZE_BYTES,
        expected_stat=BASE_SUPPORT_STAT_IDENTITY,
        reason="collector_base_source_path_drift",
    )


def collector_source_sha256() -> str:
    _assert_collector_source_unchanged()
    return MODULE_SOURCE_SHA256


def _verified_base_support_sha256_bounded() -> str:
    """Bound the inherited verifier before its pinned base-source read."""

    _assert_exact_runtime_source(
        BASE_SUPPORT_PATH,
        expected_sha256=BASE_SUPPORT_SHA256,
        expected_size=BASE_SUPPORT_SIZE_BYTES,
        expected_stat=BASE_SUPPORT_STAT_IDENTITY,
        reason="collector_base_source_path_drift",
    )
    return BASE_SUPPORT_SHA256


def expected_capture_integrity_contract() -> dict[str, Any]:
    return {
        "schema_version": CAPTURE_INTEGRITY_SCHEMA_VERSION,
        "contract_id": CAPTURE_INTEGRITY_POLICY_ID,
        "collector_source_sha256": collector_source_sha256(),
        "collector_wrapper_source_sha256": SUPPORT_SHA256,
        "collector_wrapper_source_size_bytes": SUPPORT_SIZE_BYTES,
        "collector_base_source_sha256": BASE_SUPPORT_SHA256,
        "collector_base_source_size_bytes": BASE_SUPPORT_SIZE_BYTES,
        "first_authenticated_finalized_observation_is_immutable": True,
        "covered_matching_later_overlap_is_ignored": True,
        "covered_revised_later_overlap_is_ignored": True,
        "covered_overlap_never_overwrites_or_duplicates_a_bar": True,
        "late_unseen_epoch_at_or_before_watermark_is_permanent_gap": True,
        "late_unseen_epoch_is_never_backfilled": True,
        "late_unseen_epoch_is_never_baseline_eligible": True,
        "late_gap_record_schema_version": LATE_GAP_RECORD_SCHEMA_VERSION,
        "late_gap_chain_embedded_in_every_cycle_journal_chunk_and_manifest": True,
        "late_gap_chain_count_root_tail_and_source_are_restart_anchors": True,
        "late_gap_main_chain_deletion_truncation_or_rollback_relative_to_"
        "tail_commitment_refuses": True,
        "tail_commitment_filename": TAIL_COMMITMENT_FILENAME,
        "tail_commitment_schema_version": TAIL_COMMITMENT_SCHEMA_VERSION,
        "tail_commitment_is_independent_append_only_hash_chain": True,
        "tail_commitment_prepare_data_fsync_commit_ordering": True,
        "tail_commitment_exact_pending_operation_is_idempotently_recovered": True,
        "pre_get_cycle_reservation_schema_version": CYCLE_RESERVATION_SCHEMA_VERSION,
        "one_durable_pending_reservation_spans_entire_network_cycle": True,
        "reservation_is_fsynced_through_tail_wal_before_first_get": True,
        "reservation_is_asserted_current_immediately_before_every_get": True,
        "minimum_durable_cycle_reservation_cadence_seconds": (
            MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS
        ),
        "reservation_binds_exact_22_scope_producer_binding_prior_tail_and_watermarks": True,
        "reservation_clears_only_after_matching_fsynced_main_chain_evidence": True,
        "unresolved_first_cycle_reservation_permanently_fails_attempt": True,
        "unresolved_later_cycle_reservation_gaps_all_affected_minutes_for_all_22": True,
        "interrupted_cycle_gap_schema_version": INTERRUPTED_CYCLE_GAP_SCHEMA_VERSION,
        "post_window_finalization_is_integrity_only_and_network_free": True,
        "post_window_finalization_schema_version": (
            POST_WINDOW_FINALIZATION_SCHEMA_VERSION
        ),
        "post_window_finalization_filename": (
            POST_WINDOW_FINALIZATION_FILENAME
        ),
        "post_window_finalization_requires_end_exclusive_reached": True,
        "post_window_finalization_emits_no_evaluation_or_authority": True,
        "active_writer_supervision_fast_path_requires_exact_owned_process_held_"
        "writer_lock_and_fresh_stable_tail_and_journal_stats": True,
        "full_tail_validation_required_when_writer_absent_stale_restarted_or_"
        "post_window": True,
        "main_chain_rollback_or_deletion_is_detected_relative_to_tail_commitment": True,
        "coordinated_tail_commitment_and_data_rollback_detection_claimed": False,
        "tail_commitment_path_and_handle_identity_are_stable": True,
        "independently_mutable_late_gap_sidecar_forbidden": True,
        "gap_recovery_is_streamed_with_compact_epoch_intervals": True,
        "durable_per_symbol_watermark_never_regresses": True,
        "market_source_id_rollover_refuses": True,
        "gap_source_id_must_equal_capture_market_source_id_even_without_events": True,
        "downtime_gaps_are_preserved": True,
        "maximum_start_edge_lag_seconds": MAXIMUM_START_EDGE_LAG_SECONDS,
        "prospective_t0_is_utc_minute_aligned": True,
        "first_cycle_requires_complete_direct_m1_scope": True,
        "first_cycle_durable_commit_must_complete_by_t0_plus_30_seconds": True,
        "late_first_durability_is_permanent_attempt_refusal": True,
        "start_edge_durable_receipt_filename": START_EDGE_RECEIPT_FILENAME,
        "start_edge_durable_receipt_schema_version": (
            START_EDGE_RECEIPT_SCHEMA_VERSION
        ),
        "start_edge_miss_refuses_before_collection": True,
        "t0_reset_forbidden": True,
        "capture_restart_does_not_restart_or_extend_the_experiment": True,
        "tick_interval_seconds": DEFAULT_TICK_INTERVAL_SECS,
        "bar_interval_seconds": DEFAULT_BAR_INTERVAL_SECS,
        "bar_limit": DEFAULT_BAR_LIMIT,
        "http_timeout_seconds": DEFAULT_HTTP_TIMEOUT_SECS,
        "persistence_mode": (
            "tail_wal_prepare_then_fsynced_active_journal_or_immutable_chunk_and_"
            "append_only_manifest_then_tail_wal_commit"
        ),
        "windows_namespace_publication_uses_movefileex_write_through": True,
        "windows_directory_fsync_claimed": False,
        "posix_parent_directory_fsync_after_namespace_publication": True,
        "output_ancestors_reparse_points_forbidden_before_and_after_resolve": True,
        "root_chunk_hour_journal_manifest_and_tail_path_identities_are_stable": True,
        "manifest_append_never_rereads_or_replaces_complete_manifest": True,
        "restart_manifest_and_journal_recovery_is_streaming_and_bounded": True,
        "chunk_read_size_is_bounded_before_allocation": True,
        "maximum_manifest_bytes": MAXIMUM_MANIFEST_BYTES,
        "maximum_manifest_entries": MAXIMUM_MANIFEST_ENTRIES,
        "maximum_manifest_line_bytes": MAXIMUM_MANIFEST_LINE_BYTES,
        "maximum_active_journal_bytes": MAXIMUM_ACTIVE_JOURNAL_BYTES,
        "maximum_active_journal_records": MAXIMUM_ACTIVE_JOURNAL_RECORDS,
        "maximum_chunk_bytes": MAXIMUM_CHUNK_BYTES,
        "maximum_chunk_files": MAXIMUM_CHUNK_FILES,
        "maximum_tail_commitment_bytes": MAXIMUM_TAIL_COMMITMENT_BYTES,
        "maximum_tail_commitment_records": MAXIMUM_TAIL_COMMITMENT_RECORDS,
        "maximum_tail_commitment_line_bytes": MAXIMUM_TAIL_COMMITMENT_LINE_BYTES,
        "sealed_maximum_cycle_reservations": SEALED_MAXIMUM_CYCLE_RESERVATIONS,
        "tail_records_per_reserved_cycle_upper_bound": (
            TAIL_RECORDS_PER_RESERVED_CYCLE
        ),
        "tail_commitment_record_margin": TAIL_COMMITMENT_RECORD_MARGIN,
        "maximum_window_bar_cycles": MAXIMUM_WINDOW_BAR_CYCLES,
        "maximum_base_tail_bytes_per_reserved_cycle": (
            MAXIMUM_BASE_TAIL_BYTES_PER_RESERVED_CYCLE
        ),
        "maximum_incremental_bar_tail_bytes_per_bar_cycle": (
            MAXIMUM_INCREMENTAL_BAR_TAIL_BYTES_PER_BAR_CYCLE
        ),
        "tail_bootstrap_and_fixed_byte_margin": (
            TAIL_BOOTSTRAP_AND_FIXED_BYTE_MARGIN
        ),
        "tail_commitment_byte_cap_uses_full_window_workload_model": True,
        "bootstrap_first_cycle_finalized_immediately": True,
        "active_hour_journal_filename": ACTIVE_JOURNAL_FILENAME,
        "portable_chunk_schema_version": CHUNK_SCHEMA_VERSION,
        "portable_manifest_schema_version": MANIFEST_SCHEMA_VERSION,
        "exclusive_output_data_writer_lock_required": True,
        "preregistration_symlink_or_reparse_path_forbidden_before_resolve": True,
        "gap_v3_successor_envelope_and_4830_cell_family_required": True,
        "bridge_ea_repository_and_deployed_source_must_match": True,
        "bridge_ea_repository_deployed_source_and_ex4_paths_are_pairwise_distinct": True,
        "bridge_ea_source_and_ex4_checked_before_and_after_every_capture_cycle": True,
        "bridge_ea_source_or_ex4_drift_refuses_collection": True,
        "every_persisted_quote_observation_and_receipt_is_inside_window": True,
        "bar_response_row_count_never_exceeds_sealed_limit": True,
    }


_CAPTURE_INTEGRITY_CONTRACT_SHA256: str | None = None


def capture_integrity_contract_sha256() -> str:
    global _CAPTURE_INTEGRITY_CONTRACT_SHA256
    if _CAPTURE_INTEGRITY_CONTRACT_SHA256 is None:
        _CAPTURE_INTEGRITY_CONTRACT_SHA256 = canonical_sha256(
            expected_capture_integrity_contract()
        )
    return _CAPTURE_INTEGRITY_CONTRACT_SHA256


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        value = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(value.st_mode):
        return True
    attributes = int(getattr(value, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def _immutable_stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _absolute_existing_path_without_reparse(
    path: str | Path,
    *,
    reason: str,
) -> Path:
    supplied = Path(path).expanduser()
    absolute = Path(os.path.abspath(supplied))
    for candidate in reversed((absolute, *absolute.parents)):
        try:
            os.lstat(candidate)
        except OSError as exc:
            raise CollectionRefusal(reason) from exc
        if _is_reparse_or_symlink(candidate):
            raise CollectionRefusal(reason)
    try:
        resolved = absolute.resolve(strict=True)
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    if os.path.normcase(str(absolute)) != os.path.normcase(str(resolved)):
        raise CollectionRefusal(reason)
    return resolved


@dataclass(frozen=True, slots=True)
class SoftwareFileIdentity:
    filename: str
    sha256: str
    size_bytes: int

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        filename: str,
        reason: str = "upstream_producer_software_invalid",
    ) -> SoftwareFileIdentity:
        if (
            not isinstance(value, Mapping)
            or set(value) != {"filename", "sha256", "size_bytes"}
            or value.get("filename") != filename
            or not _is_sha256(value.get("sha256"))
            or isinstance(value.get("size_bytes"), bool)
            or not isinstance(value.get("size_bytes"), int)
            or int(value["size_bytes"]) <= 0
            or int(value["size_bytes"]) > MAXIMUM_PRODUCER_SOFTWARE_BYTES
        ):
            raise CollectionRefusal(reason)
        return cls(
            filename=filename,
            sha256=str(value["sha256"]).lower(),
            size_bytes=int(value["size_bytes"]),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class UpstreamProducerSoftware:
    repository_source: SoftwareFileIdentity
    deployed_source: SoftwareFileIdentity
    deployed_ex4: SoftwareFileIdentity
    body_sha256: str

    @classmethod
    def parse(
        cls,
        value: Any,
        *,
        source_identities: Mapping[str, Any],
    ) -> UpstreamProducerSoftware:
        reason = "upstream_producer_software_invalid"
        if not isinstance(value, Mapping):
            raise CollectionRefusal(reason)
        expected_fields = {
            "schema_version",
            "repository_source",
            "deployed_source",
            "deployed_ex4",
            "repository_and_deployed_source_bytes_identical_at_seal_time",
            "all_three_identities_rechecked_before_publication",
            "collector_cycle_revalidation_claimed",
            "runtime_or_broker_authority_derived_from_identity",
            "producer_software_body_sha256",
        }
        body = dict(value)
        claimed = str(body.pop("producer_software_body_sha256", "")).lower()
        if (
            set(value) != expected_fields
            or value.get("schema_version")
            != UPSTREAM_PRODUCER_SOFTWARE_SCHEMA_VERSION
            or value.get(
                "repository_and_deployed_source_bytes_identical_at_seal_time"
            )
            is not True
            or value.get("all_three_identities_rechecked_before_publication") is not True
            or value.get("collector_cycle_revalidation_claimed") is not True
            or value.get("runtime_or_broker_authority_derived_from_identity") is not False
            or not _is_sha256(claimed)
            or canonical_sha256(body) != claimed
        ):
            raise CollectionRefusal(reason)
        repository = SoftwareFileIdentity.parse(
            value.get("repository_source"), filename="BridgeEA.mq4", reason=reason
        )
        deployed = SoftwareFileIdentity.parse(
            value.get("deployed_source"), filename="BridgeEA.mq4", reason=reason
        )
        deployed_ex4 = SoftwareFileIdentity.parse(
            value.get("deployed_ex4"), filename="BridgeEA.ex4", reason=reason
        )
        expected_rows = {
            "production_engine_component:MQL4/Experts/BridgeEA.mq4": repository,
            "bridge_ea_deployed_source": deployed,
            "bridge_ea_deployed_ex4": deployed_ex4,
        }
        for key, expected in expected_rows.items():
            if source_identities.get(key) != expected.as_dict():
                raise CollectionRefusal(reason)
        if repository != deployed:
            raise CollectionRefusal(reason)
        return cls(repository, deployed, deployed_ex4, claimed)

    def receipt_fields(self) -> dict[str, Any]:
        return {
            "upstream_producer_software_body_sha256": self.body_sha256,
            "bridge_ea_repository_source_identity": self.repository_source.as_dict(),
            "bridge_ea_deployed_source_identity": self.deployed_source.as_dict(),
            "bridge_ea_deployed_ex4_identity": self.deployed_ex4.as_dict(),
        }


@dataclass(frozen=True, slots=True)
class _StableSoftwareSnapshot:
    path: Path
    stat_identity: tuple[int, int, int, int, int]
    sha256: str
    size_bytes: int


def _stable_software_snapshot(
    path: str | Path,
    *,
    reason: str,
) -> _StableSoftwareSnapshot:
    resolved = _absolute_existing_path_without_reparse(path, reason=reason)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(resolved)
        descriptor = os.open(resolved, flags)
        try:
            before_handle = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before_handle.st_mode)
                or before_handle.st_size <= 0
                or before_handle.st_size > MAXIMUM_PRODUCER_SOFTWARE_BYTES
            ):
                raise CollectionRefusal(reason)
            digest = hashlib.sha256()
            total = 0
            while True:
                block = os.read(descriptor, 1 << 20)
                if not block:
                    break
                digest.update(block)
                total += len(block)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.lstat(resolved)
        resolved_after = _absolute_existing_path_without_reparse(resolved, reason=reason)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    identities = {
        _immutable_stat_identity(before_path),
        _immutable_stat_identity(before_handle),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if (
        len(identities) != 1
        or total != before_handle.st_size
        or resolved_after != resolved
    ):
        raise CollectionRefusal(reason)
    return _StableSoftwareSnapshot(
        path=resolved,
        stat_identity=_immutable_stat_identity(after_handle),
        sha256=digest.hexdigest(),
        size_bytes=total,
    )


@dataclass(frozen=True, slots=True)
class ProducerSoftwareMonitor:
    contract: UpstreamProducerSoftware
    repository_source: _StableSoftwareSnapshot
    deployed_source: _StableSoftwareSnapshot
    deployed_ex4: _StableSoftwareSnapshot

    @classmethod
    def bind(
        cls,
        contract: UpstreamProducerSoftware,
        *,
        repository_source_path: str | Path,
        deployed_source_path: str | Path,
        deployed_ex4_path: str | Path,
    ) -> ProducerSoftwareMonitor:
        reason = "upstream_producer_software_path_or_identity_invalid"
        repository = _stable_software_snapshot(repository_source_path, reason=reason)
        if os.path.normcase(str(repository.path)) != os.path.normcase(
            str(BRIDGE_EA_REPOSITORY_SOURCE_PATH)
        ):
            raise CollectionRefusal(reason)
        deployed = _stable_software_snapshot(deployed_source_path, reason=reason)
        deployed_ex4 = _stable_software_snapshot(deployed_ex4_path, reason=reason)
        normalized_paths = {
            os.path.normcase(str(repository.path)),
            os.path.normcase(str(deployed.path)),
            os.path.normcase(str(deployed_ex4.path)),
        }
        if len(normalized_paths) != 3:
            raise CollectionRefusal(reason)
        rows = (
            (repository, contract.repository_source),
            (deployed, contract.deployed_source),
            (deployed_ex4, contract.deployed_ex4),
        )
        if any(
            observed.path.name != expected.filename
            or observed.sha256 != expected.sha256
            or observed.size_bytes != expected.size_bytes
            for observed, expected in rows
        ):
            raise CollectionRefusal(reason)
        if repository.sha256 != deployed.sha256 or repository.size_bytes != deployed.size_bytes:
            raise CollectionRefusal(reason)
        return cls(contract, repository, deployed, deployed_ex4)

    def assert_unchanged(self) -> None:
        self.current_proof()

    def current_proof(self) -> dict[str, Any]:
        reason = "upstream_producer_software_drift"
        rows = (
            (
                "repository_source",
                self.repository_source,
                self.contract.repository_source,
            ),
            ("deployed_source", self.deployed_source, self.contract.deployed_source),
            ("deployed_ex4", self.deployed_ex4, self.contract.deployed_ex4),
        )
        proof_rows: dict[str, Any] = {}
        for key, initial, expected in rows:
            observed = _stable_software_snapshot(initial.path, reason=reason)
            if (
                observed.stat_identity != initial.stat_identity
                or observed.sha256 != expected.sha256
                or observed.size_bytes != expected.size_bytes
            ):
                raise CollectionRefusal(reason)
            proof_rows[key] = {
                "path": str(observed.path),
                "stat_identity": list(observed.stat_identity),
                "sha256": observed.sha256,
                "size_bytes": observed.size_bytes,
            }
        body = {
            "schema_version": "fxstack.scalp.mtvclc_producer_monitor_proof.v1",
            "producer_software_body_sha256": self.contract.body_sha256,
            **proof_rows,
            "runtime_or_broker_authority_derived": False,
        }
        return {**body, "monitor_proof_sha256": canonical_sha256(body)}


@dataclass(frozen=True, slots=True)
class ProspectiveBinding(BaseProspectiveBinding):
    upstream_producer_software: UpstreamProducerSoftware | None = None
    producer_software_monitor: ProducerSoftwareMonitor | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def producer_receipt_fields(self) -> dict[str, Any]:
        if self.upstream_producer_software is None:
            raise CollectionRefusal("upstream_producer_software_binding_missing")
        return self.upstream_producer_software.receipt_fields()


def expected_preservation_filename_contract() -> dict[str, Any]:
    return {
        "schema_version": "fxstack.scalp.mtvclc_preservation_filenames.v3",
        "capture_profile_id": CAPTURE_PROFILE_ID,
        "preregistration_filename_template": (
            "mtvclc_gap_v3_preregistration_{preregistration_body_sha256}.json"
        ),
        "capture_root_name_template": (
            "mtvclc_prospective_capture_gap_v3_"
            "{preregistration_body_sha256_prefix16}"
        ),
        "handoff_filename_template": (
            "mtvclc_capture_handoff_v2_{handoff_body_sha256}.json"
        ),
        "report_filename_template": (
            "mtvclc_post_window_report_v3_{artifact_sha256}.json"
        ),
        "reservation_ledger_filename_template": (
            "mtvclc_reservation_ledger_v2_{artifact_sha256}.jsonl"
        ),
        "outcome_ledger_filename_template": (
            "mtvclc_outcome_ledger_v2_{artifact_sha256}.jsonl"
        ),
        "cell_ledger_filename_template": (
            "mtvclc_cell_ledger_v2_{artifact_sha256}.jsonl"
        ),
        "append_only_no_overwrite": True,
        "old_attempt_filenames_are_never_reused": True,
    }


def _read_preregistration_without_link_follow(path: str | Path) -> tuple[Path, bytes]:
    supplied = Path(path).expanduser()
    if _is_reparse_or_symlink(supplied):
        raise CollectionRefusal("preregistration_symlink_forbidden")
    try:
        supplied_absolute = Path(os.path.abspath(supplied))
        resolved = supplied.resolve(strict=True)
    except OSError as exc:
        raise CollectionRefusal("preregistration_file_invalid") from exc
    if os.path.normcase(str(supplied_absolute)) != os.path.normcase(str(resolved)):
        raise CollectionRefusal("preregistration_symlink_forbidden")
    if not resolved.is_file() or _is_reparse_or_symlink(resolved):
        raise CollectionRefusal("preregistration_file_invalid")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(resolved)
        descriptor = os.open(resolved, flags)
        try:
            before_handle = os.fstat(descriptor)
            if before_handle.st_size <= 0 or before_handle.st_size > MAXIMUM_PREREGISTRATION_BYTES:
                raise CollectionRefusal("preregistration_file_size_invalid")
            chunks: list[bytes] = []
            remaining = int(before_handle.st_size)
            while remaining:
                block = os.read(descriptor, min(1 << 20, remaining))
                if not block:
                    raise CollectionRefusal("preregistration_file_changed_during_read")
                chunks.append(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise CollectionRefusal("preregistration_file_changed_during_read")
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.lstat(resolved)
        resolved_after = supplied.resolve(strict=True)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal("preregistration_file_unreadable") from exc
    identities = (
        _stat_identity(before_path),
        _stat_identity(before_handle),
        _stat_identity(after_handle),
        _stat_identity(after_path),
    )
    if (
        len(set(identities)) != 1
        or resolved_after != resolved
        or _is_reparse_or_symlink(supplied)
        or _is_reparse_or_symlink(resolved)
    ):
        raise CollectionRefusal("preregistration_file_changed_during_read")
    return resolved, b"".join(chunks)


def _parse_utc_second(value: Any, reason: str) -> datetime:
    return support._parse_utc_second(value, reason)


def load_preregistration(
    path: str | Path,
    *,
    bridge_ea_repository_source: str | Path | None = None,
    bridge_ea_deployed_source: str | Path | None = None,
    bridge_ea_deployed_ex4: str | Path | None = None,
) -> ProspectiveBinding:
    """Parse the exact gap-v3 successor and optionally bind its host files."""

    _assert_collector_source_unchanged()
    _target, raw = _read_preregistration_without_link_follow(path)
    payload = _strict_json_object(raw, reason="preregistration_json_invalid")
    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not _is_sha256(claimed) or claimed != canonical_sha256(body):
        raise CollectionRefusal("preregistration_body_hash_invalid")
    if (
        body.get("declaration_revision") != DECLARATION_REVISION
        or body.get("capture_profile_id") != CAPTURE_PROFILE_ID
        or body.get("schema_version") != PREREGISTRATION_SCHEMA_VERSION
        or body.get("research_only") is not True
        or body.get("authority") != COLLECTION_AUTHORITY
        or body.get("attempt_accounting") != ATTEMPT_ACCOUNTING
        or body.get("replacement_lineage") != REPLACEMENT_LINEAGE
        or body.get("preservation_filename_contract")
        != expected_preservation_filename_contract()
        or body.get("capture_integrity_contract")
        != expected_capture_integrity_contract()
    ):
        raise CollectionRefusal("preregistration_contract_invalid")

    scope = body.get("scope")
    strategy = body.get("strategy")
    execution = body.get("execution_contract")
    window = body.get("prospective_window")
    identities = body.get("source_identities")
    gates = body.get("fixed_success_gates")
    abandoned = body.get("abandoned_preregistrations")
    if not all(
        isinstance(value, Mapping)
        for value in (scope, strategy, execution, window, identities, gates)
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    assert isinstance(scope, Mapping)
    assert isinstance(strategy, Mapping)
    assert isinstance(execution, Mapping)
    assert isinstance(window, Mapping)
    assert isinstance(identities, Mapping)
    assert isinstance(gates, Mapping)
    if (
        not isinstance(abandoned, list)
        or abandoned != expected_abandoned_preregistrations()
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    producer = UpstreamProducerSoftware.parse(
        body.get("upstream_producer_software"),
        source_identities=identities,
    )
    repository_snapshot = _stable_software_snapshot(
        BRIDGE_EA_REPOSITORY_SOURCE_PATH,
        reason="upstream_producer_software_repository_identity_invalid",
    )
    if (
        repository_snapshot.path.name != producer.repository_source.filename
        or repository_snapshot.sha256 != producer.repository_source.sha256
        or repository_snapshot.size_bytes != producer.repository_source.size_bytes
    ):
        raise CollectionRefusal("upstream_producer_software_repository_identity_invalid")
    cells = scope.get("cell_order")
    observed_cells: list[tuple[str, str, str]] = []
    if isinstance(cells, list):
        for row in cells:
            if not isinstance(row, Mapping):
                raise CollectionRefusal("preregistration_scope_invalid")
            observed_cells.append(
                (
                    str(row.get("config_id") or ""),
                    str(row.get("symbol") or "").strip().upper(),
                    str(row.get("side") or "").strip().upper(),
                )
            )
    expected_cells = [
        (CONFIG_ID, symbol, side)
        for symbol in SYMBOLS
        for side in ("BUY", "SELL")
    ]

    expected_identities = (
        ("collector_source", TOOL_PATH, MODULE_SOURCE_SHA256, MODULE_SOURCE_SIZE_BYTES),
        ("collector_support_source", SUPPORT_PATH, SUPPORT_SHA256, SUPPORT_SIZE_BYTES),
        (
            "collector_base_source",
            BASE_SUPPORT_PATH,
            BASE_SUPPORT_SHA256,
            BASE_SUPPORT_SIZE_BYTES,
        ),
    )
    identities_valid = True
    for key, source_path, digest, size in expected_identities:
        identity = identities.get(key)
        identities_valid = identities_valid and bool(
            isinstance(identity, Mapping)
            and set(identity) == {"filename", "sha256", "size_bytes"}
            and identity.get("filename") == source_path.name
            and str(identity.get("sha256") or "").lower() == digest
            and identity.get("size_bytes") == size
        )
    manifest = strategy.get("attempt_manifest")
    manifest_valid = bool(
        isinstance(manifest, Mapping)
        and strategy.get("attempt_manifest_sha256") == canonical_sha256(manifest)
        and manifest.get("prior_attempted_cells_lower_bound")
        == PRIOR_ATTEMPTED_CELLS_LOWER_BOUND
        and manifest.get("current_attempted_cells") == CURRENT_ATTEMPTED_CELLS
        and manifest.get("cumulative_attempted_cells_lower_bound")
        == CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        and manifest.get("win_probability_familywise_attempted_cells")
        == CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        and manifest.get("win_probability_alpha_allocation")
        == "one_sided_0.05_over_4830"
        and manifest.get("descriptive_df99_bonferroni_abs_t_threshold")
        == BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        and manifest.get("cell_summary_source")
        == "exclusive_recomputation_from_complete_reservation_and_outcome_ledgers"
        and manifest.get("empty_missing_duplicate_or_inconsistent_ledgers_refuse")
        is True
    )
    if (
        scope.get("ordered_symbols") != list(SYMBOLS)
        or scope.get("scope_version") != SCOPE_VERSION
        or scope.get("venue_id") != VENUE_ID
        or scope.get("sides") != ["BUY", "SELL"]
        or observed_cells != expected_cells
        or strategy.get("strategy_id") != STRATEGY_ID
        or strategy.get("strategy_version") != STRATEGY_VERSION
        or strategy.get("config_id") != CONFIG_ID
        or not _is_sha256(strategy.get("config_sha256"))
        or strategy.get("source_contract_id") != SOURCE_CONTRACT_ID
        or strategy.get("activity_metric_id") != ACTIVITY_METRIC_ID
        or not manifest_valid
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day") != 1
        or not identities_valid
        or gates.get("all_44_cells_must_pass") is not True
        or gates.get("minimum_trades_per_cell") != 30
        or gates.get("minimum_independent_utc_days_per_cell") != 10
        or gates.get("cell_win_probability_interval")
        != "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
        or gates.get("cell_win_probability_family_confidence") != 0.95
        or gates.get("descriptive_df99_bonferroni_abs_t_threshold")
        != BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
        or window.get("consecutive_days") != PROSPECTIVE_WINDOW_DAYS
        or window.get("fixed_before_any_eligible_observation") is not True
        or window.get("observations_before_t0_forbidden") is not True
        or window.get("observations_at_or_after_end_forbidden") is not True
        or window.get("interim_signal_or_outcome_evaluation_forbidden") is not True
        or window.get("early_success_forbidden") is not True
        or window.get("no_optional_extension_or_restart_after_failure") is not True
    ):
        raise CollectionRefusal("preregistration_contract_invalid")
    sealed_at = _parse_utc_second(body.get("sealed_at_utc"), "preregistration_time_invalid")
    t0 = _parse_utc_second(window.get("t0_utc_inclusive"), "preregistration_time_invalid")
    end = _parse_utc_second(window.get("end_utc_exclusive"), "preregistration_time_invalid")
    if (
        t0 <= sealed_at
        or (t0 - sealed_at).total_seconds() < MINIMUM_PUBLICATION_LEAD_SECONDS
        or int(t0.timestamp()) % 60 != 0
        or end - t0 != timedelta(days=PROSPECTIVE_WINDOW_DAYS)
    ):
        raise CollectionRefusal("preregistration_time_invalid")
    supplied_paths = (
        bridge_ea_repository_source,
        bridge_ea_deployed_source,
        bridge_ea_deployed_ex4,
    )
    if any(value is not None for value in supplied_paths) and not all(
        value is not None for value in supplied_paths
    ):
        raise CollectionRefusal("upstream_producer_software_paths_incomplete")
    monitor = None
    if all(value is not None for value in supplied_paths):
        assert bridge_ea_repository_source is not None
        assert bridge_ea_deployed_source is not None
        assert bridge_ea_deployed_ex4 is not None
        monitor = ProducerSoftwareMonitor.bind(
            producer,
            repository_source_path=bridge_ea_repository_source,
            deployed_source_path=bridge_ea_deployed_source,
            deployed_ex4_path=bridge_ea_deployed_ex4,
        )
    return ProspectiveBinding(
        preregistration_body_sha256=claimed,
        preregistration_artifact_sha256=hashlib.sha256(raw).hexdigest(),
        t0_utc=t0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        end_utc_exclusive=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        t0_epoch=t0.timestamp(),
        end_epoch_exclusive=end.timestamp(),
        upstream_producer_software=producer,
        producer_software_monitor=monitor,
    )


def _directory_stat_identity(value: os.stat_result) -> tuple[int, int]:
    return int(value.st_dev), int(value.st_ino)


def _secure_directory_chain(
    path: Path,
    *,
    allow_missing_tail: bool,
    reason: str,
) -> dict[str, tuple[int, int]]:
    """Return stable identities for an absolute, reparse-free directory chain."""

    absolute = Path(os.path.abspath(path.expanduser()))
    identities: dict[str, tuple[int, int]] = {}
    missing = False
    for candidate in reversed((absolute, *absolute.parents)):
        key = os.path.normcase(str(candidate))
        try:
            observed = os.lstat(candidate)
        except FileNotFoundError:
            if not allow_missing_tail:
                raise CollectionRefusal(reason) from None
            missing = True
            continue
        except OSError as exc:
            raise CollectionRefusal(reason) from exc
        if missing or _is_reparse_or_symlink(candidate) or not stat.S_ISDIR(
            observed.st_mode
        ):
            raise CollectionRefusal(reason)
        identities[key] = _directory_stat_identity(observed)
    if not identities:
        raise CollectionRefusal(reason)
    return identities


def _secure_parent_for_write(path: Path, *, reason: str) -> dict[str, tuple[int, int]]:
    before = _secure_directory_chain(
        path.parent, allow_missing_tail=True, reason=reason
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    after = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=reason
    )
    if any(after.get(key) != value for key, value in before.items()):
        raise CollectionRefusal(reason)
    return after


def _assert_secure_parent_unchanged(
    path: Path,
    expected: Mapping[str, tuple[int, int]],
    *,
    reason: str,
) -> None:
    observed = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=reason
    )
    if dict(expected) != observed:
        raise CollectionRefusal(reason)


def _path_lexists(path: Path) -> bool:
    try:
        os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise CollectionRefusal("filesystem_path_identity_invalid") from exc
    return True


def _windows_namespace_publish(source: Path, target: Path, *, replace: bool) -> None:
    """Publish one Windows namespace mutation with MOVEFILE_WRITE_THROUGH."""

    if os.name != "nt":  # pragma: no cover - guarded by callers
        raise RuntimeError("windows_namespace_publish_called_off_windows")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
    move_file_ex.restype = ctypes.c_int
    flags = MOVEFILE_WRITE_THROUGH | (MOVEFILE_REPLACE_EXISTING if replace else 0)
    if not move_file_ex(str(source), str(target), flags):
        error = ctypes.get_last_error()
        if not replace and error in {80, 183}:
            raise FileExistsError(error, os.strerror(error), str(target))
        raise OSError(error, os.strerror(error), str(target))


def _sync_parent_directory(path: Path) -> None:
    """Fsync a POSIX parent; Windows publication uses MoveFileExW write-through."""

    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _verify_published_file(
    path: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    parent_identities: Mapping[str, tuple[int, int]],
    reason: str,
) -> tuple[int, int, int, int, int]:
    flags = (
        (os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.lstat(path)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size != expected_size
                or _immutable_stat_identity(before)
                != _immutable_stat_identity(opened)
            ):
                raise CollectionRefusal(reason)
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
            digest = hashlib.sha256()
            remaining = expected_size
            while remaining:
                block = os.read(descriptor, min(1 << 20, remaining))
                if not block:
                    raise CollectionRefusal(reason)
                digest.update(block)
                remaining -= len(block)
            if os.read(descriptor, 1) or digest.hexdigest() != expected_sha256:
                raise CollectionRefusal(reason)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = os.lstat(path)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    identities = {
        _immutable_stat_identity(before),
        _immutable_stat_identity(opened),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if len(identities) != 1:
        raise CollectionRefusal(reason)
    _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
    return identities.pop()


def _durable_atomic_write_new(path: Path, payload: bytes) -> None:
    """Publish one immutable file without overwrite and with truthful durability."""

    reason = "immutable_file_publication_path_drift"
    if _path_lexists(path):
        raise CollectionRefusal("chunk_path_already_exists")
    parent_identities = _secure_parent_for_write(path, reason=reason)
    if _path_lexists(path):
        raise CollectionRefusal("chunk_path_already_exists")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    published = False
    try:
        with os.fdopen(descriptor, "wb") as handle:
            offset = 0
            while offset < len(payload):
                written = handle.write(payload[offset:])
                if written is None or written <= 0:
                    raise OSError("short immutable write")
                offset += written
            handle.flush()
            os.fsync(handle.fileno())
            temporary_handle_identity = _immutable_stat_identity(
                os.fstat(handle.fileno())
            )
        temporary_path_identity = _immutable_stat_identity(os.lstat(temporary))
        if temporary_path_identity != temporary_handle_identity:
            raise CollectionRefusal(reason)
        _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
        if os.name == "nt":
            _windows_namespace_publish(temporary, path, replace=False)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise CollectionRefusal("chunk_path_already_exists") from exc
        published = True
        _verify_published_file(
            path,
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            parent_identities=parent_identities,
            reason=reason,
        )
        _sync_parent_directory(path)
        if os.name != "nt":
            temporary.unlink()
            _sync_parent_directory(path)
    except Exception:
        if published:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _durable_replace(path: Path, payload: bytes) -> None:
    reason = "replace_file_publication_path_drift"
    parent_identities = _secure_parent_for_write(path, reason=reason)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".replace", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
            temporary_handle_identity = _immutable_stat_identity(
                os.fstat(handle.fileno())
            )
        temporary_path_identity = _immutable_stat_identity(os.lstat(temporary))
        if temporary_path_identity != temporary_handle_identity:
            raise CollectionRefusal(reason)
        _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
        if os.name == "nt":
            _windows_namespace_publish(temporary, path, replace=True)
        else:
            os.replace(temporary, path)
        _verify_published_file(
            path,
            expected_size=len(payload),
            expected_sha256=hashlib.sha256(payload).hexdigest(),
            parent_identities=parent_identities,
            reason=reason,
        )
        _sync_parent_directory(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _read_bounded_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    missing_reason: str,
    invalid_reason: str,
) -> tuple[bytes, tuple[int, int, int, int, int]]:
    parent_identities = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=invalid_reason
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(path)
        if (
            _is_reparse_or_symlink(path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size < 0
            or before_path.st_size > maximum_bytes
        ):
            raise CollectionRefusal(invalid_reason)
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CollectionRefusal(missing_reason) from exc
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(invalid_reason) from exc
    try:
        before_handle = os.fstat(descriptor)
        if _immutable_stat_identity(before_path) != _immutable_stat_identity(
            before_handle
        ):
            raise CollectionRefusal(invalid_reason)
        payload = bytearray()
        remaining = int(before_handle.st_size)
        while remaining:
            block = os.read(descriptor, min(1 << 20, remaining))
            if not block:
                raise CollectionRefusal(invalid_reason)
            payload.extend(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise CollectionRefusal(invalid_reason)
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise CollectionRefusal(invalid_reason) from exc
    identities = {
        _immutable_stat_identity(before_path),
        _immutable_stat_identity(before_handle),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if len(identities) != 1 or len(payload) != before_handle.st_size:
        raise CollectionRefusal(invalid_reason)
    _assert_secure_parent_unchanged(path, parent_identities, reason=invalid_reason)
    return bytes(payload), identities.pop()


def _append_fsynced_line(
    path: Path,
    line: bytes,
    *,
    expected_size: int,
    expected_identity: tuple[int, int, int, int, int] | None,
    maximum_bytes: int,
    reason: str,
) -> tuple[int, tuple[int, int, int, int, int]]:
    if (
        not line
        or not line.endswith(b"\n")
        or expected_size < 0
        or expected_size + len(line) > maximum_bytes
    ):
        raise CollectionRefusal(reason)
    parent_identities = _secure_parent_for_write(path, reason=reason)
    if expected_size == 0:
        if expected_identity is not None or _path_lexists(path):
            raise CollectionRefusal(reason)
        try:
            _durable_atomic_write_new(path, line)
        except CollectionRefusal as exc:
            raise CollectionRefusal(reason) from exc
        observed = os.lstat(path)
        identity = _immutable_stat_identity(observed)
        _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
        return len(line), identity
    if expected_identity is None:
        raise CollectionRefusal(reason)
    flags = (
        os.O_WRONLY
        | os.O_APPEND
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor: int | None = None
    try:
        before_path = os.lstat(path)
        if (
            _is_reparse_or_symlink(path)
            or _immutable_stat_identity(before_path) != expected_identity
            or before_path.st_size != expected_size
        ):
            raise CollectionRefusal(reason)
        descriptor = os.open(path, flags)
        before_handle = os.fstat(descriptor)
        if (
            _immutable_stat_identity(before_handle) != expected_identity
            or not stat.S_ISREG(before_handle.st_mode)
        ):
            raise CollectionRefusal(reason)
        offset = 0
        while offset < len(line):
            written = os.write(descriptor, line[offset:])
            if written <= 0:
                raise OSError("short append")
            offset += written
        os.fsync(descriptor)
        after_handle = os.fstat(descriptor)
        after_path = os.lstat(path)
        if (
            _immutable_stat_identity(after_handle)
            != _immutable_stat_identity(after_path)
            or after_handle.st_size != expected_size + len(line)
        ):
            raise CollectionRefusal(reason)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
    return int(after_handle.st_size), _immutable_stat_identity(after_handle)


class ExclusiveDataWriterLock(BaseExclusiveDataWriterLock):
    """Output lock that refuses reparse ancestors before any filesystem mutation."""

    def __init__(self, output_root: str | Path) -> None:
        self.root = Path(os.path.abspath(Path(output_root).expanduser()))
        _secure_directory_chain(
            self.root,
            allow_missing_tail=True,
            reason="data_writer_lock_path_invalid",
        )
        self.path = self.root / DATA_WRITER_LOCK_FILENAME
        self._handle: Any = None
        self.acquired = False

    def acquire(self) -> ExclusiveDataWriterLock:
        parent_before = _secure_directory_chain(
            self.root,
            allow_missing_tail=True,
            reason="data_writer_lock_path_invalid",
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CollectionRefusal("data_writer_lock_path_invalid") from exc
        parent_after = _secure_directory_chain(
            self.root,
            allow_missing_tail=False,
            reason="data_writer_lock_path_invalid",
        )
        if any(parent_after.get(key) != value for key, value in parent_before.items()):
            raise CollectionRefusal("data_writer_lock_path_invalid")
        registry_key = str(self.path).casefold()
        with support._DATA_LOCK_REGISTRY_GUARD:
            if registry_key in support._HELD_DATA_LOCK_PATHS:
                raise CollectionRefusal("exclusive_data_writer_lock_unavailable")
            support._HELD_DATA_LOCK_PATHS.add(registry_key)
        descriptor: int | None = None
        handle: Any = None
        try:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(self.path, flags, 0o600)
            path_stat = os.lstat(self.path)
            handle_stat = os.fstat(descriptor)
            if (
                _is_reparse_or_symlink(self.path)
                or not stat.S_ISREG(handle_stat.st_mode)
                or _immutable_stat_identity(path_stat)
                != _immutable_stat_identity(handle_stat)
            ):
                raise CollectionRefusal("data_writer_lock_path_invalid")
            handle = os.fdopen(descriptor, "r+b", buffering=0)
            descriptor = None
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(  # type: ignore[attr-defined]
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
                )
            observed_after = os.lstat(self.path)
            if _immutable_stat_identity(observed_after) != _immutable_stat_identity(
                os.fstat(handle.fileno())
            ):
                raise CollectionRefusal("data_writer_lock_path_invalid")
            current_chain = _secure_directory_chain(
                self.root,
                allow_missing_tail=False,
                reason="data_writer_lock_path_invalid",
            )
            if current_chain != parent_after:
                raise CollectionRefusal("data_writer_lock_path_invalid")
        except (CollectionRefusal, OSError, ImportError) as exc:
            if descriptor is not None:
                os.close(descriptor)
            if handle is not None:
                handle.close()
            with support._DATA_LOCK_REGISTRY_GUARD:
                support._HELD_DATA_LOCK_PATHS.discard(registry_key)
            if isinstance(exc, CollectionRefusal):
                raise
            raise CollectionRefusal("exclusive_data_writer_lock_unavailable") from exc
        self._handle = handle
        self.acquired = True
        return self

    def authorizes(self, output_root: str | Path) -> bool:
        supplied = Path(os.path.abspath(Path(output_root).expanduser()))
        if not self.acquired or supplied != self.root:
            return False
        try:
            _secure_directory_chain(
                supplied,
                allow_missing_tail=False,
                reason="data_writer_lock_path_invalid",
            )
        except CollectionRefusal:
            return False
        return True


_GAP_EVENT_FIELDS = frozenset(
    {
        "symbol",
        "minute_epoch",
        "watermark_epoch",
        "late_payload_sha256",
        "disposition",
        "baseline_eligible",
    }
)
_GAP_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "gap_sequence",
        "previous_gap_entry_sha256",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "market_source_id",
        "observed_at_epoch",
        "events",
        "gap_entry_sha256",
    }
)
_GAP_CYCLE_FIELDS = frozenset(
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
        "late_gap_records",
    }
)
_GAP_MANIFEST_FIELDS = frozenset(
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
    }
)
_RESERVATION_CYCLE_FIELDS = frozenset(
    {
        "cycle_reservation_sequence",
        "cycle_reservation_sha256",
        "cycle_part_index",
        "cycle_part_count",
        "cycle_part_final",
        "cycle_reservation_parts",
        "interrupted_cycle_gap_evidence",
    }
)
_RESERVATION_MANIFEST_FIELDS = frozenset(
    {
        "cycle_reservation_sequence",
        "cycle_reservation_sha256",
        "cycle_part_index",
        "cycle_part_count",
        "cycle_part_final",
        "cycle_reservation_part_count",
        "cycle_reservation_parts_sha256",
        "interrupted_cycle_gap_count",
        "interrupted_cycle_gap_evidence_sha256",
    }
)
_CYCLE_RESERVATION_FIELDS = frozenset(
    {
        "schema_version",
        "reservation_sequence",
        "previous_cycle_reservation_sha256",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "upstream_producer_software_body_sha256",
        "bridge_ea_repository_source_identity",
        "bridge_ea_deployed_source_identity",
        "bridge_ea_deployed_ex4_identity",
        "producer_monitor_proof",
        "symbol_scope",
        "symbol_scope_sha256",
        "include_bars",
        "first_cycle",
        "cycle_started_at_epoch",
        "prior_main_state",
        "prior_main_state_sha256",
        "prior_market_source",
        "collection_only",
        "evaluation_performed",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
        "reservation_sha256",
    }
)
_ATTEMPT_FAILURE_FIELDS = frozenset(
    {
        "schema_version",
        "reservation_sequence",
        "reservation_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "reason",
        "failed_at_epoch",
        "terminal_for_preregistration_attempt",
        "collection_only",
        "evaluation_performed",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
        "failure_sha256",
    }
)
_INTERRUPTED_GAP_FIELDS = frozenset(
    {
        "schema_version",
        "reservation_sequence",
        "reservation_sha256",
        "symbol",
        "start_minute_epoch",
        "end_minute_epoch",
        "minute_count",
        "recovered_at_epoch",
        "disposition",
        "baseline_eligible",
    }
)
_CYCLE_PART_FIELDS = frozenset(
    {
        "reservation_sequence",
        "reservation_sha256",
        "part_index",
        "part_count",
        "final",
    }
)
_PRIOR_MAIN_STATE_FIELDS = frozenset(
    {
        "manifest_sequence",
        "manifest_entry_sha256",
        "manifest_size_bytes",
        "journal_manifest_sequence_target",
        "journal_sequence",
        "journal_entry_sha256",
        "journal_size_bytes",
        "tail_registry_sequence",
        "tail_entry_sha256",
        "tail_committed_state_sha256",
        "active_hour",
        "gap_chain_count",
        "gap_event_count",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "last_bar_epoch_by_symbol",
        "last_tick_sequence_by_symbol",
        "last_tick_transport_epoch_by_symbol",
        "last_tick_snapshot_sha256_by_symbol",
    }
)


def _validated_cycle_reservation(
    value: Any,
    *,
    expected_contract_sha256: str | None = None,
) -> dict[str, Any]:
    reason = "cycle_reservation_invalid"
    if not isinstance(value, Mapping) or set(value) != _CYCLE_RESERVATION_FIELDS:
        raise CollectionRefusal(reason)
    reservation = dict(value)
    body = dict(reservation)
    claimed = str(body.pop("reservation_sha256", "")).lower()
    prior = reservation.get("prior_main_state")
    proof = reservation.get("producer_monitor_proof")
    proof_body = dict(proof) if isinstance(proof, Mapping) else {}
    proof_hash = str(proof_body.pop("monitor_proof_sha256", "")).lower()
    binding = tuple(
        str(reservation.get(key) or "")
        for key in (
            "preregistration_body_sha256",
            "preregistration_artifact_sha256",
            "prospective_t0_utc_inclusive",
            "prospective_end_utc_exclusive",
        )
    )
    started = _positive_float(
        reservation.get("cycle_started_at_epoch"), reason
    )
    producer_keys = (
        (
            "bridge_ea_repository_source_identity",
            "repository_source",
        ),
        ("bridge_ea_deployed_source_identity", "deployed_source"),
        ("bridge_ea_deployed_ex4_identity", "deployed_ex4"),
    )
    producer_identities_valid = True
    for identity_key, proof_key in producer_keys:
        identity = reservation.get(identity_key)
        proof_row = proof.get(proof_key) if isinstance(proof, Mapping) else None
        if (
            not isinstance(identity, Mapping)
            or set(identity) != {"filename", "sha256", "size_bytes"}
            or not _is_sha256(identity.get("sha256"))
            or isinstance(identity.get("size_bytes"), bool)
            or not isinstance(identity.get("size_bytes"), int)
            or int(identity["size_bytes"]) <= 0
            or not isinstance(proof_row, Mapping)
            or proof_row.get("sha256") != identity.get("sha256")
            or proof_row.get("size_bytes") != identity.get("size_bytes")
            or not isinstance(proof_row.get("path"), str)
            or not isinstance(proof_row.get("stat_identity"), list)
            or len(proof_row["stat_identity"]) != 5
            or any(
                isinstance(item, bool) or not isinstance(item, int)
                for item in proof_row["stat_identity"]
            )
        ):
            producer_identities_valid = False
            break
    prior_source = reservation.get("prior_market_source")
    prior_maps_valid = isinstance(prior, Mapping)
    if prior_maps_valid:
        for key, value_kind in (
            ("last_bar_epoch_by_symbol", "int"),
            ("last_tick_sequence_by_symbol", "int"),
            ("last_tick_transport_epoch_by_symbol", "float"),
            ("last_tick_snapshot_sha256_by_symbol", "hash"),
        ):
            observed_map = prior.get(key)
            if not isinstance(observed_map, Mapping) or set(observed_map) != SYMBOL_SET:
                prior_maps_valid = False
                break
            if value_kind == "int" and any(
                isinstance(item, bool) or not isinstance(item, int) or item < 0
                for item in observed_map.values()
            ):
                prior_maps_valid = False
                break
            if value_kind == "float" and any(
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not (0.0 <= float(item) < float("inf"))
                for item in observed_map.values()
            ):
                prior_maps_valid = False
                break
            if value_kind == "hash" and any(
                not _is_sha256(item) for item in observed_map.values()
            ):
                prior_maps_valid = False
                break
    if (
        reservation.get("schema_version") != CYCLE_RESERVATION_SCHEMA_VERSION
        or reservation.get("capture_integrity_contract_sha256")
        != (expected_contract_sha256 or capture_integrity_contract_sha256())
        or reservation.get("collector_source_sha256") != MODULE_SOURCE_SHA256
        or reservation.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
        or reservation.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
        or not _is_sha256(binding[0])
        or not _is_sha256(binding[1])
        or started < _parse_utc_second(binding[2], reason).timestamp()
        or started >= _parse_utc_second(binding[3], reason).timestamp()
        or reservation.get("symbol_scope") != list(SYMBOLS)
        or reservation.get("symbol_scope_sha256")
        != canonical_sha256(list(SYMBOLS))
        or not isinstance(reservation.get("include_bars"), bool)
        or not isinstance(reservation.get("first_cycle"), bool)
        or not isinstance(prior, Mapping)
        or set(prior) != _PRIOR_MAIN_STATE_FIELDS
        or not prior_maps_valid
        or reservation.get("prior_main_state_sha256") != canonical_sha256(prior)
        or not isinstance(proof, Mapping)
        or set(proof)
        != {
            "schema_version",
            "producer_software_body_sha256",
            "repository_source",
            "deployed_source",
            "deployed_ex4",
            "runtime_or_broker_authority_derived",
            "monitor_proof_sha256",
        }
        or proof.get("schema_version")
        != "fxstack.scalp.mtvclc_producer_monitor_proof.v1"
        or proof.get("runtime_or_broker_authority_derived") is not False
        or not _is_sha256(proof_hash)
        or canonical_sha256(proof_body) != proof_hash
        or not _is_sha256(
            reservation.get("upstream_producer_software_body_sha256")
        )
        or proof.get("producer_software_body_sha256")
        != reservation.get("upstream_producer_software_body_sha256")
        or not producer_identities_valid
        or (
            reservation.get("first_cycle") is True
            and prior_source is not None
        )
        or (
            reservation.get("first_cycle") is False
            and (
                not isinstance(prior_source, Mapping)
                or not _is_sha256(prior_source.get("market_source_id"))
            )
        )
        or reservation.get("collection_only") is not True
        or reservation.get("evaluation_performed") is not False
        or reservation.get("authority_granted") is not False
        or reservation.get("activation_authorized") is not False
        or reservation.get("order_authorized") is not False
    ):
        raise CollectionRefusal(reason)
    if (
        _strict_positive_int(reservation.get("reservation_sequence"), reason)
        > SEALED_MAXIMUM_CYCLE_RESERVATIONS
    ):
        raise CollectionRefusal(reason)
    previous = str(reservation.get("previous_cycle_reservation_sha256") or "")
    if not _is_sha256(previous):
        raise CollectionRefusal(reason)
    return reservation


def _validated_attempt_failure(value: Any) -> dict[str, Any]:
    reason = "cycle_attempt_failure_invalid"
    if not isinstance(value, Mapping) or set(value) != _ATTEMPT_FAILURE_FIELDS:
        raise CollectionRefusal(reason)
    failure = dict(value)
    body = dict(failure)
    claimed = str(body.pop("failure_sha256", "")).lower()
    binding = tuple(
        str(failure.get(key) or "")
        for key in (
            "preregistration_body_sha256",
            "preregistration_artifact_sha256",
            "prospective_t0_utc_inclusive",
            "prospective_end_utc_exclusive",
        )
    )
    if (
        failure.get("schema_version") != CYCLE_ATTEMPT_FAILURE_SCHEMA_VERSION
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
        or not _is_sha256(failure.get("reservation_sha256"))
        or not _is_sha256(binding[0])
        or not _is_sha256(binding[1])
        or _parse_utc_second(binding[2], reason)
        >= _parse_utc_second(binding[3], reason)
        or failure.get("reason")
        != "unresolved_first_cycle_reservation_after_process_interruption"
        or failure.get("terminal_for_preregistration_attempt") is not True
        or failure.get("collection_only") is not True
        or any(
            failure.get(key) is not False
            for key in (
                "evaluation_performed",
                "authority_granted",
                "activation_authorized",
                "order_authorized",
            )
        )
    ):
        raise CollectionRefusal(reason)
    _strict_positive_int(failure.get("reservation_sequence"), reason)
    _positive_float(failure.get("failed_at_epoch"), reason)
    return failure


def _validated_interrupted_gap_evidence(
    value: Any,
    *,
    binding: tuple[str, str, str, str],
) -> list[dict[str, Any]]:
    reason = "interrupted_cycle_gap_evidence_invalid"
    if not isinstance(value, list):
        raise CollectionRefusal(reason)
    if not value:
        return []
    t0 = int(_parse_utc_second(binding[2], reason).timestamp())
    end = int(_parse_utc_second(binding[3], reason).timestamp())
    groups: dict[str, set[str]] = {}
    group_windows: dict[str, tuple[int, int, int, int, float]] = {}
    normalized: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _INTERRUPTED_GAP_FIELDS:
            raise CollectionRefusal(reason)
        row = dict(raw)
        reservation_hash = str(row.get("reservation_sha256") or "").lower()
        symbol = str(row.get("symbol") or "").strip().upper()
        start = _strict_positive_int(row.get("start_minute_epoch"), reason)
        finish = _strict_positive_int(row.get("end_minute_epoch"), reason)
        count = _strict_positive_int(row.get("minute_count"), reason)
        if (
            row.get("schema_version") != INTERRUPTED_CYCLE_GAP_SCHEMA_VERSION
            or not _is_sha256(reservation_hash)
            or symbol not in SYMBOL_SET
            or start % 60 != 0
            or finish % 60 != 0
            or finish < start
            or start < t0
            or finish >= end
            or count != (finish - start) // 60 + 1
            or row.get("disposition")
            != "permanent_interrupted_cycle_gap_not_backfilled"
            or row.get("baseline_eligible") is not False
        ):
            raise CollectionRefusal(reason)
        reservation_sequence = _strict_positive_int(
            row.get("reservation_sequence"), reason
        )
        recovered_at = _positive_float(row.get("recovered_at_epoch"), reason)
        window = (reservation_sequence, start, finish, count, recovered_at)
        if reservation_hash in group_windows:
            if group_windows[reservation_hash] != window:
                raise CollectionRefusal(reason)
        else:
            group_windows[reservation_hash] = window
        symbols = groups.setdefault(reservation_hash, set())
        if symbol in symbols:
            raise CollectionRefusal(reason)
        symbols.add(symbol)
        normalized.append(row)
    if any(symbols != SYMBOL_SET for symbols in groups.values()):
        raise CollectionRefusal(reason)
    return normalized


def _validated_cycle_reservation_parts(value: Any) -> list[dict[str, Any]]:
    reason = "cycle_reservation_parts_invalid"
    if not isinstance(value, list) or not value:
        raise CollectionRefusal(reason)
    normalized: list[dict[str, Any]] = []
    last_sequence = 0
    group_hash = ""
    group_count = 0
    group_index = 0
    closed: set[str] = set()
    for raw in value:
        if not isinstance(raw, Mapping) or set(raw) != _CYCLE_PART_FIELDS:
            raise CollectionRefusal(reason)
        row = dict(raw)
        sequence = _strict_positive_int(row.get("reservation_sequence"), reason)
        reservation_hash = str(row.get("reservation_sha256") or "").lower()
        index = _strict_positive_int(row.get("part_index"), reason)
        count = _strict_positive_int(row.get("part_count"), reason)
        final = row.get("final")
        if (
            not _is_sha256(reservation_hash)
            or index > count
            or not isinstance(final, bool)
            or final is not (index == count)
            or sequence > SEALED_MAXIMUM_CYCLE_RESERVATIONS
        ):
            raise CollectionRefusal(reason)
        if reservation_hash != group_hash:
            if reservation_hash in closed or sequence <= last_sequence:
                raise CollectionRefusal(reason)
            if group_hash:
                closed.add(group_hash)
            last_sequence = sequence
            group_hash = reservation_hash
            group_count = count
            group_index = index - 1
        if (
            sequence != last_sequence
            or count != group_count
            or index != group_index + 1
        ):
            raise CollectionRefusal(reason)
        group_index = index
        normalized.append(row)
    return normalized


@dataclass(frozen=True, slots=True)
class GapAnchor:
    count: int = 0
    event_count: int = 0
    root_hash: str = ZERO_SHA256
    tail_hash: str = ZERO_SHA256
    source_id: str = ""


class _GapEpochCoverage:
    """Compact exact gap membership with bounded interval insertion."""

    __slots__ = ("_ends", "_starts")

    def __init__(self) -> None:
        self._starts: list[int] = []
        self._ends: list[int] = []

    def contains(self, epoch: int) -> bool:
        from bisect import bisect_right

        position = bisect_right(self._starts, epoch) - 1
        return position >= 0 and epoch <= self._ends[position]

    def add(self, epoch: int) -> None:
        from bisect import bisect_left

        if epoch <= 0 or self.contains(epoch):
            raise CollectionRefusal("late_gap_event_duplicate")
        position = bisect_left(self._starts, epoch)
        if position > 0 and self._ends[position - 1] + 60 == epoch:
            self._ends[position - 1] = epoch
            if (
                position < len(self._starts)
                and epoch + 60 == self._starts[position]
            ):
                self._ends[position - 1] = self._ends[position]
                del self._starts[position]
                del self._ends[position]
            return
        if position < len(self._starts) and epoch + 60 == self._starts[position]:
            self._starts[position] = epoch
            return
        self._starts.insert(position, epoch)
        self._ends.insert(position, epoch)

    def add_interval(self, start_epoch: int, end_epoch: int) -> None:
        from bisect import bisect_left

        if (
            start_epoch <= 0
            or end_epoch < start_epoch
            or start_epoch % 60 != 0
            or end_epoch % 60 != 0
        ):
            raise CollectionRefusal("late_gap_interval_invalid")
        if self.contains(start_epoch) or self.contains(end_epoch):
            raise CollectionRefusal("late_gap_event_duplicate")
        position = bisect_left(self._starts, start_epoch)
        if position < len(self._starts) and self._starts[position] <= end_epoch:
            raise CollectionRefusal("late_gap_event_duplicate")
        merge_left = (
            position > 0 and self._ends[position - 1] + 60 == start_epoch
        )
        merge_right = (
            position < len(self._starts)
            and end_epoch + 60 == self._starts[position]
        )
        if merge_left and merge_right:
            self._ends[position - 1] = self._ends[position]
            del self._starts[position]
            del self._ends[position]
        elif merge_left:
            self._ends[position - 1] = end_epoch
        elif merge_right:
            self._starts[position] = start_epoch
        else:
            self._starts.insert(position, start_epoch)
            self._ends.insert(position, end_epoch)

    def union_interval(self, start_epoch: int, end_epoch: int) -> None:
        """Merge one conservative interruption interval into compact coverage."""

        from bisect import bisect_left

        if start_epoch <= 0 or end_epoch < start_epoch:
            raise CollectionRefusal("gap_epoch_invalid")
        position = bisect_left(self._starts, start_epoch)
        if position and self._ends[position - 1] + 60 >= start_epoch:
            position -= 1
            start_epoch = min(start_epoch, self._starts[position])
            end_epoch = max(end_epoch, self._ends[position])
            del self._starts[position]
            del self._ends[position]
        while position < len(self._starts) and self._starts[position] <= end_epoch + 60:
            start_epoch = min(start_epoch, self._starts[position])
            end_epoch = max(end_epoch, self._ends[position])
            del self._starts[position]
            del self._ends[position]
        self._starts.insert(position, start_epoch)
        self._ends.insert(position, end_epoch)


def _anchor_fields(anchor: GapAnchor) -> dict[str, Any]:
    return {
        "gap_chain_schema_version": LATE_GAP_RECORD_SCHEMA_VERSION,
        "gap_chain_count": anchor.count,
        "gap_event_count": anchor.event_count,
        "gap_root_sha256": anchor.root_hash,
        "gap_tail_sha256": anchor.tail_hash,
        "gap_source_id": anchor.source_id,
    }


def _anchor_from_mapping(value: Mapping[str, Any], *, reason: str) -> GapAnchor:
    count = _strict_nonnegative_int(value.get("gap_chain_count"), reason)
    event_count = _strict_nonnegative_int(value.get("gap_event_count"), reason)
    root = str(value.get("gap_root_sha256") or "").lower()
    tail = str(value.get("gap_tail_sha256") or "").lower()
    source = str(value.get("gap_source_id") or "").lower()
    if (
        value.get("gap_chain_schema_version") != LATE_GAP_RECORD_SCHEMA_VERSION
        or not _is_sha256(root)
        or not _is_sha256(tail)
        or not _is_sha256(source)
        or (count == 0 and (event_count != 0 or root != ZERO_SHA256 or tail != ZERO_SHA256))
        or (count > 0 and (event_count < count or root == ZERO_SHA256 or tail == ZERO_SHA256))
    ):
        raise CollectionRefusal(reason)
    return GapAnchor(count, event_count, root, tail, source)


def _validated_gap_event(value: Any, *, reason: str) -> tuple[str, int, int]:
    if not isinstance(value, Mapping) or set(value) != _GAP_EVENT_FIELDS:
        raise CollectionRefusal(reason)
    symbol = str(value.get("symbol") or "").strip().upper()
    epoch = _strict_positive_int(value.get("minute_epoch"), reason)
    watermark = _strict_positive_int(value.get("watermark_epoch"), reason)
    digest = str(value.get("late_payload_sha256") or "").lower()
    if (
        symbol not in SYMBOL_SET
        or epoch % 60 != 0
        or watermark % 60 != 0
        or epoch > watermark
        or not _is_sha256(digest)
        or value.get("disposition") != "permanent_gap_not_backfilled"
        or value.get("baseline_eligible") is not False
    ):
        raise CollectionRefusal(reason)
    return symbol, epoch, watermark


def _advance_gap_anchor(
    prior: GapAnchor,
    records: Sequence[Mapping[str, Any]],
    *,
    expected_source_id: str,
    binding: tuple[str, str, str, str],
    event_validator: Callable[[Any], None] | None = None,
) -> GapAnchor:
    source_id = str(expected_source_id or "").lower()
    if not _is_sha256(source_id) or (prior.source_id and prior.source_id != source_id):
        raise CollectionRefusal("gap_source_identity_invalid")
    count = prior.count
    event_count = prior.event_count
    root = prior.root_hash
    tail = prior.tail_hash
    for raw in records:
        if not isinstance(raw, Mapping) or set(raw) != _GAP_RECORD_FIELDS:
            raise CollectionRefusal("late_gap_record_scope_invalid")
        record = dict(raw)
        body = dict(record)
        claimed = str(body.pop("gap_entry_sha256", "")).lower()
        events = record.get("events")
        observed_at = _positive_float(
            record.get("observed_at_epoch"), "late_gap_record_invalid"
        )
        t0 = _parse_utc_second(binding[2], "late_gap_record_invalid").timestamp()
        end = _parse_utc_second(binding[3], "late_gap_record_invalid").timestamp()
        if (
            record.get("schema_version") != LATE_GAP_RECORD_SCHEMA_VERSION
            or record.get("gap_sequence") != count + 1
            or record.get("previous_gap_entry_sha256") != tail
            or record.get("capture_integrity_contract_sha256")
            != capture_integrity_contract_sha256()
            or record.get("collector_source_sha256") != MODULE_SOURCE_SHA256
            or record.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
            or record.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
            or tuple(record.get(key) for key in (
                "preregistration_body_sha256",
                "preregistration_artifact_sha256",
                "prospective_t0_utc_inclusive",
                "prospective_end_utc_exclusive",
            )) != binding
            or str(record.get("market_source_id") or "").lower() != source_id
            or observed_at < t0
            or observed_at >= end
            or not isinstance(events, list)
            or not events
            or not _is_sha256(claimed)
            or canonical_sha256(body) != claimed
        ):
            raise CollectionRefusal("late_gap_record_invalid")
        seen_in_record: set[tuple[str, int]] = set()
        for event in events:
            symbol, epoch, _watermark = _validated_gap_event(
                event, reason="late_gap_event_invalid"
            )
            key = (symbol, epoch)
            if key in seen_in_record:
                raise CollectionRefusal("late_gap_event_duplicate")
            seen_in_record.add(key)
            if event_validator is not None:
                event_validator(event)
        count += 1
        event_count += len(events)
        tail = claimed
        if count == 1:
            root = claimed
    return GapAnchor(count, event_count, root, tail, source_id)


# Rebind only this private support-module instance to the exact three-source
# executable identity and to the expanded portable schemas.
support.COLLECTOR_SCHEMA_VERSION = COLLECTOR_SCHEMA_VERSION
support._assert_collector_source_unchanged = _assert_collector_source_unchanged
support._verified_support_sha256 = _verified_base_support_sha256_bounded
support.collector_source_sha256 = collector_source_sha256
support.expected_capture_integrity_contract = expected_capture_integrity_contract
support._atomic_write_new = _durable_atomic_write_new
support._PORTABLE_CHUNK_FIELDS = (
    frozenset(support._PORTABLE_CHUNK_FIELDS)
    | _GAP_CYCLE_FIELDS
    | _RESERVATION_CYCLE_FIELDS
)
support._MANIFEST_FIELDS = (
    frozenset(support._MANIFEST_FIELDS)
    | _GAP_MANIFEST_FIELDS
    | _RESERVATION_MANIFEST_FIELDS
)


_TAIL_STATE_FIELDS = frozenset(
    {
        "state_kind",
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
        "gap_chain_count",
        "gap_event_count",
        "gap_root_sha256",
        "gap_tail_sha256",
        "gap_source_id",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "cycle_reservation_sequence",
        "last_cycle_reservation_sha256",
        "unresolved_cycle_reservation_sha256",
        "attempt_failure_sha256",
    }
)
_TAIL_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "registry_sequence",
        "previous_registry_entry_sha256",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "phase",
        "operation_kind",
        "operation_id",
        "previous_committed_state_sha256",
        "state",
        "prepared_manifest_entry",
        "prepared_journal_line_sha256",
        "prepared_journal_payload_zlib_base64",
        "cycle_reservation",
        "cycle_resolution",
        "attempt_failure",
        "registry_entry_sha256",
    }
)


def _genesis_tail_state() -> dict[str, Any]:
    return {
        "state_kind": "genesis",
        "manifest_sequence": 0,
        "manifest_entry_sha256": ZERO_SHA256,
        "manifest_size_bytes": 0,
        "journal_manifest_sequence_target": 0,
        "journal_sequence": 0,
        "journal_entry_sha256": ZERO_SHA256,
        "journal_size_bytes": 0,
        "covered_journal_manifest_sequence_target": 0,
        "covered_journal_sequence": 0,
        "covered_journal_entry_sha256": ZERO_SHA256,
        "covered_journal_size_bytes": 0,
        "latest_chunk_path": "",
        "latest_chunk_sha256": ZERO_SHA256,
        "latest_chunk_size_bytes": 0,
        "gap_chain_count": 0,
        "gap_event_count": 0,
        "gap_root_sha256": ZERO_SHA256,
        "gap_tail_sha256": ZERO_SHA256,
        "gap_source_id": "",
        "preregistration_body_sha256": "",
        "preregistration_artifact_sha256": "",
        "prospective_t0_utc_inclusive": "",
        "prospective_end_utc_exclusive": "",
        "cycle_reservation_sequence": 0,
        "last_cycle_reservation_sha256": ZERO_SHA256,
        "unresolved_cycle_reservation_sha256": ZERO_SHA256,
        "attempt_failure_sha256": ZERO_SHA256,
    }


def _nonnegative_int(value: Any, reason: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CollectionRefusal(reason)
    return int(value)


def _validate_tail_state(value: Any) -> dict[str, Any]:
    reason = "tail_commitment_state_invalid"
    if not isinstance(value, Mapping) or set(value) != _TAIL_STATE_FIELDS:
        raise CollectionRefusal(reason)
    state = dict(value)
    kind = state.get("state_kind")
    integer_fields = (
        "manifest_sequence",
        "manifest_size_bytes",
        "journal_manifest_sequence_target",
        "journal_sequence",
        "journal_size_bytes",
        "covered_journal_manifest_sequence_target",
        "covered_journal_sequence",
        "covered_journal_size_bytes",
        "latest_chunk_size_bytes",
        "gap_chain_count",
        "gap_event_count",
        "cycle_reservation_sequence",
    )
    numbers = {key: _nonnegative_int(state.get(key), reason) for key in integer_fields}
    hash_fields = (
        "manifest_entry_sha256",
        "journal_entry_sha256",
        "covered_journal_entry_sha256",
        "latest_chunk_sha256",
        "gap_root_sha256",
        "gap_tail_sha256",
        "last_cycle_reservation_sha256",
        "unresolved_cycle_reservation_sha256",
        "attempt_failure_sha256",
    )
    if any(not _is_sha256(str(state.get(key) or "").lower()) for key in hash_fields):
        raise CollectionRefusal(reason)
    manifest_sequence = numbers["manifest_sequence"]
    manifest_size = numbers["manifest_size_bytes"]
    journal_target = numbers["journal_manifest_sequence_target"]
    journal_sequence = numbers["journal_sequence"]
    journal_size = numbers["journal_size_bytes"]
    covered_target = numbers["covered_journal_manifest_sequence_target"]
    covered_sequence = numbers["covered_journal_sequence"]
    covered_size = numbers["covered_journal_size_bytes"]
    chunk_size = numbers["latest_chunk_size_bytes"]
    count = numbers["gap_chain_count"]
    event_count = numbers["gap_event_count"]
    manifest_hash = str(state["manifest_entry_sha256"]).lower()
    journal_hash = str(state["journal_entry_sha256"]).lower()
    covered_hash = str(state["covered_journal_entry_sha256"]).lower()
    chunk_hash = str(state["latest_chunk_sha256"]).lower()
    root_hash = str(state["gap_root_sha256"]).lower()
    tail_hash = str(state["gap_tail_sha256"]).lower()
    source_id = str(state.get("gap_source_id") or "").lower()
    binding = tuple(
        str(state.get(key) or "")
        for key in (
            "preregistration_body_sha256",
            "preregistration_artifact_sha256",
            "prospective_t0_utc_inclusive",
            "prospective_end_utc_exclusive",
        )
    )
    reservation_sequence = numbers["cycle_reservation_sequence"]
    last_reservation = str(state["last_cycle_reservation_sha256"]).lower()
    unresolved_reservation = str(
        state["unresolved_cycle_reservation_sha256"]
    ).lower()
    attempt_failure = str(state["attempt_failure_sha256"]).lower()
    if (
        (reservation_sequence == 0 and last_reservation != ZERO_SHA256)
        or (reservation_sequence > 0 and last_reservation == ZERO_SHA256)
        or (
            unresolved_reservation != ZERO_SHA256
            and unresolved_reservation != last_reservation
        )
        or (attempt_failure != ZERO_SHA256 and unresolved_reservation != ZERO_SHA256)
    ):
        raise CollectionRefusal(reason)
    if kind == "genesis":
        genesis = _genesis_tail_state()
        reservation_fields = {
            "cycle_reservation_sequence",
            "last_cycle_reservation_sha256",
            "unresolved_cycle_reservation_sha256",
            "attempt_failure_sha256",
        }
        if any(
            state[key] != genesis[key]
            for key in _TAIL_STATE_FIELDS - reservation_fields
        ):
            raise CollectionRefusal(reason)
        return state
    if kind not in {"journal", "manifest"}:
        raise CollectionRefusal(reason)
    if (
        not _is_sha256(binding[0])
        or not _is_sha256(binding[1])
        or _parse_utc_second(binding[2], reason) >= _parse_utc_second(binding[3], reason)
        or not _is_sha256(source_id)
        or event_count < count
        or (count == 0 and (root_hash != ZERO_SHA256 or tail_hash != ZERO_SHA256))
        or (count > 0 and (root_hash == ZERO_SHA256 or tail_hash == ZERO_SHA256))
        or manifest_sequence > MAXIMUM_MANIFEST_ENTRIES
        or manifest_size > MAXIMUM_MANIFEST_BYTES
        or journal_sequence > MAXIMUM_ACTIVE_JOURNAL_RECORDS
        or journal_size > MAXIMUM_ACTIVE_JOURNAL_BYTES
        or covered_sequence > MAXIMUM_ACTIVE_JOURNAL_RECORDS
        or covered_size > MAXIMUM_ACTIVE_JOURNAL_BYTES
        or chunk_size > MAXIMUM_CHUNK_BYTES
    ):
        raise CollectionRefusal(reason)
    if manifest_sequence == 0:
        if (
            manifest_hash != ZERO_SHA256
            or manifest_size != 0
            or state.get("latest_chunk_path") != ""
            or chunk_hash != ZERO_SHA256
            or chunk_size != 0
        ):
            raise CollectionRefusal(reason)
    elif (
        manifest_hash == ZERO_SHA256
        or manifest_size <= 0
        or not str(state.get("latest_chunk_path") or "")
        or chunk_hash == ZERO_SHA256
        or chunk_size <= 0
    ):
        raise CollectionRefusal(reason)
    if kind == "journal":
        if (
            journal_target != manifest_sequence + 1
            or journal_sequence <= 0
            or journal_hash == ZERO_SHA256
            or journal_size <= 0
            or covered_target != 0
            or covered_sequence != 0
            or covered_hash != ZERO_SHA256
            or covered_size != 0
        ):
            raise CollectionRefusal(reason)
    elif (
        manifest_sequence <= 0
        or journal_target != 0
        or journal_sequence != 0
        or journal_hash != ZERO_SHA256
        or journal_size != 0
        or covered_target != manifest_sequence
        or covered_sequence <= 0
        or covered_hash == ZERO_SHA256
        or covered_size <= 0
    ):
        raise CollectionRefusal(reason)
    return state


def _validate_tail_transition_values(
    previous: Mapping[str, Any],
    state_value: Mapping[str, Any],
    operation_kind: str,
) -> dict[str, Any]:
    state = _validate_tail_state(state_value)
    stable_manifest_fields = (
        "manifest_sequence",
        "manifest_entry_sha256",
        "manifest_size_bytes",
        "latest_chunk_path",
        "latest_chunk_sha256",
        "latest_chunk_size_bytes",
    )
    binding_fields = (
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
    )
    reservation_stable_fields = (
        "cycle_reservation_sequence",
        "last_cycle_reservation_sha256",
        "attempt_failure_sha256",
    )
    if operation_kind == "journal_append":
        if (
            state["state_kind"] != "journal"
            or any(state[key] != previous[key] for key in stable_manifest_fields)
            or any(
                state[key] != previous[key]
                for key in reservation_stable_fields
            )
            or state["unresolved_cycle_reservation_sha256"]
            not in {
                previous["unresolved_cycle_reservation_sha256"],
                ZERO_SHA256,
            }
        ):
            raise CollectionRefusal("tail_commitment_transition_invalid")
        if previous["state_kind"] == "journal":
            if (
                state["journal_manifest_sequence_target"]
                != previous["journal_manifest_sequence_target"]
                or state["journal_sequence"] != previous["journal_sequence"] + 1
                or state["journal_size_bytes"] <= previous["journal_size_bytes"]
                or any(state[key] != previous[key] for key in binding_fields)
            ):
                raise CollectionRefusal("tail_commitment_transition_invalid")
        elif (
            state["journal_sequence"] != 1
            or state["journal_manifest_sequence_target"]
            != previous["manifest_sequence"] + 1
        ):
            raise CollectionRefusal("tail_commitment_transition_invalid")
    elif operation_kind == "manifest_finalize":
        if (
            previous["state_kind"] != "journal"
            or state["state_kind"] != "manifest"
            or state["manifest_sequence"] != previous["manifest_sequence"] + 1
            or state["manifest_size_bytes"] <= previous["manifest_size_bytes"]
            or state["covered_journal_manifest_sequence_target"]
            != previous["journal_manifest_sequence_target"]
            or state["covered_journal_sequence"] != previous["journal_sequence"]
            or state["covered_journal_entry_sha256"]
            != previous["journal_entry_sha256"]
            or state["covered_journal_size_bytes"]
            != previous["journal_size_bytes"]
            or any(state[key] != previous[key] for key in binding_fields)
            or any(
                state[key] != previous[key]
                for key in (
                    *reservation_stable_fields,
                    "unresolved_cycle_reservation_sha256",
                )
            )
            or any(
                state[key] != previous[key]
                for key in (
                    "gap_chain_count",
                    "gap_event_count",
                    "gap_root_sha256",
                    "gap_tail_sha256",
                    "gap_source_id",
                )
            )
        ):
            raise CollectionRefusal("tail_commitment_transition_invalid")
    else:
        raise CollectionRefusal("tail_commitment_transition_invalid")
    return state


def _tail_operation_id(
    *,
    operation_kind: str,
    previous_state_sha256: str,
    state: Mapping[str, Any],
    prepared_manifest_entry: Mapping[str, Any] | None,
    prepared_journal_line_sha256: str,
    prepared_journal_payload_zlib_base64: str,
    cycle_reservation: Mapping[str, Any] | None,
    cycle_resolution: Mapping[str, Any] | None,
    attempt_failure: Mapping[str, Any] | None,
) -> str:
    return canonical_sha256(
        {
            "operation_kind": operation_kind,
            "previous_committed_state_sha256": previous_state_sha256,
            "state": dict(state),
            "prepared_manifest_entry": (
                dict(prepared_manifest_entry)
                if prepared_manifest_entry is not None
                else None
            ),
            "prepared_journal_line_sha256": prepared_journal_line_sha256,
            "prepared_journal_payload_zlib_base64": (
                prepared_journal_payload_zlib_base64
            ),
            "cycle_reservation": (
                dict(cycle_reservation) if cycle_reservation is not None else None
            ),
            "cycle_resolution": (
                dict(cycle_resolution) if cycle_resolution is not None else None
            ),
            "attempt_failure": (
                dict(attempt_failure) if attempt_failure is not None else None
            ),
        }
    )


def _compress_prepared_journal_line(line: bytes) -> str:
    return base64.b64encode(zlib.compress(line, level=9)).decode("ascii")


def _decompress_prepared_journal_line(value: str) -> bytes:
    reason = "tail_commitment_prepared_journal_invalid"
    try:
        compressed = base64.b64decode(value.encode("ascii"), validate=True)
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(
            compressed, support.MAXIMUM_JOURNAL_LINE_BYTES + 1
        )
    except (UnicodeEncodeError, ValueError, zlib.error) as exc:
        raise CollectionRefusal(reason) from exc
    if (
        len(raw) > support.MAXIMUM_JOURNAL_LINE_BYTES
        or not decompressor.eof
        or decompressor.unconsumed_tail
        or decompressor.unused_data
        or not raw.endswith(b"\n")
    ):
        raise CollectionRefusal(reason)
    return raw


@dataclass(frozen=True, slots=True)
class _TailRegistryView:
    record_count: int
    head_sha256: str
    tail_sha256: str
    committed_state: dict[str, Any]
    pending_record: dict[str, Any] | None
    last_cycle_reservation: dict[str, Any] | None
    attempt_failure: dict[str, Any] | None
    size_bytes: int
    artifact_sha256: str
    stat_identity: tuple[int, int, int, int, int]


def _stream_tail_commitment_registry(path: Path) -> _TailRegistryView:
    reason = "tail_commitment_invalid"
    expected_contract_sha256 = capture_integrity_contract_sha256()
    parent_identities = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=reason
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(path)
        if (
            _is_reparse_or_symlink(path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > MAXIMUM_TAIL_COMMITMENT_BYTES
        ):
            raise CollectionRefusal(reason)
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CollectionRefusal("tail_commitment_missing") from exc
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    prior_hash = ZERO_SHA256
    head_hash = ""
    committed_state: dict[str, Any] | None = None
    pending: dict[str, Any] | None = None
    last_cycle_reservation: dict[str, Any] | None = None
    attempt_failure: dict[str, Any] | None = None
    count = 0
    total = 0
    artifact_digest = hashlib.sha256()
    try:
        before_handle = os.fstat(descriptor)
        if _immutable_stat_identity(before_path) != _immutable_stat_identity(
            before_handle
        ):
            raise CollectionRefusal(reason)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while True:
                raw_line = handle.readline(MAXIMUM_TAIL_COMMITMENT_LINE_BYTES + 1)
                if not raw_line:
                    break
                count += 1
                total += len(raw_line)
                artifact_digest.update(raw_line)
                if (
                    count > MAXIMUM_TAIL_COMMITMENT_RECORDS
                    or len(raw_line) > MAXIMUM_TAIL_COMMITMENT_LINE_BYTES
                    or not raw_line.endswith(b"\n")
                    or raw_line == b"\n"
                    or total > MAXIMUM_TAIL_COMMITMENT_BYTES
                ):
                    raise CollectionRefusal(reason)
                parsed = _strict_json_object(raw_line, reason=reason)
                if (
                    set(parsed) != _TAIL_RECORD_FIELDS
                    or raw_line != canonical_json_bytes(parsed) + b"\n"
                ):
                    raise CollectionRefusal(reason)
                body = dict(parsed)
                claimed = str(body.pop("registry_entry_sha256", "")).lower()
                sequence = _strict_positive_int(
                    parsed.get("registry_sequence"), reason
                )
                state = _validate_tail_state(parsed.get("state"))
                manifest_entry = parsed.get("prepared_manifest_entry")
                if manifest_entry is not None and not isinstance(manifest_entry, Mapping):
                    raise CollectionRefusal(reason)
                cycle_reservation_value = parsed.get("cycle_reservation")
                cycle_resolution = parsed.get("cycle_resolution")
                attempt_failure_value = parsed.get("attempt_failure")
                cycle_reservation = (
                    _validated_cycle_reservation(
                        cycle_reservation_value,
                        expected_contract_sha256=expected_contract_sha256,
                    )
                    if cycle_reservation_value is not None
                    else None
                )
                failure = (
                    _validated_attempt_failure(attempt_failure_value)
                    if attempt_failure_value is not None
                    else None
                )
                if cycle_resolution is not None:
                    # Resolution is inseparable from the fsynced final journal
                    # commit; a tail-only resolution object is never authority.
                    raise CollectionRefusal(reason)
                prepared_journal_hash = str(
                    parsed.get("prepared_journal_line_sha256") or ""
                ).lower()
                prepared_journal_payload = parsed.get(
                    "prepared_journal_payload_zlib_base64"
                )
                previous_state_hash = str(
                    parsed.get("previous_committed_state_sha256") or ""
                ).lower()
                operation_kind = str(parsed.get("operation_kind") or "")
                phase = str(parsed.get("phase") or "")
                operation_id = str(parsed.get("operation_id") or "").lower()
                if (
                    parsed.get("schema_version") != TAIL_COMMITMENT_SCHEMA_VERSION
                    or sequence != count
                    or parsed.get("previous_registry_entry_sha256") != prior_hash
                    or parsed.get("capture_integrity_contract_sha256")
                    != expected_contract_sha256
                    or parsed.get("collector_source_sha256") != MODULE_SOURCE_SHA256
                    or parsed.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
                    or parsed.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
                    or not _is_sha256(claimed)
                    or canonical_sha256(body) != claimed
                    or not _is_sha256(previous_state_hash)
                    or not _is_sha256(prepared_journal_hash)
                    or not _is_sha256(operation_id)
                    or not isinstance(prepared_journal_payload, str)
                ):
                    raise CollectionRefusal(reason)
                if count == 1:
                    if (
                        phase != "commit"
                        or operation_kind != "genesis"
                        or previous_state_hash != ZERO_SHA256
                        or state != _genesis_tail_state()
                        or manifest_entry is not None
                        or prepared_journal_hash != ZERO_SHA256
                        or prepared_journal_payload != ""
                        or cycle_reservation is not None
                        or failure is not None
                    ):
                        raise CollectionRefusal(reason)
                    expected_operation = _tail_operation_id(
                        operation_kind=operation_kind,
                        previous_state_sha256=previous_state_hash,
                        state=state,
                        prepared_manifest_entry=None,
                        prepared_journal_line_sha256=prepared_journal_hash,
                        prepared_journal_payload_zlib_base64="",
                        cycle_reservation=None,
                        cycle_resolution=None,
                        attempt_failure=None,
                    )
                    if operation_id != expected_operation:
                        raise CollectionRefusal(reason)
                    committed_state = state
                else:
                    if committed_state is None:
                        raise CollectionRefusal(reason)
                    committed_hash = canonical_sha256(committed_state)
                    if phase == "event":
                        if (
                            pending is not None
                            or previous_state_hash != committed_hash
                            or manifest_entry is not None
                            or prepared_journal_hash != ZERO_SHA256
                            or prepared_journal_payload != ""
                        ):
                            raise CollectionRefusal(reason)
                        expected_operation = _tail_operation_id(
                            operation_kind=operation_kind,
                            previous_state_sha256=previous_state_hash,
                            state=state,
                            prepared_manifest_entry=None,
                            prepared_journal_line_sha256=ZERO_SHA256,
                            prepared_journal_payload_zlib_base64="",
                            cycle_reservation=cycle_reservation,
                            cycle_resolution=None,
                            attempt_failure=failure,
                        )
                        if operation_id != expected_operation:
                            raise CollectionRefusal(reason)
                        if operation_kind == "cycle_reserve":
                            if cycle_reservation is None or failure is not None:
                                raise CollectionRefusal(reason)
                            reservation_sequence = int(
                                cycle_reservation["reservation_sequence"]
                            )
                            reservation_hash = str(
                                cycle_reservation["reservation_sha256"]
                            )
                            previous_reservation_hash = str(
                                cycle_reservation[
                                    "previous_cycle_reservation_sha256"
                                ]
                            )
                            prior_started = (
                                float(last_cycle_reservation[
                                    "cycle_started_at_epoch"
                                ])
                                if last_cycle_reservation is not None
                                else None
                            )
                            prior_main = cycle_reservation["prior_main_state"]
                            expected_state = dict(committed_state)
                            expected_state.update(
                                {
                                    "cycle_reservation_sequence": reservation_sequence,
                                    "last_cycle_reservation_sha256": reservation_hash,
                                    "unresolved_cycle_reservation_sha256": reservation_hash,
                                }
                            )
                            if (
                                committed_state["attempt_failure_sha256"]
                                != ZERO_SHA256
                                or committed_state[
                                    "unresolved_cycle_reservation_sha256"
                                ]
                                != ZERO_SHA256
                                or reservation_sequence
                                != int(committed_state[
                                    "cycle_reservation_sequence"
                                ])
                                + 1
                                or reservation_sequence
                                > SEALED_MAXIMUM_CYCLE_RESERVATIONS
                                or previous_reservation_hash
                                != committed_state[
                                    "last_cycle_reservation_sha256"
                                ]
                                or prior_main["tail_registry_sequence"]
                                != count - 1
                                or prior_main["tail_entry_sha256"] != prior_hash
                                or prior_main["tail_committed_state_sha256"]
                                != committed_hash
                                or any(
                                    prior_main[key] != committed_state[key]
                                    for key in (
                                        "manifest_sequence",
                                        "manifest_entry_sha256",
                                        "manifest_size_bytes",
                                        "journal_manifest_sequence_target",
                                        "journal_sequence",
                                        "journal_entry_sha256",
                                        "journal_size_bytes",
                                        "gap_chain_count",
                                        "gap_event_count",
                                        "gap_root_sha256",
                                        "gap_tail_sha256",
                                        "gap_source_id",
                                    )
                                )
                                or (
                                    prior_started is not None
                                    and float(cycle_reservation[
                                        "cycle_started_at_epoch"
                                    ])
                                    < prior_started
                                    + MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS
                                )
                                or state != expected_state
                            ):
                                raise CollectionRefusal(reason)
                            last_cycle_reservation = cycle_reservation
                        elif operation_kind == "attempt_fail":
                            if cycle_reservation is not None or failure is None:
                                raise CollectionRefusal(reason)
                            expected_state = dict(committed_state)
                            expected_state.update(
                                {
                                    "unresolved_cycle_reservation_sha256": ZERO_SHA256,
                                    "attempt_failure_sha256": failure[
                                        "failure_sha256"
                                    ],
                                }
                            )
                            if (
                                committed_state[
                                    "unresolved_cycle_reservation_sha256"
                                ]
                                == ZERO_SHA256
                                or failure["reservation_sha256"]
                                != committed_state[
                                    "unresolved_cycle_reservation_sha256"
                                ]
                                or failure["reservation_sequence"]
                                != committed_state["cycle_reservation_sequence"]
                                or last_cycle_reservation is None
                                or any(
                                    failure[key] != last_cycle_reservation[key]
                                    for key in (
                                        "preregistration_body_sha256",
                                        "preregistration_artifact_sha256",
                                        "prospective_t0_utc_inclusive",
                                        "prospective_end_utc_exclusive",
                                    )
                                )
                                or state != expected_state
                            ):
                                raise CollectionRefusal(reason)
                            attempt_failure = failure
                        else:
                            raise CollectionRefusal(reason)
                        committed_state = state
                    elif phase == "prepare":
                        if (
                            pending is not None
                            or operation_kind not in {"journal_append", "manifest_finalize"}
                            or previous_state_hash != committed_hash
                            or cycle_reservation is not None
                            or failure is not None
                        ):
                            raise CollectionRefusal(reason)
                        _validate_tail_transition_values(
                            committed_state,
                            state,
                            operation_kind,
                        )
                        if operation_kind == "journal_append":
                            if (
                                manifest_entry is not None
                                or prepared_journal_hash == ZERO_SHA256
                                or not prepared_journal_payload
                            ):
                                raise CollectionRefusal(reason)
                            prepared_line = _decompress_prepared_journal_line(
                                prepared_journal_payload
                            )
                            prepared_wrapper = _strict_json_object(
                                prepared_line,
                                reason="tail_commitment_prepared_journal_invalid",
                            )
                            prepared_cycle = prepared_wrapper.get("cycle")
                            if not isinstance(prepared_cycle, Mapping):
                                raise CollectionRefusal(reason)
                            final_part = prepared_cycle.get("cycle_part_final")
                            current_reservation = committed_state[
                                "unresolved_cycle_reservation_sha256"
                            ]
                            if (
                                current_reservation == ZERO_SHA256
                                or prepared_cycle.get("cycle_reservation_sha256")
                                != current_reservation
                                or prepared_cycle.get(
                                    "cycle_reservation_sequence"
                                )
                                != committed_state["cycle_reservation_sequence"]
                                or not isinstance(final_part, bool)
                                or (
                                    state[
                                        "unresolved_cycle_reservation_sha256"
                                    ]
                                    == ZERO_SHA256
                                )
                                is not final_part
                            ):
                                raise CollectionRefusal(reason)
                            prior_journal_size = (
                                int(committed_state["journal_size_bytes"])
                                if committed_state["state_kind"] == "journal"
                                else 0
                            )
                            if (
                                hashlib.sha256(prepared_line).hexdigest()
                                != prepared_journal_hash
                                or len(prepared_line)
                                != int(state["journal_size_bytes"])
                                - prior_journal_size
                            ):
                                raise CollectionRefusal(reason)
                        elif (
                            not isinstance(manifest_entry, Mapping)
                            or prepared_journal_hash != ZERO_SHA256
                            or prepared_journal_payload != ""
                        ):
                            raise CollectionRefusal(reason)
                        expected_operation = _tail_operation_id(
                            operation_kind=operation_kind,
                            previous_state_sha256=previous_state_hash,
                            state=state,
                            prepared_manifest_entry=(
                                dict(manifest_entry)
                                if isinstance(manifest_entry, Mapping)
                                else None
                            ),
                            prepared_journal_line_sha256=prepared_journal_hash,
                            prepared_journal_payload_zlib_base64=(
                                prepared_journal_payload
                            ),
                            cycle_reservation=None,
                            cycle_resolution=None,
                            attempt_failure=None,
                        )
                        if operation_id != expected_operation:
                            raise CollectionRefusal(reason)
                        pending = dict(parsed)
                    elif phase in {"commit", "abort"}:
                        if (
                            pending is None
                            or parsed.get("operation_id") != pending.get("operation_id")
                            or operation_kind != pending.get("operation_kind")
                            or previous_state_hash
                            != pending.get("previous_committed_state_sha256")
                            or manifest_entry is not None
                            or prepared_journal_hash != ZERO_SHA256
                            or prepared_journal_payload != ""
                            or cycle_reservation is not None
                            or failure is not None
                        ):
                            raise CollectionRefusal(reason)
                        if state != pending.get("state"):
                            raise CollectionRefusal(reason)
                        if phase == "commit":
                            committed_state = state
                        pending = None
                    else:
                        raise CollectionRefusal(reason)
                prior_hash = claimed
                if not head_hash:
                    head_hash = claimed
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    identities = {
        _immutable_stat_identity(before_path),
        _immutable_stat_identity(before_handle),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if (
        count <= 0
        or committed_state is None
        or total != before_handle.st_size
        or len(identities) != 1
    ):
        raise CollectionRefusal(reason)
    _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
    return _TailRegistryView(
        record_count=count,
        head_sha256=head_hash,
        tail_sha256=prior_hash,
        committed_state=committed_state,
        pending_record=pending,
        last_cycle_reservation=last_cycle_reservation,
        attempt_failure=attempt_failure,
        size_bytes=total,
        artifact_sha256=artifact_digest.hexdigest(),
        stat_identity=identities.pop(),
    )


@dataclass(frozen=True, slots=True)
class _ManifestProjection:
    sequence: int
    entry_sha256: str
    size_bytes: int
    last_entry: dict[str, Any] | None
    stat_identity: tuple[int, int, int, int, int] | None
    chunk_stat_identities: dict[str, tuple[int, int, int, int, int]]


@dataclass(frozen=True, slots=True)
class _JournalProjection:
    target: int
    sequence: int
    entry_sha256: str
    size_bytes: int
    previous_manifest_sha256: str
    stat_identity: tuple[int, int, int, int, int] | None


def _journal_projection_from_complete_bytes(raw: bytes) -> _JournalProjection:
    """Validate complete canonical journal bytes without touching a pathname."""

    if not raw:
        return _JournalProjection(0, 0, ZERO_SHA256, 0, ZERO_SHA256, None)
    reason = "active_journal_invalid"
    if len(raw) > MAXIMUM_ACTIVE_JOURNAL_BYTES or not raw.endswith(b"\n"):
        raise CollectionRefusal(reason)
    prior_hash = ZERO_SHA256
    target = 0
    previous_manifest = ""
    lines = raw.splitlines(keepends=True)
    if len(lines) > MAXIMUM_ACTIVE_JOURNAL_RECORDS:
        raise CollectionRefusal(reason)
    for expected_sequence, raw_line in enumerate(lines, start=1):
        if (
            len(raw_line) > support.MAXIMUM_JOURNAL_LINE_BYTES
            or not raw_line.endswith(b"\n")
            or raw_line == b"\n"
        ):
            raise CollectionRefusal(reason)
        parsed = _strict_json_object(raw_line, reason=reason)
        if (
            set(parsed) != support._JOURNAL_RECORD_FIELDS
            or raw_line != canonical_json_bytes(parsed) + b"\n"
        ):
            raise CollectionRefusal(reason)
        body = dict(parsed)
        claimed = str(body.pop("journal_entry_sha256", "")).lower()
        sequence = _strict_positive_int(
            parsed.get("journal_sequence"), reason
        )
        observed_target = _strict_positive_int(
            parsed.get("manifest_sequence_target"), reason
        )
        observed_previous = str(
            parsed.get("previous_manifest_entry_sha256") or ""
        ).lower()
        if (
            sequence != expected_sequence
            or parsed.get("previous_journal_entry_sha256") != prior_hash
            or not _is_sha256(observed_previous)
            or not _is_sha256(claimed)
            or canonical_sha256(body) != claimed
        ):
            raise CollectionRefusal(reason)
        if expected_sequence == 1:
            target = observed_target
            previous_manifest = observed_previous
        elif observed_target != target or observed_previous != previous_manifest:
            raise CollectionRefusal(reason)
        prior_hash = claimed
    return _JournalProjection(
        target,
        len(lines),
        prior_hash,
        len(raw),
        previous_manifest,
        None,
    )


def _stream_manifest_projection(root: Path) -> _ManifestProjection:
    path = root / MANIFEST_FILENAME
    if not _path_lexists(path):
        return _ManifestProjection(0, ZERO_SHA256, 0, None, None, {})
    reason = "manifest_invalid"
    parent_identities = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=reason
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(path)
        if (
            _is_reparse_or_symlink(path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > MAXIMUM_MANIFEST_BYTES
        ):
            raise CollectionRefusal("manifest_size_limit_exceeded")
        descriptor = os.open(path, flags)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    prior_hash = ZERO_SHA256
    count = 0
    total = 0
    last_entry: dict[str, Any] | None = None
    chunk_stat_identities: dict[str, tuple[int, int, int, int, int]] = {}
    try:
        before_handle = os.fstat(descriptor)
        if _immutable_stat_identity(before_path) != _immutable_stat_identity(
            before_handle
        ):
            raise CollectionRefusal(reason)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while True:
                raw_line = handle.readline(MAXIMUM_MANIFEST_LINE_BYTES + 1)
                if not raw_line:
                    break
                count += 1
                total += len(raw_line)
                if (
                    count > MAXIMUM_MANIFEST_ENTRIES
                    or len(raw_line) > MAXIMUM_MANIFEST_LINE_BYTES
                    or not raw_line.endswith(b"\n")
                    or raw_line == b"\n"
                    or total > MAXIMUM_MANIFEST_BYTES
                ):
                    raise CollectionRefusal("manifest_size_limit_exceeded")
                parsed = _strict_json_object(raw_line, reason=reason)
                if (
                    set(parsed) != support._MANIFEST_FIELDS
                    or raw_line != canonical_json_bytes(parsed) + b"\n"
                ):
                    raise CollectionRefusal(reason)
                entry_body = dict(parsed)
                claimed = str(entry_body.pop("manifest_entry_sha256", "")).lower()
                sequence = _strict_positive_int(parsed.get("sequence"), reason)
                utc_hour = str(parsed.get("utc_hour") or "")
                segment = _strict_positive_int(parsed.get("segment_index"), reason)
                expected_relative = PurePosixPath(
                    support.CHUNK_DIRECTORY,
                    utc_hour,
                    f"ig-mt4-m1-activity-s{segment:04d}-q{sequence:010d}.json",
                ).as_posix()
                relative = str(parsed.get("chunk_path") or "")
                chunk_size = _nonnegative_int(parsed.get("chunk_size_bytes"), reason)
                if (
                    sequence != count
                    or parsed.get("previous_entry_sha256") != prior_hash
                    or segment != 1
                    or relative != expected_relative
                    or not _is_sha256(claimed)
                    or canonical_sha256(entry_body) != claimed
                    or chunk_size <= 0
                    or chunk_size > MAXIMUM_CHUNK_BYTES
                ):
                    raise CollectionRefusal(reason)
                chunk_path = root.joinpath(*PurePosixPath(relative).parts)
                chunk_parent = _secure_directory_chain(
                    chunk_path.parent,
                    allow_missing_tail=False,
                    reason="manifest_chunk_path_invalid",
                )
                try:
                    chunk_stat = os.lstat(chunk_path)
                except OSError as exc:
                    raise CollectionRefusal("manifest_chunk_missing") from exc
                if (
                    _is_reparse_or_symlink(chunk_path)
                    or not stat.S_ISREG(chunk_stat.st_mode)
                    or chunk_stat.st_size != chunk_size
                    or chunk_stat.st_size > MAXIMUM_CHUNK_BYTES
                ):
                    raise CollectionRefusal("manifest_chunk_size_limit_exceeded")
                chunk_stat_identities[relative] = _immutable_stat_identity(
                    chunk_stat
                )
                _assert_secure_parent_unchanged(
                    chunk_path,
                    chunk_parent,
                    reason="manifest_chunk_path_invalid",
                )
                prior_hash = claimed
                last_entry = dict(parsed)
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    identities = {
        _immutable_stat_identity(before_path),
        _immutable_stat_identity(before_handle),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if count <= 0 or total != before_handle.st_size or len(identities) != 1:
        raise CollectionRefusal(reason)
    _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
    return _ManifestProjection(
        count,
        prior_hash,
        total,
        last_entry,
        identities.pop(),
        chunk_stat_identities,
    )


def _stream_journal_projection(root: Path) -> _JournalProjection:
    path = root / ACTIVE_JOURNAL_FILENAME
    if not _path_lexists(path):
        return _JournalProjection(0, 0, ZERO_SHA256, 0, ZERO_SHA256, None)
    reason = "active_journal_invalid"
    parent_identities = _secure_directory_chain(
        path.parent, allow_missing_tail=False, reason=reason
    )
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = os.lstat(path)
        if (
            _is_reparse_or_symlink(path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > MAXIMUM_ACTIVE_JOURNAL_BYTES
        ):
            raise CollectionRefusal("active_journal_size_limit_exceeded")
        descriptor = os.open(path, flags)
    except CollectionRefusal:
        raise
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    prior_hash = ZERO_SHA256
    target = 0
    previous_manifest = ""
    count = 0
    total = 0
    try:
        before_handle = os.fstat(descriptor)
        if _immutable_stat_identity(before_path) != _immutable_stat_identity(
            before_handle
        ):
            raise CollectionRefusal(reason)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            while True:
                raw_line = handle.readline(support.MAXIMUM_JOURNAL_LINE_BYTES + 1)
                if not raw_line:
                    break
                count += 1
                total += len(raw_line)
                if (
                    count > MAXIMUM_ACTIVE_JOURNAL_RECORDS
                    or len(raw_line) > support.MAXIMUM_JOURNAL_LINE_BYTES
                    or not raw_line.endswith(b"\n")
                    or raw_line == b"\n"
                    or total > MAXIMUM_ACTIVE_JOURNAL_BYTES
                ):
                    raise CollectionRefusal("active_journal_size_limit_exceeded")
                parsed = _strict_json_object(raw_line, reason=reason)
                if (
                    set(parsed) != support._JOURNAL_RECORD_FIELDS
                    or raw_line != canonical_json_bytes(parsed) + b"\n"
                ):
                    raise CollectionRefusal(reason)
                body = dict(parsed)
                claimed = str(body.pop("journal_entry_sha256", "")).lower()
                sequence = _strict_positive_int(parsed.get("journal_sequence"), reason)
                observed_target = _strict_positive_int(
                    parsed.get("manifest_sequence_target"), reason
                )
                observed_previous = str(
                    parsed.get("previous_manifest_entry_sha256") or ""
                ).lower()
                if (
                    sequence != count
                    or parsed.get("previous_journal_entry_sha256") != prior_hash
                    or not _is_sha256(observed_previous)
                    or not _is_sha256(claimed)
                    or canonical_sha256(body) != claimed
                ):
                    raise CollectionRefusal(reason)
                if count == 1:
                    target = observed_target
                    previous_manifest = observed_previous
                elif observed_target != target or observed_previous != previous_manifest:
                    raise CollectionRefusal(reason)
                prior_hash = claimed
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = os.lstat(path)
    except OSError as exc:
        raise CollectionRefusal(reason) from exc
    identities = {
        _immutable_stat_identity(before_path),
        _immutable_stat_identity(before_handle),
        _immutable_stat_identity(after_handle),
        _immutable_stat_identity(after_path),
    }
    if count <= 0 or total != before_handle.st_size or len(identities) != 1:
        raise CollectionRefusal(reason)
    _assert_secure_parent_unchanged(path, parent_identities, reason=reason)
    return _JournalProjection(
        target,
        count,
        prior_hash,
        total,
        previous_manifest,
        identities.pop(),
    )


class ManifestLedger(support.ManifestLedger):
    """Main manifest ledger that also owns the permanent-gap chain."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        writer_lock: ExclusiveDataWriterLock,
    ) -> None:
        if not writer_lock.authorizes(output_root):
            raise CollectionRefusal("exclusive_data_writer_lock_required")
        supplied_root = Path(os.path.abspath(Path(output_root).expanduser()))
        root = _absolute_existing_path_without_reparse(
            supplied_root, reason="output_root_reparse_or_identity_invalid"
        )
        if os.path.normcase(str(supplied_root)) != os.path.normcase(str(root)):
            raise CollectionRefusal("output_root_reparse_or_identity_invalid")
        root_chain = _secure_directory_chain(
            root, allow_missing_tail=False, reason="output_root_reparse_or_identity_invalid"
        )
        chunks_root = root / support.CHUNK_DIRECTORY
        chunks_chain = _secure_parent_for_write(
            chunks_root / ".identity-probe",
            reason="chunk_directory_reparse_or_identity_invalid",
        )
        self.root = root
        self.chunks_root = chunks_root
        self.manifest_path = root / MANIFEST_FILENAME
        self.manifest_staging_path = root / f".{MANIFEST_FILENAME}.next"
        self.journal_path = root / ACTIVE_JOURNAL_FILENAME
        self.tail_commitment_path = root / TAIL_COMMITMENT_FILENAME
        self._root_directory_identity = root_chain[os.path.normcase(str(root))]
        self._chunks_directory_identity = chunks_chain[
            os.path.normcase(str(chunks_root))
        ]
        self.gap_anchor = GapAnchor()
        self.gap_epoch_coverage_by_symbol = {
            symbol: _GapEpochCoverage() for symbol in SYMBOLS
        }
        self.interrupted_gap_epoch_coverage_by_symbol = {
            symbol: _GapEpochCoverage() for symbol in SYMBOLS
        }
        self._reservation_parts_by_hash: dict[str, list[dict[str, Any]]] = {}
        self._last_source_mapping: dict[str, Any] | None = None
        self._journal_descriptor: int | None = None
        self._journal_expected_size = 0
        self._journal_stat_identity: tuple[int, int, int, int, int] | None = None
        self._manifest_expected_size = 0
        self._manifest_stat_identity: tuple[int, int, int, int, int] | None = None
        self._tail_expected_size = 0
        self._tail_stat_identity: tuple[int, int, int, int, int] | None = None
        self._tail_record_count = 0
        self._tail_head_sha256 = ""
        self._tail_hash = ZERO_SHA256
        self._tail_committed_state = _genesis_tail_state()
        self._tail_pending_record: dict[str, Any] | None = None
        self._last_cycle_reservation: dict[str, Any] | None = None
        self._attempt_failure: dict[str, Any] | None = None

        manifest_projection = _stream_manifest_projection(root)
        self._manifest_expected_size = manifest_projection.size_bytes
        self._manifest_stat_identity = manifest_projection.stat_identity
        if not _path_lexists(self.tail_commitment_path):
            if manifest_projection.sequence or _path_lexists(self.journal_path):
                raise CollectionRefusal("tail_commitment_missing_for_existing_capture")
            self._initialize_tail_commitment_registry()
        else:
            self._install_tail_registry_view(
                _stream_tail_commitment_registry(self.tail_commitment_path)
            )
        self._repair_pending_partial_journal()
        journal_projection = _stream_journal_projection(root)
        self._journal_expected_size = journal_projection.size_bytes
        self._journal_stat_identity = journal_projection.stat_identity
        if self._tail_pending_record is not None:
            self._recover_pending_tail_operation(
                manifest_projection=manifest_projection,
                journal_projection=journal_projection,
            )
            manifest_projection = _stream_manifest_projection(root)
            journal_projection = _stream_journal_projection(root)
            self._manifest_expected_size = manifest_projection.size_bytes
            self._manifest_stat_identity = manifest_projection.stat_identity
            self._journal_expected_size = journal_projection.size_bytes
            self._journal_stat_identity = journal_projection.stat_identity
        self._validate_physical_tail_state(
            manifest_projection=manifest_projection,
            journal_projection=journal_projection,
        )
        self._preflight_manifest_projection = manifest_projection
        super().__init__(output_root, writer_lock=writer_lock)
        self._assert_ledger_directories_unchanged()
        post_replay_manifest_projection = _stream_manifest_projection(root)
        if (
            post_replay_manifest_projection.sequence
            < self._preflight_manifest_projection.sequence
            or post_replay_manifest_projection.sequence != len(self.entries)
            or post_replay_manifest_projection.entry_sha256
            != self.last_entry_sha256
            or any(
                post_replay_manifest_projection.chunk_stat_identities.get(path)
                != identity
                for path, identity in self._preflight_manifest_projection.chunk_stat_identities.items()
            )
            or (
                post_replay_manifest_projection.sequence
                == self._preflight_manifest_projection.sequence
                and (
                    post_replay_manifest_projection.size_bytes
                    != self._preflight_manifest_projection.size_bytes
                    or post_replay_manifest_projection.stat_identity
                    != self._preflight_manifest_projection.stat_identity
                )
            )
        ):
            raise CollectionRefusal("manifest_or_chunk_identity_drift_during_replay")
        self._manifest_expected_size = (
            int(os.lstat(self.manifest_path).st_size)
            if _path_lexists(self.manifest_path)
            else 0
        )
        self._manifest_stat_identity = (
            _immutable_stat_identity(os.lstat(self.manifest_path))
            if _path_lexists(self.manifest_path)
            else None
        )
        self._journal_expected_size = (
            int(os.lstat(self.journal_path).st_size)
            if _path_lexists(self.journal_path)
            else 0
        )
        self._journal_stat_identity = (
            _immutable_stat_identity(os.lstat(self.journal_path))
            if _path_lexists(self.journal_path)
            else None
        )
        self._replay_complete_gap_chain()

    def _assert_ledger_directories_unchanged(self) -> None:
        root_chain = _secure_directory_chain(
            self.root,
            allow_missing_tail=False,
            reason="output_root_reparse_or_identity_invalid",
        )
        chunks_chain = _secure_directory_chain(
            self.chunks_root,
            allow_missing_tail=False,
            reason="chunk_directory_reparse_or_identity_invalid",
        )
        if (
            root_chain.get(os.path.normcase(str(self.root)))
            != self._root_directory_identity
            or chunks_chain.get(os.path.normcase(str(self.chunks_root)))
            != self._chunks_directory_identity
        ):
            raise CollectionRefusal("collector_ledger_directory_identity_drift")

    def _install_tail_registry_view(self, view: _TailRegistryView) -> None:
        self._tail_record_count = view.record_count
        self._tail_head_sha256 = view.head_sha256
        self._tail_hash = view.tail_sha256
        self._tail_committed_state = dict(view.committed_state)
        self._tail_pending_record = (
            dict(view.pending_record) if view.pending_record is not None else None
        )
        self._last_cycle_reservation = (
            dict(view.last_cycle_reservation)
            if view.last_cycle_reservation is not None
            else None
        )
        self._attempt_failure = (
            dict(view.attempt_failure)
            if view.attempt_failure is not None
            else None
        )
        self._tail_expected_size = view.size_bytes
        self._tail_stat_identity = view.stat_identity

    def _repair_pending_partial_journal(self) -> None:
        pending = self._tail_pending_record
        if pending is None or pending.get("operation_kind") != "journal_append":
            return
        encoded = pending.get("prepared_journal_payload_zlib_base64")
        if not isinstance(encoded, str) or not encoded:
            raise CollectionRefusal("tail_commitment_pending_journal_payload_missing")
        prepared_line = _decompress_prepared_journal_line(encoded)
        previous = self._tail_committed_state
        previous_size = (
            int(previous["journal_size_bytes"])
            if previous["state_kind"] == "journal"
            else 0
        )
        target_size = int(pending["state"]["journal_size_bytes"])
        if target_size != previous_size + len(prepared_line):
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        if not _path_lexists(self.journal_path):
            if previous_size:
                raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
            return
        raw, identity = _read_bounded_regular_file(
            self.journal_path,
            maximum_bytes=MAXIMUM_ACTIVE_JOURNAL_BYTES,
            missing_reason="tail_commitment_pending_journal_mismatch",
            invalid_reason="tail_commitment_pending_journal_mismatch",
        )
        if len(raw) < previous_size or len(raw) > target_size:
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        prefix = raw[:previous_size]
        prefix_projection = _journal_projection_from_complete_bytes(prefix)
        if previous_size and not self._journal_projection_matches_active(
            prefix_projection, previous
        ):
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        suffix = raw[previous_size:]
        if not prepared_line.startswith(suffix):
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        if len(raw) == previous_size or len(raw) == target_size:
            return
        remainder = prepared_line[len(suffix):]
        size, repaired_identity = _append_fsynced_line(
            self.journal_path,
            remainder,
            expected_size=len(raw),
            expected_identity=identity,
            maximum_bytes=MAXIMUM_ACTIVE_JOURNAL_BYTES,
            reason="tail_commitment_pending_journal_mismatch",
        )
        if size != target_size:
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        self._journal_expected_size = size
        self._journal_stat_identity = repaired_identity

    def _prior_main_state(self) -> dict[str, Any]:
        state = self._tail_committed_state
        return {
            "manifest_sequence": state["manifest_sequence"],
            "manifest_entry_sha256": state["manifest_entry_sha256"],
            "manifest_size_bytes": state["manifest_size_bytes"],
            "journal_manifest_sequence_target": state[
                "journal_manifest_sequence_target"
            ],
            "journal_sequence": state["journal_sequence"],
            "journal_entry_sha256": state["journal_entry_sha256"],
            "journal_size_bytes": state["journal_size_bytes"],
            "tail_registry_sequence": self._tail_record_count,
            "tail_entry_sha256": self._tail_hash,
            "tail_committed_state_sha256": canonical_sha256(state),
            "active_hour": self.active_hour,
            "gap_chain_count": self.gap_anchor.count,
            "gap_event_count": self.gap_anchor.event_count,
            "gap_root_sha256": self.gap_anchor.root_hash,
            "gap_tail_sha256": self.gap_anchor.tail_hash,
            "gap_source_id": self.gap_anchor.source_id,
            "last_bar_epoch_by_symbol": dict(self.last_bar_epoch_by_symbol),
            "last_tick_sequence_by_symbol": dict(
                self.last_tick_sequence_by_symbol
            ),
            "last_tick_transport_epoch_by_symbol": dict(
                self.last_tick_transport_epoch_by_symbol
            ),
            "last_tick_snapshot_sha256_by_symbol": dict(
                self.last_tick_snapshot_sha256_by_symbol
            ),
        }

    def reserve_cycle(
        self,
        *,
        binding: ProspectiveBinding,
        producer_monitor_proof: Mapping[str, Any],
        include_bars: bool,
        first_cycle: bool,
        cycle_started_at_epoch: float,
    ) -> dict[str, Any]:
        """Fsync one whole-cycle reservation before any post-T0 GET."""

        _assert_collector_source_unchanged()
        if self._attempt_failure is not None or self._tail_committed_state[
            "attempt_failure_sha256"
        ] != ZERO_SHA256:
            raise CollectionRefusal("cycle_attempt_permanently_failed")
        if self._tail_pending_record is not None:
            raise CollectionRefusal("tail_commitment_operation_already_pending")
        for path, expected_size, expected_identity, reason in (
            (
                self.manifest_path,
                self._manifest_expected_size,
                self._manifest_stat_identity,
                "manifest_path_drift",
            ),
            (
                self.journal_path,
                self._journal_expected_size,
                self._journal_stat_identity,
                "active_journal_path_drift",
            ),
            (
                self.tail_commitment_path,
                self._tail_expected_size,
                self._tail_stat_identity,
                "tail_commitment_path_drift",
            ),
        ):
            if expected_size == 0:
                if _path_lexists(path):
                    raise CollectionRefusal(reason)
                continue
            try:
                observed = os.lstat(path)
            except OSError as exc:
                raise CollectionRefusal(reason) from exc
            if (
                _is_reparse_or_symlink(path)
                or int(observed.st_size) != expected_size
                or _immutable_stat_identity(observed) != expected_identity
            ):
                raise CollectionRefusal(reason)
        if self._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ] != ZERO_SHA256:
            raise CollectionRefusal("unresolved_cycle_reservation_requires_recovery")
        started = _positive_float(
            cycle_started_at_epoch, "cycle_reservation_invalid"
        )
        sequence = int(self._tail_committed_state["cycle_reservation_sequence"]) + 1
        if sequence > SEALED_MAXIMUM_CYCLE_RESERVATIONS:
            raise CollectionRefusal("cycle_reservation_capacity_exhausted")
        if self._last_cycle_reservation is not None and started < float(
            self._last_cycle_reservation["cycle_started_at_epoch"]
        ) + MINIMUM_RESERVED_CYCLE_CADENCE_SECONDS:
            raise CollectionRefusal("cycle_reservation_cadence_too_fast")
        prior = self._prior_main_state()
        producer_fields = binding.producer_receipt_fields()
        body = {
            "schema_version": CYCLE_RESERVATION_SCHEMA_VERSION,
            "reservation_sequence": sequence,
            "previous_cycle_reservation_sha256": self._tail_committed_state[
                "last_cycle_reservation_sha256"
            ],
            "capture_integrity_contract_sha256": (
                capture_integrity_contract_sha256()
            ),
            "collector_source_sha256": MODULE_SOURCE_SHA256,
            "collector_wrapper_source_sha256": SUPPORT_SHA256,
            "collector_base_source_sha256": BASE_SUPPORT_SHA256,
            **binding.chunk_fields(),
            **producer_fields,
            "producer_monitor_proof": dict(producer_monitor_proof),
            "symbol_scope": list(SYMBOLS),
            "symbol_scope_sha256": canonical_sha256(list(SYMBOLS)),
            "include_bars": bool(include_bars),
            "first_cycle": bool(first_cycle),
            "cycle_started_at_epoch": started,
            "prior_main_state": prior,
            "prior_main_state_sha256": canonical_sha256(prior),
            "prior_market_source": (
                dict(self._last_source_mapping)
                if self._last_source_mapping is not None
                else None
            ),
            "collection_only": True,
            "evaluation_performed": False,
            "authority_granted": False,
            "activation_authorized": False,
            "order_authorized": False,
        }
        reservation = {
            **body,
            "reservation_sha256": canonical_sha256(body),
        }
        reservation = _validated_cycle_reservation(reservation)
        state = dict(self._tail_committed_state)
        state.update(
            {
                "cycle_reservation_sequence": sequence,
                "last_cycle_reservation_sha256": reservation[
                    "reservation_sha256"
                ],
                "unresolved_cycle_reservation_sha256": reservation[
                    "reservation_sha256"
                ],
            }
        )
        self._append_tail_event(
            operation_kind="cycle_reserve",
            state=_validate_tail_state(state),
            cycle_reservation=reservation,
        )
        return reservation

    def assert_cycle_reservation_current(self, reservation_sha256: str) -> None:
        """Fast-path continuity proof used immediately before each GET."""

        if (
            self._attempt_failure is not None
            or self._tail_pending_record is not None
            or self._last_cycle_reservation is None
            or self._tail_committed_state[
                "unresolved_cycle_reservation_sha256"
            ]
            != reservation_sha256
            or self._last_cycle_reservation["reservation_sha256"]
            != reservation_sha256
            or not self.writer_lock.authorizes(self.root)
        ):
            raise CollectionRefusal("cycle_reservation_not_current")
        try:
            observed = os.lstat(self.tail_commitment_path)
        except OSError as exc:
            raise CollectionRefusal("cycle_reservation_not_current") from exc
        if (
            _is_reparse_or_symlink(self.tail_commitment_path)
            or _immutable_stat_identity(observed) != self._tail_stat_identity
            or int(observed.st_size) != self._tail_expected_size
        ):
            raise CollectionRefusal("cycle_reservation_not_current")

    def live_writer_supervision_fast_path(self) -> dict[str, Any]:
        """Prove this exact live writer/lock/stat continuity without a WAL rescan."""

        if not self.writer_lock.authorizes(self.root):
            raise CollectionRefusal("live_writer_supervision_lock_not_held")
        rows: dict[str, Any] = {}
        for key, path, expected_size, expected_identity in (
            (
                "manifest",
                self.manifest_path,
                self._manifest_expected_size,
                self._manifest_stat_identity,
            ),
            (
                "journal",
                self.journal_path,
                self._journal_expected_size,
                self._journal_stat_identity,
            ),
            (
                "tail",
                self.tail_commitment_path,
                self._tail_expected_size,
                self._tail_stat_identity,
            ),
        ):
            if expected_size == 0:
                if _path_lexists(path):
                    raise CollectionRefusal("live_writer_supervision_stat_drift")
                rows[key] = {"present": False, "size_bytes": 0}
                continue
            try:
                observed = os.lstat(path)
            except OSError as exc:
                raise CollectionRefusal(
                    "live_writer_supervision_stat_drift"
                ) from exc
            identity = _immutable_stat_identity(observed)
            if (
                _is_reparse_or_symlink(path)
                or identity != expected_identity
                or int(observed.st_size) != expected_size
            ):
                raise CollectionRefusal("live_writer_supervision_stat_drift")
            rows[key] = {
                "present": True,
                "size_bytes": expected_size,
                "stat_identity": list(identity),
            }
        body = {
            "schema_version": (
                "fxstack.external_ig_mt4_m1_live_writer_fast_path.v1"
            ),
            "process_id": os.getpid(),
            "writer_lock_path": str(self.writer_lock.path),
            "writer_lock_held": True,
            "observed_at_epoch": time.time(),
            "tail_registry_sequence": self._tail_record_count,
            "tail_entry_sha256": self._tail_hash,
            "tail_committed_state_sha256": canonical_sha256(
                self._tail_committed_state
            ),
            "artifacts": rows,
            "full_tail_scan_performed": False,
            "full_tail_scan_required_if_process_or_lock_or_stats_are_not_current": True,
            "collection_only": True,
            "evaluation_performed": False,
            "authority_granted": False,
            "order_authorized": False,
        }
        return {**body, "proof_sha256": canonical_sha256(body)}

    def recover_unresolved_cycle(
        self,
        *,
        binding: ProspectiveBinding,
        recovered_at_epoch: float,
    ) -> bool:
        """Resolve a crashed cycle before another GET, conservatively and durably."""

        unresolved = str(
            self._tail_committed_state[
                "unresolved_cycle_reservation_sha256"
            ]
        )
        if unresolved == ZERO_SHA256:
            if self._attempt_failure is not None:
                raise CollectionRefusal("cycle_attempt_permanently_failed")
            return False
        reservation = self._last_cycle_reservation
        if reservation is None or reservation.get("reservation_sha256") != unresolved:
            raise CollectionRefusal("unresolved_cycle_reservation_invalid")
        expected_binding = (
            binding.preregistration_body_sha256,
            binding.preregistration_artifact_sha256,
            binding.t0_utc,
            binding.end_utc_exclusive,
        )
        if tuple(
            reservation[key]
            for key in (
                "preregistration_body_sha256",
                "preregistration_artifact_sha256",
                "prospective_t0_utc_inclusive",
                "prospective_end_utc_exclusive",
            )
        ) != expected_binding:
            raise CollectionRefusal("unresolved_cycle_reservation_binding_mismatch")
        recovered_at = _positive_float(
            recovered_at_epoch, "collector_clock_invalid"
        )
        if reservation["first_cycle"] is True:
            body = {
                "schema_version": CYCLE_ATTEMPT_FAILURE_SCHEMA_VERSION,
                "reservation_sequence": reservation["reservation_sequence"],
                "reservation_sha256": unresolved,
                **binding.chunk_fields(),
                "reason": (
                    "unresolved_first_cycle_reservation_after_process_interruption"
                ),
                "failed_at_epoch": recovered_at,
                "terminal_for_preregistration_attempt": True,
                "collection_only": True,
                "evaluation_performed": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            failure = {**body, "failure_sha256": canonical_sha256(body)}
            failure = _validated_attempt_failure(failure)
            state = dict(self._tail_committed_state)
            state.update(
                {
                    "unresolved_cycle_reservation_sha256": ZERO_SHA256,
                    "attempt_failure_sha256": failure["failure_sha256"],
                }
            )
            self._append_tail_event(
                operation_kind="attempt_fail",
                state=_validate_tail_state(state),
                attempt_failure=failure,
            )
            raise CollectionRefusal("cycle_attempt_permanently_failed")

        source = reservation.get("prior_market_source")
        if not isinstance(source, Mapping):
            raise CollectionRefusal("interrupted_cycle_prior_source_missing")
        source_mapping = dict(source)
        source_id = str(source_mapping.get("market_source_id") or "").lower()
        if not _is_sha256(source_id):
            raise CollectionRefusal("interrupted_cycle_prior_source_invalid")
        existing = list(self._reservation_parts_by_hash.get(unresolved, []))
        if existing:
            _validated_cycle_reservation_parts(existing)
            part_count = int(existing[0]["part_count"])
            if any(row["final"] is True for row in existing):
                raise CollectionRefusal("resolved_cycle_still_marked_unresolved")
            next_part = int(existing[-1]["part_index"]) + 1
            if next_part > part_count:
                raise CollectionRefusal("cycle_reservation_parts_invalid")
        else:
            part_count = 1
            next_part = 1
        start = float(reservation["cycle_started_at_epoch"])
        completed = max(
            start,
            min(recovered_at, binding.end_epoch_exclusive - 1e-6),
        )
        if completed >= binding.end_epoch_exclusive:
            completed = binding.end_epoch_exclusive - 1e-6
        start_minute = max(
            int(binding.t0_epoch + 59) // 60 * 60,
            int(start // 60) * 60,
        )
        end_minute = min(
            (int(binding.end_epoch_exclusive) // 60) * 60 - 60,
            int(min(recovered_at, binding.end_epoch_exclusive - 1e-6) // 60)
            * 60,
        )
        if end_minute < start_minute:
            end_minute = start_minute
        evidence = [
            {
                "schema_version": INTERRUPTED_CYCLE_GAP_SCHEMA_VERSION,
                "reservation_sequence": reservation["reservation_sequence"],
                "reservation_sha256": unresolved,
                "symbol": symbol,
                "start_minute_epoch": start_minute,
                "end_minute_epoch": end_minute,
                "minute_count": (end_minute - start_minute) // 60 + 1,
                "recovered_at_epoch": recovered_at,
                "disposition": (
                    "permanent_interrupted_cycle_gap_not_backfilled"
                ),
                "baseline_eligible": False,
            }
            for symbol in SYMBOLS
        ]
        _validated_interrupted_gap_evidence(
            evidence, binding=expected_binding
        )
        for part_index in range(next_part, part_count + 1):
            final = part_index == part_count
            part = {
                "reservation_sequence": reservation["reservation_sequence"],
                "reservation_sha256": unresolved,
                "part_index": part_index,
                "part_count": part_count,
                "final": final,
            }
            utc_hour = datetime.fromtimestamp(completed, tz=UTC).strftime(
                "%Y%m%dT%H"
            )
            chunk = {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "collector_schema_version": COLLECTOR_SCHEMA_VERSION,
                "collector_source_sha256": MODULE_SOURCE_SHA256,
                "collector_wrapper_source_sha256": SUPPORT_SHA256,
                "collector_base_source_sha256": BASE_SUPPORT_SHA256,
                "source_contract_id": SOURCE_CONTRACT_ID,
                "activity_metric_id": ACTIVITY_METRIC_ID,
                "scope_version": SCOPE_VERSION,
                "symbol_scope": list(SYMBOLS),
                "timeframe": TIMEFRAME,
                "minimum_m1_history_bars": MINIMUM_M1_BARS,
                "maximum_quote_gap_seconds": MAXIMUM_TICK_INTERVAL_SECS,
                "requested_bar_limit": DEFAULT_BAR_LIMIT,
                "configured_tick_interval_seconds": DEFAULT_TICK_INTERVAL_SECS,
                "utc_hour": utc_hour,
                "segment_index": 1,
                "collector_cycle_started_at_epoch": start,
                "collector_cycle_completed_at_epoch": completed,
                "observed_at_epoch": completed,
                **binding.chunk_fields(),
                "source": source_mapping,
                "bars": [],
                "quotes": [],
                "last_bar_epoch_by_symbol": dict(
                    self.last_bar_epoch_by_symbol
                ),
                "last_tick_sequence_by_symbol": dict(
                    self.last_tick_sequence_by_symbol
                ),
                "last_tick_transport_epoch_by_symbol": dict(
                    self.last_tick_transport_epoch_by_symbol
                ),
                "last_tick_snapshot_sha256_by_symbol": dict(
                    self.last_tick_snapshot_sha256_by_symbol
                ),
                **_anchor_fields(self.gap_anchor),
                "late_gap_records": [],
                "cycle_reservation_sequence": reservation[
                    "reservation_sequence"
                ],
                "cycle_reservation_sha256": unresolved,
                "cycle_part_index": part_index,
                "cycle_part_count": part_count,
                "cycle_part_final": final,
                "cycle_reservation_parts": [part],
                "interrupted_cycle_gap_evidence": evidence if final else [],
                "collection_only": True,
                "evaluation_performed": False,
                "success_claim_authorized": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            self.append_cycle(chunk)
        return True

    def _tail_record(
        self,
        *,
        phase: str,
        operation_kind: str,
        state: Mapping[str, Any],
        previous_state_sha256: str,
        prepared_manifest_entry: Mapping[str, Any] | None,
        prepared_journal_line_sha256: str,
        prepared_journal_payload_zlib_base64: str,
        cycle_reservation: Mapping[str, Any] | None = None,
        cycle_resolution: Mapping[str, Any] | None = None,
        attempt_failure: Mapping[str, Any] | None = None,
        operation_id: str | None = None,
    ) -> dict[str, Any]:
        validated_state = _validate_tail_state(state)
        observed_operation_id = operation_id or _tail_operation_id(
            operation_kind=operation_kind,
            previous_state_sha256=previous_state_sha256,
            state=validated_state,
            prepared_manifest_entry=prepared_manifest_entry,
            prepared_journal_line_sha256=prepared_journal_line_sha256,
            prepared_journal_payload_zlib_base64=(
                prepared_journal_payload_zlib_base64
            ),
            cycle_reservation=cycle_reservation,
            cycle_resolution=cycle_resolution,
            attempt_failure=attempt_failure,
        )
        body = {
            "schema_version": TAIL_COMMITMENT_SCHEMA_VERSION,
            "registry_sequence": self._tail_record_count + 1,
            "previous_registry_entry_sha256": self._tail_hash,
            "capture_integrity_contract_sha256": (
                capture_integrity_contract_sha256()
            ),
            "collector_source_sha256": MODULE_SOURCE_SHA256,
            "collector_wrapper_source_sha256": SUPPORT_SHA256,
            "collector_base_source_sha256": BASE_SUPPORT_SHA256,
            "phase": phase,
            "operation_kind": operation_kind,
            "operation_id": observed_operation_id,
            "previous_committed_state_sha256": previous_state_sha256,
            "state": validated_state,
            "prepared_manifest_entry": (
                dict(prepared_manifest_entry)
                if prepared_manifest_entry is not None
                else None
            ),
            "prepared_journal_line_sha256": prepared_journal_line_sha256,
            "prepared_journal_payload_zlib_base64": (
                prepared_journal_payload_zlib_base64
            ),
            "cycle_reservation": (
                dict(cycle_reservation) if cycle_reservation is not None else None
            ),
            "cycle_resolution": (
                dict(cycle_resolution) if cycle_resolution is not None else None
            ),
            "attempt_failure": (
                dict(attempt_failure) if attempt_failure is not None else None
            ),
        }
        return {**body, "registry_entry_sha256": canonical_sha256(body)}

    def _append_tail_record(self, record: Mapping[str, Any]) -> None:
        self._assert_ledger_directories_unchanged()
        line = canonical_json_bytes(dict(record)) + b"\n"
        if (
            len(line) > MAXIMUM_TAIL_COMMITMENT_LINE_BYTES
            or self._tail_record_count + 1 > MAXIMUM_TAIL_COMMITMENT_RECORDS
        ):
            raise CollectionRefusal("tail_commitment_line_too_large")
        size, identity = _append_fsynced_line(
            self.tail_commitment_path,
            line,
            expected_size=self._tail_expected_size,
            expected_identity=self._tail_stat_identity,
            maximum_bytes=MAXIMUM_TAIL_COMMITMENT_BYTES,
            reason="tail_commitment_append_failed",
        )
        self._tail_expected_size = size
        self._tail_stat_identity = identity
        self._tail_record_count += 1
        self._tail_hash = str(record["registry_entry_sha256"])
        if not self._tail_head_sha256:
            self._tail_head_sha256 = self._tail_hash

    def _append_tail_event(
        self,
        *,
        operation_kind: str,
        state: Mapping[str, Any],
        cycle_reservation: Mapping[str, Any] | None = None,
        attempt_failure: Mapping[str, Any] | None = None,
    ) -> None:
        if self._tail_pending_record is not None:
            raise CollectionRefusal("tail_commitment_operation_already_pending")
        previous_state_hash = canonical_sha256(self._tail_committed_state)
        record = self._tail_record(
            phase="event",
            operation_kind=operation_kind,
            state=state,
            previous_state_sha256=previous_state_hash,
            prepared_manifest_entry=None,
            prepared_journal_line_sha256=ZERO_SHA256,
            prepared_journal_payload_zlib_base64="",
            cycle_reservation=cycle_reservation,
            attempt_failure=attempt_failure,
        )
        self._append_tail_record(record)
        self._tail_committed_state = dict(state)
        if cycle_reservation is not None:
            self._last_cycle_reservation = dict(cycle_reservation)
        if attempt_failure is not None:
            self._attempt_failure = dict(attempt_failure)

    def _initialize_tail_commitment_registry(self) -> None:
        state = _genesis_tail_state()
        record = self._tail_record(
            phase="commit",
            operation_kind="genesis",
            state=state,
            previous_state_sha256=ZERO_SHA256,
            prepared_manifest_entry=None,
            prepared_journal_line_sha256=ZERO_SHA256,
            prepared_journal_payload_zlib_base64="",
        )
        self._append_tail_record(record)
        self._tail_committed_state = state

    def _prepare_tail_operation(
        self,
        *,
        operation_kind: str,
        state: Mapping[str, Any],
        prepared_manifest_entry: Mapping[str, Any] | None,
        prepared_journal_line_sha256: str,
        prepared_journal_payload_zlib_base64: str,
    ) -> dict[str, Any]:
        if self._tail_pending_record is not None:
            raise CollectionRefusal("tail_commitment_operation_already_pending")
        self._validate_tail_transition(operation_kind, state)
        previous_state_hash = canonical_sha256(self._tail_committed_state)
        record = self._tail_record(
            phase="prepare",
            operation_kind=operation_kind,
            state=state,
            previous_state_sha256=previous_state_hash,
            prepared_manifest_entry=prepared_manifest_entry,
            prepared_journal_line_sha256=prepared_journal_line_sha256,
            prepared_journal_payload_zlib_base64=(
                prepared_journal_payload_zlib_base64
            ),
        )
        self._append_tail_record(record)
        self._tail_pending_record = dict(record)
        return record

    def _finish_tail_operation(self, *, phase: str) -> None:
        pending = self._tail_pending_record
        if pending is None or phase not in {"commit", "abort"}:
            raise CollectionRefusal("tail_commitment_pending_operation_invalid")
        state = dict(pending["state"])
        record = self._tail_record(
            phase=phase,
            operation_kind=str(pending["operation_kind"]),
            state=state,
            previous_state_sha256=str(pending["previous_committed_state_sha256"]),
            prepared_manifest_entry=None,
            prepared_journal_line_sha256=ZERO_SHA256,
            prepared_journal_payload_zlib_base64="",
            operation_id=str(pending["operation_id"]),
        )
        self._append_tail_record(record)
        if phase == "commit":
            self._tail_committed_state = dict(pending["state"])
        self._tail_pending_record = None

    def _validate_tail_transition(
        self, operation_kind: str, state_value: Mapping[str, Any]
    ) -> None:
        _validate_tail_transition_values(
            self._tail_committed_state,
            state_value,
            operation_kind,
        )

    @staticmethod
    def _manifest_projection_matches(
        projection: _ManifestProjection, state: Mapping[str, Any]
    ) -> bool:
        return (
            projection.sequence == state["manifest_sequence"]
            and projection.entry_sha256 == state["manifest_entry_sha256"]
            and projection.size_bytes == state["manifest_size_bytes"]
        )

    @staticmethod
    def _journal_projection_matches_active(
        projection: _JournalProjection, state: Mapping[str, Any]
    ) -> bool:
        return (
            projection.target == state["journal_manifest_sequence_target"]
            and projection.sequence == state["journal_sequence"]
            and projection.entry_sha256 == state["journal_entry_sha256"]
            and projection.size_bytes == state["journal_size_bytes"]
            and projection.previous_manifest_sha256 == state["manifest_entry_sha256"]
        )

    def _latest_chunk_matches_state(self, state: Mapping[str, Any]) -> bool:
        if state["manifest_sequence"] == 0:
            return True
        path = self.root.joinpath(*PurePosixPath(str(state["latest_chunk_path"])).parts)
        try:
            payload, _identity = _read_bounded_regular_file(
                path,
                maximum_bytes=MAXIMUM_CHUNK_BYTES,
                missing_reason="tail_commitment_latest_chunk_missing",
                invalid_reason="tail_commitment_latest_chunk_invalid",
            )
        except CollectionRefusal:
            return False
        return (
            len(payload) == state["latest_chunk_size_bytes"]
            and hashlib.sha256(payload).hexdigest() == state["latest_chunk_sha256"]
        )

    def _validate_physical_tail_state(
        self,
        *,
        manifest_projection: _ManifestProjection,
        journal_projection: _JournalProjection,
    ) -> None:
        state = self._tail_committed_state
        if not self._manifest_projection_matches(manifest_projection, state):
            raise CollectionRefusal("tail_commitment_manifest_projection_mismatch")
        if not self._latest_chunk_matches_state(state):
            raise CollectionRefusal("tail_commitment_latest_chunk_mismatch")
        if state["state_kind"] == "journal":
            if not self._journal_projection_matches_active(journal_projection, state):
                raise CollectionRefusal("tail_commitment_journal_projection_mismatch")
        elif state["state_kind"] == "manifest":
            if journal_projection.sequence and not (
                journal_projection.target
                == state["covered_journal_manifest_sequence_target"]
                and journal_projection.sequence == state["covered_journal_sequence"]
                and journal_projection.entry_sha256
                == state["covered_journal_entry_sha256"]
                and journal_projection.size_bytes == state["covered_journal_size_bytes"]
            ):
                raise CollectionRefusal("tail_commitment_journal_projection_mismatch")
        elif journal_projection.sequence:
            raise CollectionRefusal("tail_commitment_journal_projection_mismatch")

    def _recover_pending_tail_operation(
        self,
        *,
        manifest_projection: _ManifestProjection,
        journal_projection: _JournalProjection,
    ) -> None:
        pending = self._tail_pending_record
        if pending is None:
            return
        operation_kind = str(pending["operation_kind"])
        target = dict(pending["state"])
        previous = self._tail_committed_state
        if operation_kind == "journal_append":
            if not self._manifest_projection_matches(manifest_projection, previous):
                raise CollectionRefusal("tail_commitment_pending_manifest_mismatch")
            previous_journal_matches = (
                journal_projection.sequence == 0
                if previous["state_kind"] != "journal"
                else self._journal_projection_matches_active(
                    journal_projection, previous
                )
            )
            if previous_journal_matches:
                encoded = pending.get("prepared_journal_payload_zlib_base64")
                if not isinstance(encoded, str) or not encoded:
                    raise CollectionRefusal(
                        "tail_commitment_pending_journal_payload_missing"
                    )
                line = _decompress_prepared_journal_line(encoded)
                if hashlib.sha256(line).hexdigest() != pending.get(
                    "prepared_journal_line_sha256"
                ):
                    raise CollectionRefusal(
                        "tail_commitment_pending_journal_payload_invalid"
                    )
                size, identity = _append_fsynced_line(
                    self.journal_path,
                    line,
                    expected_size=journal_projection.size_bytes,
                    expected_identity=journal_projection.stat_identity,
                    maximum_bytes=MAXIMUM_ACTIVE_JOURNAL_BYTES,
                    reason="active_journal_path_drift",
                )
                if size != target["journal_size_bytes"]:
                    raise CollectionRefusal(
                        "tail_commitment_pending_journal_mismatch"
                    )
                self._journal_expected_size = size
                self._journal_stat_identity = identity
                self._finish_tail_operation(phase="commit")
            elif self._journal_projection_matches_active(journal_projection, target):
                self._finish_tail_operation(phase="commit")
            else:
                raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
            return
        if operation_kind != "manifest_finalize":
            raise CollectionRefusal("tail_commitment_pending_operation_invalid")
        if not self._journal_projection_matches_active(journal_projection, previous):
            raise CollectionRefusal("tail_commitment_pending_journal_mismatch")
        old_manifest = self._manifest_projection_matches(manifest_projection, previous)
        new_manifest = self._manifest_projection_matches(manifest_projection, target)
        entry = pending.get("prepared_manifest_entry")
        if not isinstance(entry, Mapping):
            raise CollectionRefusal("tail_commitment_pending_manifest_invalid")
        chunk_path = self.root.joinpath(*PurePosixPath(str(target["latest_chunk_path"])).parts)
        chunk_exists = _path_lexists(chunk_path)
        chunk_matches = self._latest_chunk_matches_state(target) if chunk_exists else False
        if old_manifest and not chunk_exists:
            self._finish_tail_operation(phase="abort")
            return
        if not chunk_matches or not (old_manifest or new_manifest):
            raise CollectionRefusal("tail_commitment_pending_manifest_mismatch")
        if old_manifest:
            line = canonical_json_bytes(dict(entry)) + b"\n"
            size, identity = _append_fsynced_line(
                self.manifest_path,
                line,
                expected_size=manifest_projection.size_bytes,
                expected_identity=manifest_projection.stat_identity,
                maximum_bytes=MAXIMUM_MANIFEST_BYTES,
                reason="manifest_append_failed",
            )
            if size != target["manifest_size_bytes"]:
                raise CollectionRefusal("tail_commitment_pending_manifest_mismatch")
            self._manifest_expected_size = size
            self._manifest_stat_identity = identity
        self._finish_tail_operation(phase="commit")

    def _validate_gap_cycle_structure(self, value: Mapping[str, Any]) -> None:
        if (
            value.get("collector_source_sha256") != MODULE_SOURCE_SHA256
            or value.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
            or value.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
        ):
            raise CollectionRefusal("capture_source_identity_invalid")
        source = value.get("source")
        records = value.get("late_gap_records")
        if not isinstance(source, Mapping) or not isinstance(records, list):
            raise CollectionRefusal("gap_cycle_invalid")
        source_id = str(source.get("market_source_id") or "").lower()
        anchor = _anchor_from_mapping(value, reason="gap_cycle_anchor_invalid")
        if anchor.source_id != source_id:
            raise CollectionRefusal("gap_source_identity_invalid")
        inferred_count = anchor.count - len(records)
        inferred_event_count = anchor.event_count - sum(
            len(record.get("events") or []) if isinstance(record, Mapping) else 0
            for record in records
        )
        if inferred_count < 0 or inferred_event_count < 0:
            raise CollectionRefusal("gap_cycle_anchor_invalid")
        if records:
            first = records[0]
            if not isinstance(first, Mapping):
                raise CollectionRefusal("late_gap_record_invalid")
            inferred_tail = str(first.get("previous_gap_entry_sha256") or "").lower()
        else:
            inferred_tail = anchor.tail_hash
        inferred_root = ZERO_SHA256 if inferred_count == 0 else anchor.root_hash
        inferred = GapAnchor(
            inferred_count,
            inferred_event_count,
            inferred_root,
            inferred_tail,
            source_id,
        )
        observed = _advance_gap_anchor(
            inferred,
            records,
            expected_source_id=source_id,
            binding=tuple(value[key] for key in (
                "preregistration_body_sha256",
                "preregistration_artifact_sha256",
                "prospective_t0_utc_inclusive",
                "prospective_end_utc_exclusive",
            )),
        )
        if observed != anchor:
            raise CollectionRefusal("gap_cycle_anchor_invalid")
        reservation_sequence = _strict_positive_int(
            value.get("cycle_reservation_sequence"),
            "cycle_reservation_parts_invalid",
        )
        reservation_hash = str(
            value.get("cycle_reservation_sha256") or ""
        ).lower()
        part_index = _strict_positive_int(
            value.get("cycle_part_index"), "cycle_reservation_parts_invalid"
        )
        part_count = _strict_positive_int(
            value.get("cycle_part_count"), "cycle_reservation_parts_invalid"
        )
        final = value.get("cycle_part_final")
        parts = _validated_cycle_reservation_parts(
            value.get("cycle_reservation_parts")
        )
        last_part = parts[-1]
        if (
            not _is_sha256(reservation_hash)
            or part_index > part_count
            or not isinstance(final, bool)
            or final is not (part_index == part_count)
            or (
                reservation_sequence,
                reservation_hash,
                part_index,
                part_count,
                final,
            )
            != (
                last_part["reservation_sequence"],
                last_part["reservation_sha256"],
                last_part["part_index"],
                last_part["part_count"],
                last_part["final"],
            )
        ):
            raise CollectionRefusal("cycle_reservation_parts_invalid")
        interrupted = _validated_interrupted_gap_evidence(
            value.get("interrupted_cycle_gap_evidence"),
            binding=tuple(
                str(value[key])
                for key in (
                    "preregistration_body_sha256",
                    "preregistration_artifact_sha256",
                    "prospective_t0_utc_inclusive",
                    "prospective_end_utc_exclusive",
                )
            ),
        )
        final_parts = {
            (row["reservation_sequence"], row["reservation_sha256"])
            for row in parts
            if row["final"] is True
        }
        if any(
            (row["reservation_sequence"], row["reservation_sha256"])
            not in final_parts
            for row in interrupted
        ):
            raise CollectionRefusal("interrupted_cycle_gap_evidence_invalid")

    def _load_manifest_streaming(self) -> None:
        """Replay the preflight-bound manifest with bounded no-follow reads."""

        projection = self._preflight_manifest_projection
        if projection.sequence == 0:
            return
        manifest_raw, manifest_identity = _read_bounded_regular_file(
            self.manifest_path,
            maximum_bytes=MAXIMUM_MANIFEST_BYTES,
            missing_reason="manifest_unreadable",
            invalid_reason="manifest_invalid",
        )
        if manifest_identity != projection.stat_identity:
            raise CollectionRefusal("manifest_identity_drift_during_replay")
        prior_hash = ZERO_SHA256
        lines = manifest_raw.splitlines(keepends=True)
        if len(lines) != projection.sequence:
            raise CollectionRefusal("manifest_chain_invalid")
        for expected_sequence, raw_line in enumerate(lines, start=1):
            if (
                len(raw_line) > MAXIMUM_MANIFEST_LINE_BYTES
                or not raw_line.endswith(b"\n")
                or raw_line == b"\n"
            ):
                raise CollectionRefusal("manifest_line_invalid")
            parsed = _strict_json_object(raw_line, reason="manifest_invalid")
            if (
                set(parsed) != support._MANIFEST_FIELDS
                or raw_line != canonical_json_bytes(parsed) + b"\n"
            ):
                raise CollectionRefusal("manifest_scope_invalid")
            entry = dict(parsed)
            claimed = str(entry.pop("manifest_entry_sha256", "")).lower()
            sequence = _strict_positive_int(
                entry.get("sequence"), "manifest_sequence_invalid"
            )
            utc_hour = str(entry.get("utc_hour") or "")
            segment = _strict_positive_int(
                entry.get("segment_index"), "manifest_segment_invalid"
            )
            if (
                not _is_sha256(claimed)
                or canonical_sha256(entry) != claimed
                or entry.get("schema_version") != MANIFEST_SCHEMA_VERSION
                or sequence != expected_sequence
                or entry.get("previous_entry_sha256") != prior_hash
                or segment != 1
            ):
                raise CollectionRefusal("manifest_chain_invalid")
            expected_relative = PurePosixPath(
                support.CHUNK_DIRECTORY,
                utc_hour,
                f"ig-mt4-m1-activity-s{segment:04d}-q{sequence:010d}.json",
            ).as_posix()
            relative = str(entry.get("chunk_path") or "")
            if relative != expected_relative:
                raise CollectionRefusal("manifest_chunk_path_invalid")
            expected_identity = projection.chunk_stat_identities.get(relative)
            chunk_path = self.root.joinpath(*PurePosixPath(relative).parts)
            chunk_raw, chunk_identity = _read_bounded_regular_file(
                chunk_path,
                maximum_bytes=MAXIMUM_CHUNK_BYTES,
                missing_reason="manifest_chunk_missing",
                invalid_reason="manifest_chunk_invalid",
            )
            if (
                expected_identity is None
                or chunk_identity != expected_identity
                or len(chunk_raw) != entry.get("chunk_size_bytes")
                or hashlib.sha256(chunk_raw).hexdigest()
                != entry.get("chunk_sha256")
            ):
                raise CollectionRefusal("manifest_chunk_hash_mismatch")
            chunk = _strict_json_object(
                chunk_raw, reason="manifest_chunk_invalid"
            )
            if chunk_raw != canonical_json_bytes(chunk) + b"\n":
                raise CollectionRefusal("manifest_chunk_not_canonical")
            source_id, state = self._validate_portable_chunk(
                chunk, base_state=self._state_copies()
            )
            binding = support._validated_binding_tuple(
                entry, reason="manifest_preregistration_binding_invalid"
            )
            chunk_binding = support._validated_binding_tuple(
                chunk, reason="manifest_preregistration_binding_invalid"
            )
            bars = chunk.get("bars")
            quotes = chunk.get("quotes")
            if not isinstance(bars, list) or not isinstance(quotes, list):
                raise CollectionRefusal("manifest_chunk_rows_invalid")
            entry_maps = (
                support._symbol_int_map(
                    entry.get("last_bar_epoch_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                support._symbol_int_map(
                    entry.get("last_tick_sequence_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                support._symbol_float_map(
                    entry.get("last_tick_transport_epoch_by_symbol"),
                    "manifest_state_map_invalid",
                ),
                support._symbol_hash_map(
                    entry.get("last_tick_snapshot_sha256_by_symbol"),
                    "manifest_state_map_invalid",
                ),
            )
            if (
                binding != chunk_binding
                or str(entry.get("market_source_id") or "").lower()
                != source_id
                or entry.get("chunk_schema_version") != CHUNK_SCHEMA_VERSION
                or entry.get("bar_rows") != len(bars)
                or entry.get("quote_rows") != len(quotes)
                or entry_maps != state[:4]
            ):
                raise CollectionRefusal("manifest_entry_chunk_mismatch")
            entry["manifest_entry_sha256"] = claimed
            self.entries.append(entry)
            self._referenced_chunks.add(relative)
            self._install_state(state)
            self.last_source_id = source_id
            self.last_segment_index = 1
            prior_hash = claimed
        if prior_hash != projection.entry_sha256:
            raise CollectionRefusal("manifest_chain_invalid")
        latest = self.entries[-1]
        self.next_sequence = int(latest["sequence"]) + 1
        self.last_entry_sha256 = str(latest["manifest_entry_sha256"])

    def _validate_portable_chunk(self, chunk: Mapping[str, Any], *, base_state: Any) -> Any:
        result = super()._validate_portable_chunk(chunk, base_state=base_state)
        self._validate_gap_cycle_structure(chunk)
        source = chunk.get("source")
        if isinstance(source, Mapping):
            self._last_source_mapping = dict(source)
        return result

    def _read_active_journal(
        self,
    ) -> tuple[int, str, str, list[dict[str, Any]]]:
        projection = _stream_journal_projection(self.root)
        if projection.sequence <= 0:
            raise CollectionRefusal("active_journal_empty")
        reason = "active_journal_invalid"
        flags = (
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            before_path = os.lstat(self.journal_path)
            descriptor = os.open(self.journal_path, flags)
        except OSError as exc:
            raise CollectionRefusal("active_journal_unreadable") from exc
        records: list[dict[str, Any]] = []
        prior_hash = ZERO_SHA256
        target = 0
        previous_manifest = ""
        active_hour = ""
        total = 0
        executing_source_sha256 = collector_source_sha256()
        try:
            before_handle = os.fstat(descriptor)
            if (
                projection.stat_identity is None
                or _immutable_stat_identity(before_path) != projection.stat_identity
                or _immutable_stat_identity(before_handle) != projection.stat_identity
            ):
                raise CollectionRefusal(reason)
            with os.fdopen(descriptor, "rb", closefd=False) as handle:
                while True:
                    raw_line = handle.readline(support.MAXIMUM_JOURNAL_LINE_BYTES + 1)
                    if not raw_line:
                        break
                    expected_sequence = len(records) + 1
                    total += len(raw_line)
                    if (
                        expected_sequence > MAXIMUM_ACTIVE_JOURNAL_RECORDS
                        or len(raw_line) > support.MAXIMUM_JOURNAL_LINE_BYTES
                        or not raw_line.endswith(b"\n")
                        or raw_line == b"\n"
                        or total > MAXIMUM_ACTIVE_JOURNAL_BYTES
                    ):
                        raise CollectionRefusal("active_journal_line_invalid")
                    parsed = _strict_json_object(
                        raw_line, reason="active_journal_invalid"
                    )
                    if (
                        set(parsed) != support._JOURNAL_RECORD_FIELDS
                        or raw_line != canonical_json_bytes(parsed) + b"\n"
                    ):
                        raise CollectionRefusal("active_journal_not_canonical")
                    wrapper = dict(parsed)
                    claimed = str(
                        wrapper.pop("journal_entry_sha256", "")
                    ).lower()
                    sequence = _strict_positive_int(
                        wrapper.get("journal_sequence"),
                        "active_journal_sequence_invalid",
                    )
                    cycle = wrapper.get("cycle")
                    if not isinstance(cycle, Mapping):
                        raise CollectionRefusal("active_journal_cycle_invalid")
                    cycle_dict = dict(cycle)
                    if (
                        not _is_sha256(claimed)
                        or canonical_sha256(wrapper) != claimed
                        or wrapper.get("schema_version")
                        != support.ACTIVE_JOURNAL_SCHEMA_VERSION
                        or sequence != expected_sequence
                        or wrapper.get("previous_journal_entry_sha256") != prior_hash
                        or wrapper.get("capture_integrity_contract_sha256")
                        != self._journal_integrity_sha256()
                        or wrapper.get("collector_source_sha256")
                        != executing_source_sha256
                        or wrapper.get("collector_support_source_sha256")
                        != BASE_SUPPORT_SHA256
                        or set(cycle_dict) != support._PORTABLE_CHUNK_FIELDS
                    ):
                        raise CollectionRefusal("active_journal_contract_invalid")
                    observed_target = _strict_positive_int(
                        wrapper.get("manifest_sequence_target"),
                        "active_journal_target_invalid",
                    )
                    observed_previous = str(
                        wrapper.get("previous_manifest_entry_sha256") or ""
                    ).lower()
                    observed_hour = str(cycle_dict.get("utc_hour") or "")
                    if not _is_sha256(observed_previous):
                        raise CollectionRefusal("active_journal_target_invalid")
                    if not records:
                        target = observed_target
                        previous_manifest = observed_previous
                        active_hour = observed_hour
                    elif (
                        observed_target != target
                        or observed_previous != previous_manifest
                        or observed_hour != active_hour
                    ):
                        raise CollectionRefusal("active_journal_identity_changed")
                    records.append(cycle_dict)
                    prior_hash = claimed
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            after_path = os.lstat(self.journal_path)
        except OSError as exc:
            raise CollectionRefusal(reason) from exc
        if (
            not records
            or total != projection.size_bytes
            or _immutable_stat_identity(after_handle) != projection.stat_identity
            or _immutable_stat_identity(after_path) != projection.stat_identity
        ):
            raise CollectionRefusal(reason)
        self._journal_expected_size = projection.size_bytes
        self._journal_stat_identity = projection.stat_identity
        return target, previous_manifest, prior_hash, records

    def _aggregate_journal_cycles(
        self, records: Sequence[Mapping[str, Any]]
    ) -> dict[str, Any]:
        aggregate = super()._aggregate_journal_cycles(records)
        late_records: list[Any] = []
        reservation_parts: list[Any] = []
        interrupted_evidence: list[Any] = []
        for record in records:
            value = record.get("late_gap_records")
            parts = record.get("cycle_reservation_parts")
            interrupted = record.get("interrupted_cycle_gap_evidence")
            if (
                not isinstance(value, list)
                or not isinstance(parts, list)
                or not isinstance(interrupted, list)
            ):
                raise CollectionRefusal("gap_cycle_invalid")
            late_records.extend(value)
            reservation_parts.extend(parts)
            interrupted_evidence.extend(interrupted)
        last = records[-1]
        aggregate.update(
            {
                "collector_source_sha256": MODULE_SOURCE_SHA256,
                "collector_wrapper_source_sha256": SUPPORT_SHA256,
                "collector_base_source_sha256": BASE_SUPPORT_SHA256,
                **{key: last[key] for key in _GAP_MANIFEST_FIELDS if key.startswith("gap_")},
                "late_gap_records": late_records,
                **{
                    key: last[key]
                    for key in (
                        "cycle_reservation_sequence",
                        "cycle_reservation_sha256",
                        "cycle_part_index",
                        "cycle_part_count",
                        "cycle_part_final",
                    )
                },
                "cycle_reservation_parts": reservation_parts,
                "interrupted_cycle_gap_evidence": interrupted_evidence,
            }
        )
        self._validate_gap_cycle_structure(aggregate)
        return aggregate

    def _expected_manifest_entry(
        self,
        chunk: Mapping[str, Any],
        chunk_bytes: bytes,
        *,
        sequence: int,
        previous_hash: str,
    ) -> dict[str, Any]:
        entry = super()._expected_manifest_entry(
            chunk,
            chunk_bytes,
            sequence=sequence,
            previous_hash=previous_hash,
        )
        body = dict(entry)
        body.pop("manifest_entry_sha256", None)
        body.update(
            {
                "collector_source_sha256": MODULE_SOURCE_SHA256,
                "collector_wrapper_source_sha256": SUPPORT_SHA256,
                "collector_base_source_sha256": BASE_SUPPORT_SHA256,
                **{key: chunk[key] for key in _GAP_MANIFEST_FIELDS if key.startswith("gap_")},
                **{
                    key: chunk[key]
                    for key in (
                        "cycle_reservation_sequence",
                        "cycle_reservation_sha256",
                        "cycle_part_index",
                        "cycle_part_count",
                        "cycle_part_final",
                    )
                },
                "cycle_reservation_part_count": len(
                    chunk["cycle_reservation_parts"]
                ),
                "cycle_reservation_parts_sha256": canonical_sha256(
                    chunk["cycle_reservation_parts"]
                ),
                "interrupted_cycle_gap_count": len(
                    chunk["interrupted_cycle_gap_evidence"]
                ),
                "interrupted_cycle_gap_evidence_sha256": canonical_sha256(
                    chunk["interrupted_cycle_gap_evidence"]
                ),
            }
        )
        return {**body, "manifest_entry_sha256": canonical_sha256(body)}

    def _tail_state_for_journal(
        self,
        cycle: Mapping[str, Any],
        *,
        sequence: int,
        entry_sha256: str,
        size_bytes: int,
    ) -> dict[str, Any]:
        previous = self._tail_committed_state
        state = {
            "state_kind": "journal",
            **{
                key: previous[key]
                for key in (
                    "manifest_sequence",
                    "manifest_entry_sha256",
                    "manifest_size_bytes",
                    "latest_chunk_path",
                    "latest_chunk_sha256",
                    "latest_chunk_size_bytes",
                )
            },
            "journal_manifest_sequence_target": self.active_manifest_target,
            "journal_sequence": sequence,
            "journal_entry_sha256": entry_sha256,
            "journal_size_bytes": size_bytes,
            "covered_journal_manifest_sequence_target": 0,
            "covered_journal_sequence": 0,
            "covered_journal_entry_sha256": ZERO_SHA256,
            "covered_journal_size_bytes": 0,
            **{
                key: cycle[key]
                for key in (
                    "gap_chain_count",
                    "gap_event_count",
                    "gap_root_sha256",
                    "gap_tail_sha256",
                    "gap_source_id",
                    "preregistration_body_sha256",
                    "preregistration_artifact_sha256",
                    "prospective_t0_utc_inclusive",
                    "prospective_end_utc_exclusive",
                )
            },
            "cycle_reservation_sequence": previous[
                "cycle_reservation_sequence"
            ],
            "last_cycle_reservation_sha256": previous[
                "last_cycle_reservation_sha256"
            ],
            "unresolved_cycle_reservation_sha256": (
                ZERO_SHA256
                if cycle["cycle_part_final"] is True
                else previous["unresolved_cycle_reservation_sha256"]
            ),
            "attempt_failure_sha256": previous["attempt_failure_sha256"],
        }
        return _validate_tail_state(state)

    def _tail_state_for_manifest(
        self,
        entry: Mapping[str, Any],
        *,
        manifest_size_bytes: int,
    ) -> dict[str, Any]:
        previous = self._tail_committed_state
        state = {
            "state_kind": "manifest",
            "manifest_sequence": entry["sequence"],
            "manifest_entry_sha256": entry["manifest_entry_sha256"],
            "manifest_size_bytes": manifest_size_bytes,
            "journal_manifest_sequence_target": 0,
            "journal_sequence": 0,
            "journal_entry_sha256": ZERO_SHA256,
            "journal_size_bytes": 0,
            "covered_journal_manifest_sequence_target": previous[
                "journal_manifest_sequence_target"
            ],
            "covered_journal_sequence": previous["journal_sequence"],
            "covered_journal_entry_sha256": previous["journal_entry_sha256"],
            "covered_journal_size_bytes": previous["journal_size_bytes"],
            "latest_chunk_path": entry["chunk_path"],
            "latest_chunk_sha256": entry["chunk_sha256"],
            "latest_chunk_size_bytes": entry["chunk_size_bytes"],
            **{
                key: entry[key]
                for key in (
                    "gap_chain_count",
                    "gap_event_count",
                    "gap_root_sha256",
                    "gap_tail_sha256",
                    "gap_source_id",
                    "preregistration_body_sha256",
                    "preregistration_artifact_sha256",
                    "prospective_t0_utc_inclusive",
                    "prospective_end_utc_exclusive",
                )
            },
            **{
                key: previous[key]
                for key in (
                    "cycle_reservation_sequence",
                    "last_cycle_reservation_sha256",
                    "unresolved_cycle_reservation_sha256",
                    "attempt_failure_sha256",
                )
            },
        }
        return _validate_tail_state(state)

    def _append_manifest_entry(self, entry: Mapping[str, Any]) -> None:
        _assert_collector_source_unchanged()
        self._assert_ledger_directories_unchanged()
        line = canonical_json_bytes(dict(entry)) + b"\n"
        if (
            len(line) > MAXIMUM_MANIFEST_LINE_BYTES
            or len(self.entries) + 1 > MAXIMUM_MANIFEST_ENTRIES
        ):
            raise CollectionRefusal("manifest_size_limit_exceeded")
        size, identity = _append_fsynced_line(
            self.manifest_path,
            line,
            expected_size=self._manifest_expected_size,
            expected_identity=self._manifest_stat_identity,
            maximum_bytes=MAXIMUM_MANIFEST_BYTES,
            reason="manifest_append_failed",
        )
        self._manifest_expected_size = size
        self._manifest_stat_identity = identity

    def _finalize_active_records(
        self,
        *,
        records: Sequence[Mapping[str, Any]],
        target: int,
        previous_manifest: str,
    ) -> dict[str, Any]:
        _assert_collector_source_unchanged()
        self._assert_ledger_directories_unchanged()
        if target != self.next_sequence or previous_manifest != self.last_entry_sha256:
            raise CollectionRefusal("active_journal_target_invalid")
        chunk = self._aggregate_journal_cycles(records)
        chunk_bytes = canonical_json_bytes(chunk) + b"\n"
        if len(chunk_bytes) > MAXIMUM_CHUNK_BYTES:
            raise CollectionRefusal("chunk_size_limit_exceeded")
        entry = self._expected_manifest_entry(
            chunk,
            chunk_bytes,
            sequence=target,
            previous_hash=previous_manifest,
        )
        manifest_line = canonical_json_bytes(entry) + b"\n"
        desired_state = self._tail_state_for_manifest(
            entry,
            manifest_size_bytes=self._manifest_expected_size + len(manifest_line),
        )
        self._prepare_tail_operation(
            operation_kind="manifest_finalize",
            state=desired_state,
            prepared_manifest_entry=entry,
            prepared_journal_line_sha256=ZERO_SHA256,
            prepared_journal_payload_zlib_base64="",
        )
        relative = str(entry["chunk_path"])
        chunk_path = self.root.joinpath(*PurePosixPath(relative).parts)
        if _path_lexists(chunk_path):
            existing, _identity = _read_bounded_regular_file(
                chunk_path,
                maximum_bytes=MAXIMUM_CHUNK_BYTES,
                missing_reason="recovery_chunk_unreadable",
                invalid_reason="recovery_chunk_unreadable",
            )
            if existing != chunk_bytes:
                raise CollectionRefusal("recovery_chunk_mismatch")
        else:
            _durable_atomic_write_new(chunk_path, chunk_bytes)
        self._assert_ledger_directories_unchanged()
        self._append_manifest_entry(entry)
        self._finish_tail_operation(phase="commit")
        stored = dict(entry)
        self.entries.append(stored)
        self._referenced_chunks.add(relative)
        self.next_sequence += 1
        self.last_entry_sha256 = str(entry["manifest_entry_sha256"])
        self.last_source_id = str(entry["market_source_id"])
        self.last_segment_index = 1
        return stored

    def _close_journal_descriptor(self) -> None:
        if self._journal_descriptor is not None:
            os.close(self._journal_descriptor)
            self._journal_descriptor = None

    def _append_journal_wrapper(self, cycle: Mapping[str, Any]) -> None:
        _assert_collector_source_unchanged()
        self._assert_ledger_directories_unchanged()
        sequence = self.active_journal_sequence + 1
        body = {
            "schema_version": support.ACTIVE_JOURNAL_SCHEMA_VERSION,
            "journal_sequence": sequence,
            "previous_journal_entry_sha256": self.active_journal_hash,
            "manifest_sequence_target": self.active_manifest_target,
            "previous_manifest_entry_sha256": self.last_entry_sha256,
            "capture_integrity_contract_sha256": self._journal_integrity_sha256(),
            "collector_source_sha256": collector_source_sha256(),
            "collector_support_source_sha256": BASE_SUPPORT_SHA256,
            "cycle": dict(cycle),
        }
        wrapper = {**body, "journal_entry_sha256": canonical_sha256(body)}
        line = canonical_json_bytes(wrapper) + b"\n"
        if (
            len(line) > support.MAXIMUM_JOURNAL_LINE_BYTES
            or sequence > MAXIMUM_ACTIVE_JOURNAL_RECORDS
            or self._journal_expected_size + len(line) > MAXIMUM_ACTIVE_JOURNAL_BYTES
        ):
            raise CollectionRefusal("active_journal_line_too_large")
        desired_state = self._tail_state_for_journal(
            cycle,
            sequence=sequence,
            entry_sha256=str(wrapper["journal_entry_sha256"]),
            size_bytes=self._journal_expected_size + len(line),
        )
        self._prepare_tail_operation(
            operation_kind="journal_append",
            state=desired_state,
            prepared_manifest_entry=None,
            prepared_journal_line_sha256=hashlib.sha256(line).hexdigest(),
            prepared_journal_payload_zlib_base64=(
                _compress_prepared_journal_line(line)
            ),
        )
        size, identity = _append_fsynced_line(
            self.journal_path,
            line,
            expected_size=self._journal_expected_size,
            expected_identity=self._journal_stat_identity,
            maximum_bytes=MAXIMUM_ACTIVE_JOURNAL_BYTES,
            reason="active_journal_path_drift",
        )
        self._journal_expected_size = size
        self._journal_stat_identity = identity
        self._finish_tail_operation(phase="commit")
        self.active_journal_sequence = sequence
        self.active_journal_hash = str(wrapper["journal_entry_sha256"])

    def finalize_active(self) -> dict[str, Any] | None:
        self._close_journal_descriptor()
        return super().finalize_active()

    def _clear_active_journal(self) -> None:
        self._close_journal_descriptor()
        super()._clear_active_journal()
        self._journal_expected_size = 0
        self._journal_stat_identity = None
        _sync_parent_directory(self.journal_path)

    def _verify_chunk_tree(self) -> None:
        self._assert_ledger_directories_unchanged()
        if _path_lexists(self.manifest_staging_path):
            raise CollectionRefusal("manifest_staging_not_recovered")
        actual: set[str] = set()
        file_count = 0
        hour_count = 0
        try:
            hour_iterator = os.scandir(self.chunks_root)
        except OSError as exc:
            raise CollectionRefusal("chunk_tree_unreadable") from exc
        with hour_iterator:
            for hour_entry in hour_iterator:
                hour_count += 1
                if hour_count > MAXIMUM_CHUNK_FILES:
                    raise CollectionRefusal("chunk_tree_size_limit_exceeded")
                hour_path = Path(hour_entry.path)
                if (
                    hour_entry.is_symlink()
                    or _is_reparse_or_symlink(hour_path)
                    or not hour_entry.is_dir(follow_symlinks=False)
                ):
                    raise CollectionRefusal("chunk_tree_reparse_or_shape_invalid")
                try:
                    datetime.strptime(hour_entry.name, "%Y%m%dT%H")
                    child_iterator = os.scandir(hour_path)
                except (OSError, ValueError) as exc:
                    raise CollectionRefusal(
                        "chunk_tree_reparse_or_shape_invalid"
                    ) from exc
                with child_iterator:
                    for child in child_iterator:
                        child_path = Path(child.path)
                        file_count += 1
                        if (
                            file_count > MAXIMUM_CHUNK_FILES
                            or child.is_symlink()
                            or _is_reparse_or_symlink(child_path)
                            or not child.is_file(follow_symlinks=False)
                            or not child.name.endswith(".json")
                        ):
                            raise CollectionRefusal("chunk_tree_size_limit_exceeded")
                        actual.add(child_path.relative_to(self.root).as_posix())
        if actual != self._referenced_chunks:
            raise CollectionRefusal("orphan_or_missing_chunk_detected")
        self._assert_ledger_directories_unchanged()

    def _validate_gap_event_against_capture(self, event: Any) -> None:
        symbol, epoch, watermark = _validated_gap_event(
            event, reason="late_gap_event_invalid"
        )
        if (
            watermark > self.last_bar_epoch_by_symbol[symbol]
            or self.bar_epoch_coverage_by_symbol[symbol].contains(epoch)
            or self.gap_epoch_coverage_by_symbol[symbol].contains(epoch)
        ):
            raise CollectionRefusal("late_gap_event_invalid")
        self.gap_epoch_coverage_by_symbol[symbol].add(epoch)

    def _replay_complete_gap_chain(self) -> None:
        anchor = GapAnchor()
        coverage = {symbol: _GapEpochCoverage() for symbol in SYMBOLS}
        interrupted_coverage = {
            symbol: _GapEpochCoverage() for symbol in SYMBOLS
        }
        reservation_parts_by_hash: dict[str, list[dict[str, Any]]] = {}
        self.gap_epoch_coverage_by_symbol = coverage
        self.interrupted_gap_epoch_coverage_by_symbol = interrupted_coverage
        for entry in self.entries:
            relative = str(entry["chunk_path"])
            path = self.root.joinpath(*PurePosixPath(relative).parts)
            raw, _identity = _read_bounded_regular_file(
                path,
                maximum_bytes=MAXIMUM_CHUNK_BYTES,
                missing_reason="manifest_chunk_missing",
                invalid_reason="manifest_chunk_invalid",
            )
            chunk = _strict_json_object(raw, reason="manifest_chunk_invalid")
            parts = _validated_cycle_reservation_parts(
                chunk.get("cycle_reservation_parts")
            )
            interrupted = _validated_interrupted_gap_evidence(
                chunk.get("interrupted_cycle_gap_evidence"),
                binding=tuple(
                    str(chunk[key])
                    for key in (
                        "preregistration_body_sha256",
                        "preregistration_artifact_sha256",
                        "prospective_t0_utc_inclusive",
                        "prospective_end_utc_exclusive",
                    )
                ),
            )
            if (
                entry.get("cycle_reservation_part_count") != len(parts)
                or entry.get("cycle_reservation_parts_sha256")
                != canonical_sha256(parts)
                or entry.get("interrupted_cycle_gap_count") != len(interrupted)
                or entry.get("interrupted_cycle_gap_evidence_sha256")
                != canonical_sha256(interrupted)
                or any(
                    entry.get(key) != chunk.get(key)
                    for key in (
                        "cycle_reservation_sequence",
                        "cycle_reservation_sha256",
                        "cycle_part_index",
                        "cycle_part_count",
                        "cycle_part_final",
                    )
                )
            ):
                raise CollectionRefusal("manifest_cycle_reservation_mismatch")
            for part in parts:
                reservation_parts_by_hash.setdefault(
                    str(part["reservation_sha256"]), []
                ).append(dict(part))
            for row in interrupted:
                interrupted_coverage[row["symbol"]].union_interval(
                    row["start_minute_epoch"], row["end_minute_epoch"]
                )
            source_id = str(entry["market_source_id"]).lower()
            records = chunk.get("late_gap_records")
            if not isinstance(records, list):
                raise CollectionRefusal("gap_cycle_invalid")

            def validate(event: Any) -> None:
                symbol, epoch, watermark = _validated_gap_event(
                    event, reason="late_gap_event_invalid"
                )
                if (
                    watermark > self.last_bar_epoch_by_symbol[symbol]
                    or self.bar_epoch_coverage_by_symbol[symbol].contains(epoch)
                    or coverage[symbol].contains(epoch)
                ):
                    raise CollectionRefusal("late_gap_event_invalid")
                coverage[symbol].add(epoch)

            anchor = _advance_gap_anchor(
                anchor,
                records,
                expected_source_id=source_id,
                binding=(
                    str(chunk["preregistration_body_sha256"]),
                    str(chunk["preregistration_artifact_sha256"]),
                    str(chunk["prospective_t0_utc_inclusive"]),
                    str(chunk["prospective_end_utc_exclusive"]),
                ),
                event_validator=validate,
            )
            observed = _anchor_from_mapping(chunk, reason="gap_cycle_anchor_invalid")
            manifest_anchor = _anchor_from_mapping(
                entry, reason="gap_manifest_anchor_invalid"
            )
            if observed != anchor or manifest_anchor != anchor:
                raise CollectionRefusal("gap_chain_rollback_or_truncation_detected")
        self.gap_anchor = anchor
        unresolved = self._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ]
        for reservation_hash, rows in reservation_parts_by_hash.items():
            _validated_cycle_reservation_parts(rows)
            if (
                rows[0]["part_index"] != 1
                or (
                    reservation_hash == unresolved
                    and rows[-1]["final"] is True
                )
                or (
                    reservation_hash != unresolved
                    and rows[-1]["final"] is not True
                )
            ):
                raise CollectionRefusal("cycle_reservation_parts_invalid")
        self._reservation_parts_by_hash = reservation_parts_by_hash

    def contains_gap(self, symbol: str, epoch: int) -> bool:
        return (
            self.gap_epoch_coverage_by_symbol[symbol].contains(epoch)
            or self.interrupted_gap_epoch_coverage_by_symbol[symbol].contains(epoch)
        )

    def prepare_gap_records(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        source_id: str,
        observed_at_epoch: float,
        binding: ProspectiveBinding,
    ) -> tuple[list[dict[str, Any]], GapAnchor]:
        _assert_collector_source_unchanged()
        source_id = str(source_id or "").lower()
        normalized: list[dict[str, Any]] = []
        seen: set[tuple[str, int]] = set()
        for raw in events:
            event = dict(raw)
            symbol, epoch, watermark = _validated_gap_event(
                event, reason="late_gap_event_invalid"
            )
            key = (symbol, epoch)
            if key in seen or self.contains_gap(symbol, epoch):
                continue
            if (
                watermark > self.last_bar_epoch_by_symbol[symbol]
                or self.bar_epoch_coverage_by_symbol[symbol].contains(epoch)
            ):
                raise CollectionRefusal("late_gap_event_invalid")
            seen.add(key)
            normalized.append(event)
        normalized.sort(key=lambda row: (SYMBOLS.index(str(row["symbol"])), int(row["minute_epoch"])))
        prior = self.gap_anchor
        if prior.source_id and prior.source_id != source_id:
            raise CollectionRefusal("gap_source_identity_invalid")
        if not normalized:
            return [], GapAnchor(
                prior.count,
                prior.event_count,
                prior.root_hash,
                prior.tail_hash,
                source_id,
            )
        observed_at = _positive_float(observed_at_epoch, "late_gap_record_invalid")
        body = {
            "schema_version": LATE_GAP_RECORD_SCHEMA_VERSION,
            "gap_sequence": prior.count + 1,
            "previous_gap_entry_sha256": prior.tail_hash,
            "capture_integrity_contract_sha256": (
                capture_integrity_contract_sha256()
            ),
            "collector_source_sha256": MODULE_SOURCE_SHA256,
            "collector_wrapper_source_sha256": SUPPORT_SHA256,
            "collector_base_source_sha256": BASE_SUPPORT_SHA256,
            **binding.chunk_fields(),
            "market_source_id": source_id,
            "observed_at_epoch": observed_at,
            "events": normalized,
        }
        record = {**body, "gap_entry_sha256": canonical_sha256(body)}
        anchor = _advance_gap_anchor(
            prior,
            [record],
            expected_source_id=source_id,
            binding=(
                binding.preregistration_body_sha256,
                binding.preregistration_artifact_sha256,
                binding.t0_utc,
                binding.end_utc_exclusive,
            ),
        )
        return [record], anchor

    def append_cycle(self, cycle: Mapping[str, Any]) -> dict[str, Any] | None:
        self._validate_gap_cycle_structure(cycle)
        reservation_hash = str(cycle.get("cycle_reservation_sha256") or "")
        if (
            self._last_cycle_reservation is None
            or reservation_hash
            != self._tail_committed_state[
                "unresolved_cycle_reservation_sha256"
            ]
            or reservation_hash
            != self._last_cycle_reservation["reservation_sha256"]
            or cycle.get("cycle_reservation_sequence")
            != self._last_cycle_reservation["reservation_sequence"]
        ):
            raise CollectionRefusal("cycle_reservation_not_current")
        parts = _validated_cycle_reservation_parts(
            cycle.get("cycle_reservation_parts")
        )
        existing_parts = self._reservation_parts_by_hash.get(
            reservation_hash, []
        )
        if (
            len(parts) != 1
            or parts[0]["part_index"] != len(existing_parts) + 1
            or (
                existing_parts
                and parts[0]["part_count"]
                != existing_parts[0]["part_count"]
            )
        ):
            raise CollectionRefusal("cycle_reservation_parts_invalid")
        source = cycle.get("source")
        if not isinstance(source, Mapping):
            raise CollectionRefusal("manifest_chunk_source_invalid")
        source_id = str(source.get("market_source_id") or "").lower()
        records = cycle.get("late_gap_records")
        if not isinstance(records, list):
            raise CollectionRefusal("gap_cycle_invalid")
        expected = _advance_gap_anchor(
            self.gap_anchor,
            records,
            expected_source_id=source_id,
            binding=tuple(cycle[key] for key in (
                "preregistration_body_sha256",
                "preregistration_artifact_sha256",
                "prospective_t0_utc_inclusive",
                "prospective_end_utc_exclusive",
            )),
        )
        if _anchor_from_mapping(cycle, reason="gap_cycle_anchor_invalid") != expected:
            raise CollectionRefusal("gap_cycle_anchor_invalid")
        emitted = super().append_cycle(cycle)
        for record in records:
            for event in record["events"]:
                symbol, epoch, _watermark = _validated_gap_event(
                    event, reason="late_gap_event_invalid"
                )
                self.gap_epoch_coverage_by_symbol[symbol].add(epoch)
        self.gap_anchor = expected
        interrupted = _validated_interrupted_gap_evidence(
            cycle.get("interrupted_cycle_gap_evidence"),
            binding=tuple(
                str(cycle[key])
                for key in (
                    "preregistration_body_sha256",
                    "preregistration_artifact_sha256",
                    "prospective_t0_utc_inclusive",
                    "prospective_end_utc_exclusive",
                )
            ),
        )
        for row in interrupted:
            self.interrupted_gap_epoch_coverage_by_symbol[row["symbol"]].union_interval(
                row["start_minute_epoch"], row["end_minute_epoch"]
            )
        self._reservation_parts_by_hash.setdefault(reservation_hash, []).extend(
            dict(row) for row in parts
        )
        return emitted


def validate_tail_commitment_registry(output_root: str | Path) -> dict[str, Any]:
    """Validate the independent tail WAL and its main-chain projection without writes."""

    supplied = Path(os.path.abspath(Path(output_root).expanduser()))
    root = _absolute_existing_path_without_reparse(
        supplied, reason="tail_commitment_root_invalid"
    )
    if os.path.normcase(str(supplied)) != os.path.normcase(str(root)):
        raise CollectionRefusal("tail_commitment_root_invalid")
    view = _stream_tail_commitment_registry(root / TAIL_COMMITMENT_FILENAME)
    if view.pending_record is not None:
        raise CollectionRefusal("tail_commitment_pending_operation_requires_recovery")
    state = view.committed_state
    manifest = _stream_manifest_projection(root)
    journal = _stream_journal_projection(root)
    if (
        manifest.sequence != state["manifest_sequence"]
        or manifest.entry_sha256 != state["manifest_entry_sha256"]
        or manifest.size_bytes != state["manifest_size_bytes"]
    ):
        raise CollectionRefusal("tail_commitment_manifest_projection_mismatch")
    if state["manifest_sequence"]:
        if manifest.last_entry is None or any(
            manifest.last_entry.get(key) != state[state_key]
            for key, state_key in (
                ("chunk_path", "latest_chunk_path"),
                ("chunk_sha256", "latest_chunk_sha256"),
                ("chunk_size_bytes", "latest_chunk_size_bytes"),
            )
        ):
            raise CollectionRefusal("tail_commitment_latest_chunk_mismatch")
        chunk_path = root.joinpath(
            *PurePosixPath(str(state["latest_chunk_path"])).parts
        )
        chunk_raw, _identity = _read_bounded_regular_file(
            chunk_path,
            maximum_bytes=MAXIMUM_CHUNK_BYTES,
            missing_reason="tail_commitment_latest_chunk_missing",
            invalid_reason="tail_commitment_latest_chunk_invalid",
        )
        if (
            len(chunk_raw) != state["latest_chunk_size_bytes"]
            or hashlib.sha256(chunk_raw).hexdigest()
            != state["latest_chunk_sha256"]
        ):
            raise CollectionRefusal("tail_commitment_latest_chunk_mismatch")
    if state["state_kind"] == "journal":
        if not (
            journal.target == state["journal_manifest_sequence_target"]
            and journal.sequence == state["journal_sequence"]
            and journal.entry_sha256 == state["journal_entry_sha256"]
            and journal.size_bytes == state["journal_size_bytes"]
            and journal.previous_manifest_sha256 == state["manifest_entry_sha256"]
        ):
            raise CollectionRefusal("tail_commitment_journal_projection_mismatch")
    elif state["state_kind"] == "manifest" and journal.sequence:
        previous_manifest = (
            str(manifest.last_entry.get("previous_entry_sha256"))
            if manifest.last_entry is not None
            else ""
        )
        if not (
            journal.target == state["covered_journal_manifest_sequence_target"]
            and journal.sequence == state["covered_journal_sequence"]
            and journal.entry_sha256 == state["covered_journal_entry_sha256"]
            and journal.size_bytes == state["covered_journal_size_bytes"]
            and journal.previous_manifest_sha256 == previous_manifest
        ):
            raise CollectionRefusal("tail_commitment_journal_projection_mismatch")
    elif state["state_kind"] == "genesis" and journal.sequence:
        raise CollectionRefusal("tail_commitment_journal_projection_mismatch")
    return {
        "status": "valid",
        "schema_version": TAIL_COMMITMENT_SCHEMA_VERSION,
        "filename": TAIL_COMMITMENT_FILENAME,
        "capture_integrity_contract_sha256": capture_integrity_contract_sha256(),
        "collector_source_sha256": MODULE_SOURCE_SHA256,
        "collector_wrapper_source_sha256": SUPPORT_SHA256,
        "collector_base_source_sha256": BASE_SUPPORT_SHA256,
        "artifact_sha256": view.artifact_sha256,
        "artifact_size_bytes": view.size_bytes,
        "record_count": view.record_count,
        "registry_sequence": view.record_count,
        "genesis_entry_sha256": view.head_sha256,
        "tail_entry_sha256": view.tail_sha256,
        "committed_state_sha256": canonical_sha256(state),
        "committed_state_kind": state["state_kind"],
        "manifest_sequence": state["manifest_sequence"],
        "manifest_entry_sha256": state["manifest_entry_sha256"],
        "manifest_size_bytes": state["manifest_size_bytes"],
        "journal_manifest_sequence_target": state[
            "journal_manifest_sequence_target"
        ],
        "journal_sequence": state["journal_sequence"],
        "journal_entry_sha256": state["journal_entry_sha256"],
        "journal_size_bytes": state["journal_size_bytes"],
        "covered_journal_manifest_sequence_target": state[
            "covered_journal_manifest_sequence_target"
        ],
        "covered_journal_sequence": state["covered_journal_sequence"],
        "covered_journal_entry_sha256": state[
            "covered_journal_entry_sha256"
        ],
        "covered_journal_size_bytes": state["covered_journal_size_bytes"],
        "latest_chunk_path": state["latest_chunk_path"],
        "latest_chunk_sha256": state["latest_chunk_sha256"],
        "latest_chunk_size_bytes": state["latest_chunk_size_bytes"],
        "gap_chain_count": state["gap_chain_count"],
        "gap_event_count": state["gap_event_count"],
        "gap_root_sha256": state["gap_root_sha256"],
        "gap_tail_sha256": state["gap_tail_sha256"],
        "gap_source_id": state["gap_source_id"],
        "preregistration_body_sha256": state["preregistration_body_sha256"],
        "preregistration_artifact_sha256": state[
            "preregistration_artifact_sha256"
        ],
        "prospective_t0_utc_inclusive": state[
            "prospective_t0_utc_inclusive"
        ],
        "prospective_end_utc_exclusive": state[
            "prospective_end_utc_exclusive"
        ],
        "cycle_reservation_sequence": state["cycle_reservation_sequence"],
        "last_cycle_reservation_sha256": state[
            "last_cycle_reservation_sha256"
        ],
        "unresolved_cycle_reservation_sha256": state[
            "unresolved_cycle_reservation_sha256"
        ],
        "attempt_failure_sha256": state["attempt_failure_sha256"],
        "physical_journal_present": journal.sequence > 0,
        "pending_operation": False,
    }


_START_EDGE_RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "upstream_producer_software_body_sha256",
        "bridge_ea_repository_source_identity",
        "bridge_ea_deployed_source_identity",
        "bridge_ea_deployed_ex4_identity",
        "first_manifest_entry_sha256",
        "first_chunk_sha256",
        "first_cycle_durable_at_epoch",
        "receipt_sha256",
    }
)


def _validate_first_entry_full_scope(
    *,
    binding: ProspectiveBinding,
    ledger: ManifestLedger,
) -> tuple[dict[str, Any], dict[str, Any], float]:
    if not ledger.entries:
        raise CollectionRefusal("start_edge_full_scope_proof_missing")
    first = ledger.entries[0]
    path = ledger.root.joinpath(*PurePosixPath(str(first["chunk_path"])).parts)
    raw, _identity = _read_bounded_regular_file(
        path,
        maximum_bytes=MAXIMUM_CHUNK_BYTES,
        missing_reason="start_edge_full_scope_proof_missing",
        invalid_reason="start_edge_full_scope_proof_invalid",
    )
    if (
        first.get("sequence") != 1
        or len(raw) != first.get("chunk_size_bytes")
        or hashlib.sha256(raw).hexdigest() != first.get("chunk_sha256")
    ):
        raise CollectionRefusal("start_edge_full_scope_proof_invalid")
    chunk = _strict_json_object(raw, reason="start_edge_full_scope_proof_invalid")
    if raw != canonical_json_bytes(chunk) + b"\n":
        raise CollectionRefusal("start_edge_full_scope_proof_invalid")
    bars = chunk.get("bars")
    quotes = chunk.get("quotes")
    if not isinstance(bars, list) or not isinstance(quotes, list):
        raise CollectionRefusal("start_edge_full_scope_proof_invalid")
    bar_counts = {symbol: 0 for symbol in SYMBOLS}
    for row in bars:
        if not isinstance(row, Mapping):
            raise CollectionRefusal("start_edge_full_scope_proof_invalid")
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol not in bar_counts:
            raise CollectionRefusal("start_edge_full_scope_proof_invalid")
        bar_counts[symbol] += 1
    quote_symbols: set[str] = set()
    for row in quotes:
        if not isinstance(row, Mapping):
            raise CollectionRefusal("start_edge_full_scope_proof_invalid")
        symbol = str(row.get("symbol") or "").strip().upper()
        quote_symbols.add(symbol)
        times = (
            row.get("observation_epoch"),
            row.get("observed_at_epoch"),
            row.get("transport_received_at_epoch"),
        )
        optional_market_time = row.get("market_event_received_at_epoch")
        if optional_market_time is not None:
            times = (*times, optional_market_time)
        for raw_time in times:
            observed = _positive_float(raw_time, "start_edge_full_scope_proof_invalid")
            if not binding.t0_epoch <= observed < binding.end_epoch_exclusive:
                raise CollectionRefusal("start_edge_full_scope_proof_invalid")
    completed = _positive_float(
        chunk.get("collector_cycle_completed_at_epoch"),
        "start_edge_full_scope_proof_invalid",
    )
    if (
        any(count < MINIMUM_M1_BARS for count in bar_counts.values())
        or quote_symbols != SYMBOL_SET
        or chunk.get("preregistration_body_sha256")
        != binding.preregistration_body_sha256
        or chunk.get("preregistration_artifact_sha256")
        != binding.preregistration_artifact_sha256
        or chunk.get("prospective_t0_utc_inclusive") != binding.t0_utc
        or chunk.get("prospective_end_utc_exclusive") != binding.end_utc_exclusive
        or completed < binding.t0_epoch
        or completed > binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
    ):
        raise CollectionRefusal("start_edge_full_scope_proof_invalid")
    return dict(first), dict(chunk), completed


def validate_start_edge_durability_receipt(
    output_root: str | Path,
    *,
    binding: ProspectiveBinding,
    ledger: ManifestLedger,
) -> dict[str, Any]:
    """Read and fully validate the start receipt without locking or writing."""

    root = Path(output_root).expanduser().resolve(strict=False)
    if root != ledger.root:
        raise CollectionRefusal("start_edge_durable_receipt_root_mismatch")
    path = root / START_EDGE_RECEIPT_FILENAME
    if not path.exists():
        raise CollectionRefusal("start_edge_durable_receipt_missing")
    if not path.is_file() or path.is_symlink() or _is_reparse_or_symlink(path):
        raise CollectionRefusal("start_edge_durable_receipt_invalid")
    raw, _identity = _read_bounded_regular_file(
        path,
        maximum_bytes=MAXIMUM_START_EDGE_RECEIPT_BYTES,
        missing_reason="start_edge_durable_receipt_missing",
        invalid_reason="start_edge_durable_receipt_unreadable_or_oversize",
    )
    value = _strict_json_object(raw, reason="start_edge_durable_receipt_invalid")
    if raw != canonical_json_bytes(value) + b"\n":
        raise CollectionRefusal("start_edge_durable_receipt_not_canonical")
    body = dict(value)
    claimed = str(body.pop("receipt_sha256", "")).lower()
    first, _chunk, completed = _validate_first_entry_full_scope(
        binding=binding,
        ledger=ledger,
    )
    durable_at = _positive_float(
        value.get("first_cycle_durable_at_epoch"),
        "start_edge_durable_receipt_invalid",
    )
    producer_fields = binding.producer_receipt_fields()
    if (
        set(value) != _START_EDGE_RECEIPT_FIELDS
        or value.get("schema_version") != START_EDGE_RECEIPT_SCHEMA_VERSION
        or value.get("capture_integrity_contract_sha256")
        != capture_integrity_contract_sha256()
        or value.get("collector_source_sha256") != MODULE_SOURCE_SHA256
        or value.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
        or value.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
        or value.get("preregistration_body_sha256")
        != binding.preregistration_body_sha256
        or value.get("preregistration_artifact_sha256")
        != binding.preregistration_artifact_sha256
        or value.get("prospective_t0_utc_inclusive") != binding.t0_utc
        or value.get("prospective_end_utc_exclusive") != binding.end_utc_exclusive
        or any(value.get(key) != expected for key, expected in producer_fields.items())
        or value.get("first_manifest_entry_sha256")
        != first["manifest_entry_sha256"]
        or value.get("first_chunk_sha256") != first["chunk_sha256"]
        or durable_at < binding.t0_epoch
        or completed > durable_at
        or durable_at > binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
    ):
        raise CollectionRefusal("start_edge_durable_receipt_invalid")
    return {
        "status": "valid",
        "receipt_sha256": claimed,
        "first_cycle_completed_at_epoch": completed,
        "first_cycle_durable_at_epoch": durable_at,
        "first_manifest_entry_sha256": first["manifest_entry_sha256"],
        "first_chunk_sha256": first["chunk_sha256"],
        **producer_fields,
        "collection_only": True,
        "evaluation_performed": False,
        "authority_granted": False,
        "order_authorized": False,
    }


class StartEdgeDurabilityReceipt:
    """Truthful post-fsync witness for the first complete 22-symbol cycle."""

    def __init__(
        self,
        output_root: str | Path,
        *,
        binding: ProspectiveBinding,
        ledger: ManifestLedger,
        writer_lock: ExclusiveDataWriterLock,
    ) -> None:
        if not writer_lock.authorizes(output_root):
            raise CollectionRefusal("exclusive_data_writer_lock_required")
        self.path = Path(output_root).expanduser().resolve(strict=False) / START_EDGE_RECEIPT_FILENAME
        self.binding = binding
        self.ledger = ledger
        self.committed = False
        self.durable_at_epoch = 0.0
        self._load()

    def _assert_first_entry_full_scope(self) -> float:
        _first, _chunk, completed = _validate_first_entry_full_scope(
            binding=self.binding,
            ledger=self.ledger,
        )
        return completed

    def _load(self) -> None:
        if not self.path.exists():
            if self.ledger.entries:
                raise CollectionRefusal("start_edge_durable_receipt_missing")
            return
        proof = validate_start_edge_durability_receipt(
            self.path.parent,
            binding=self.binding,
            ledger=self.ledger,
        )
        self.committed = True
        self.durable_at_epoch = float(proof["first_cycle_durable_at_epoch"])

    def commit(self, *, durable_at_epoch: float) -> None:
        if self.committed or len(self.ledger.entries) != 1:
            raise CollectionRefusal("start_edge_durable_receipt_state_invalid")
        durable_at = _positive_float(
            durable_at_epoch, "start_edge_durable_receipt_invalid"
        )
        if durable_at > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS:
            raise CollectionRefusal("prospective_window_first_durable_commit_late")
        completed = self._assert_first_entry_full_scope()
        if durable_at < self.binding.t0_epoch or durable_at < completed:
            raise CollectionRefusal("prospective_window_first_durable_clock_regressed")
        first = self.ledger.entries[0]
        body = {
            "schema_version": START_EDGE_RECEIPT_SCHEMA_VERSION,
            "capture_integrity_contract_sha256": (
                capture_integrity_contract_sha256()
            ),
            "collector_source_sha256": MODULE_SOURCE_SHA256,
            "collector_wrapper_source_sha256": SUPPORT_SHA256,
            "collector_base_source_sha256": BASE_SUPPORT_SHA256,
            **self.binding.chunk_fields(),
            **self.binding.producer_receipt_fields(),
            "first_manifest_entry_sha256": first["manifest_entry_sha256"],
            "first_chunk_sha256": first["chunk_sha256"],
            "first_cycle_durable_at_epoch": durable_at,
        }
        value = {**body, "receipt_sha256": canonical_sha256(body)}
        _durable_atomic_write_new(self.path, canonical_json_bytes(value) + b"\n")
        self.committed = True
        self.durable_at_epoch = durable_at


class ProspectiveActivityCollector:
    """Emit first bars or permanent absences through one durable main chain."""

    def __init__(
        self,
        *,
        client: BridgeReadClient,
        ledger: ManifestLedger,
        binding: ProspectiveBinding,
        receipt: StartEdgeDurabilityReceipt | None = None,
        policy: CollectionPolicy | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        policy = policy or CollectionPolicy()
        policy.validate()
        if (
            policy.tick_interval_secs != DEFAULT_TICK_INTERVAL_SECS
            or policy.bar_interval_secs != DEFAULT_BAR_INTERVAL_SECS
            or policy.bar_limit != DEFAULT_BAR_LIMIT
        ):
            raise CollectionRefusal("collection_policy_must_match_sealed_contract")
        if client.timeout_secs != DEFAULT_HTTP_TIMEOUT_SECS:
            raise CollectionRefusal("http_timeout_must_match_sealed_policy")
        expected_binding = (
            binding.preregistration_body_sha256,
            binding.preregistration_artifact_sha256,
            binding.t0_utc,
            binding.end_utc_exclusive,
        )
        if ledger.binding_tuple is not None and ledger.binding_tuple != expected_binding:
            raise CollectionRefusal("ledger_preregistration_binding_mismatch")
        self.client = client
        self.ledger = ledger
        self.binding = binding
        self.receipt = receipt or StartEdgeDurabilityReceipt(
            ledger.root,
            binding=binding,
            ledger=ledger,
            writer_lock=ledger.writer_lock,
        )
        self.policy = policy
        self.clock = clock
        self.current_source_id = ledger.last_source_id
        self.segment_index = ledger.last_segment_index
        self.last_bar_epoch_by_symbol = dict(ledger.last_bar_epoch_by_symbol)
        self.last_tick_sequence_by_symbol = dict(ledger.last_tick_sequence_by_symbol)
        self.last_tick_transport_epoch_by_symbol = dict(
            ledger.last_tick_transport_epoch_by_symbol
        )
        self.last_tick_snapshot_sha256_by_symbol = dict(
            ledger.last_tick_snapshot_sha256_by_symbol
        )
        self._authentication_proven = False
        monitor = self._producer_monitor()
        if self.ledger._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ] != ZERO_SHA256:
            monitor.assert_unchanged()
            self.ledger.recover_unresolved_cycle(
                binding=self.binding,
                recovered_at_epoch=_positive_float(
                    self.clock(), "collector_clock_invalid"
                ),
            )
            self._sync_state_from_ledger()
            monitor.assert_unchanged()

    @property
    def is_pristine(self) -> bool:
        return self.ledger.next_sequence == 1 and not self.ledger.active_hour

    def _sync_state_from_ledger(self) -> None:
        self.current_source_id = self.ledger.last_source_id
        self.segment_index = self.ledger.last_segment_index
        self.last_bar_epoch_by_symbol = dict(
            self.ledger.last_bar_epoch_by_symbol
        )
        self.last_tick_sequence_by_symbol = dict(
            self.ledger.last_tick_sequence_by_symbol
        )
        self.last_tick_transport_epoch_by_symbol = dict(
            self.ledger.last_tick_transport_epoch_by_symbol
        )
        self.last_tick_snapshot_sha256_by_symbol = dict(
            self.ledger.last_tick_snapshot_sha256_by_symbol
        )

    def assert_start_edge_open(self, now: float) -> None:
        if self.is_pristine and now > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS:
            raise CollectionRefusal("prospective_window_start_edge_missed")

    def _producer_monitor(self) -> ProducerSoftwareMonitor:
        monitor = self.binding.producer_software_monitor
        if monitor is None:
            raise CollectionRefusal("upstream_producer_software_paths_required")
        return monitor

    def _assert_observation_inside_window(self, value: Any) -> float:
        observed = _positive_float(value, "collector_observation_time_invalid")
        if observed < self.binding.t0_epoch:
            raise CollectionRefusal("prospective_observation_before_t0")
        if observed >= self.binding.end_epoch_exclusive:
            raise CollectionRefusal("prospective_observation_at_or_after_end")
        return observed

    def _candidate_segment(self, source: SourceIdentity) -> tuple[int, bool]:
        if not self.current_source_id:
            return 1, True
        if self.current_source_id != source.source_id:
            raise CollectionRefusal("market_source_rollover_refused")
        return self.segment_index, False

    def capture_cycle(self, *, include_bars: bool) -> dict[str, Any] | None:
        monitor = self._producer_monitor()
        if self.ledger._tail_committed_state[
            "unresolved_cycle_reservation_sha256"
        ] != ZERO_SHA256:
            monitor.assert_unchanged()
            self.ledger.recover_unresolved_cycle(
                binding=self.binding,
                recovered_at_epoch=_positive_float(
                    self.clock(), "collector_clock_invalid"
                ),
            )
            self._sync_state_from_ledger()
            monitor.assert_unchanged()
        try:
            return self._capture_cycle(include_bars=include_bars)
        finally:
            monitor.assert_unchanged()

    def _capture_cycle(self, *, include_bars: bool) -> dict[str, Any] | None:
        _assert_collector_source_unchanged()
        pristine = self.is_pristine
        if pristine and not include_bars:
            raise CollectionRefusal("first_cycle_complete_direct_m1_scope_required")
        cycle_started_at = _positive_float(self.clock(), "collector_clock_invalid")
        if cycle_started_at < self.binding.t0_epoch:
            raise CollectionRefusal("prospective_window_not_started")
        self.assert_start_edge_open(cycle_started_at)
        request_count = (
            3
            + (len(SYMBOLS) if include_bars else 0)
            + (0 if self._authentication_proven else 1)
        )
        latest_safe_start = self.binding.end_epoch_exclusive - (
            request_count * self.client.timeout_secs
        )
        if cycle_started_at >= latest_safe_start:
            raise CollectionRefusal("prospective_cycle_deadline_insufficient")

        monitor = self._producer_monitor()
        reservation = self.ledger.reserve_cycle(
            binding=self.binding,
            producer_monitor_proof=monitor.current_proof(),
            include_bars=include_bars,
            first_cycle=pristine,
            cycle_started_at_epoch=cycle_started_at,
        )
        reservation_hash = str(reservation["reservation_sha256"])

        def before_get() -> None:
            self.ledger.assert_cycle_reservation_current(reservation_hash)

        if not self._authentication_proven:
            before_get()
            self.client.prove_authentication_required()
            self._authentication_proven = True

        before_get()
        state = self.client.get_state()
        state_observed_at = self._assert_observation_inside_window(self.clock())
        source = source_from_state(state, observed_at_epoch=state_observed_at)
        candidate_segment, reset_segment = self._candidate_segment(source)
        base_last_bar = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_bar_epoch_by_symbol)
        )
        base_tick_sequence = (
            {symbol: 0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_sequence_by_symbol)
        )
        base_tick_transport = (
            {symbol: 0.0 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_transport_epoch_by_symbol)
        )
        base_snapshot_hash = (
            {symbol: ZERO_SHA256 for symbol in SYMBOLS}
            if reset_segment
            else dict(self.last_tick_snapshot_sha256_by_symbol)
        )

        before_get()
        ticks_payload = self.client.get_ticks()
        ticks_observed_at = self._assert_observation_inside_window(self.clock())
        quotes = validate_tick_snapshot(
            ticks_payload,
            expected_source=source,
            observed_at_epoch=ticks_observed_at,
            prior_sequences=base_tick_sequence,
            prior_transport_epochs=base_tick_transport,
            prior_snapshot_sha256=base_snapshot_hash,
        )
        for quote in quotes:
            observation_times: tuple[Any, ...] = (
                quote.observation_epoch,
                quote.observed_at_epoch,
                quote.transport_received_at_epoch,
            )
            if quote.market_event_received_at_epoch is not None:
                observation_times = (
                    *observation_times,
                    quote.market_event_received_at_epoch,
                )
            for observed in observation_times:
                self._assert_observation_inside_window(observed)
        if pristine and (
            not quotes
            or {quote.symbol for quote in quotes} != SYMBOL_SET
            or max(quote.transport_received_at_epoch for quote in quotes)
            > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS
        ):
            raise CollectionRefusal("prospective_window_quote_start_edge_missed")

        candidate_bars: dict[str, tuple[M1ActivityBar, ...]] = {}
        final_last_bar = dict(base_last_bar)
        late_events: list[dict[str, Any]] = []
        if include_bars:
            for symbol in SYMBOLS:
                before_get()
                raw = self.client.get_m1_bars(symbol, limit=self.policy.bar_limit)
                raw_rows = raw.get("bars") if isinstance(raw, Mapping) else None
                if (
                    not isinstance(raw, Mapping)
                    or raw.get("limit") != self.policy.bar_limit
                    or isinstance(raw_rows, (str, bytes, bytearray, Mapping))
                    or not isinstance(raw_rows, Sequence)
                    or len(raw_rows) > self.policy.bar_limit
                ):
                    raise CollectionRefusal("bar_response_exceeds_sealed_limit")
                bar_observed_at = self._assert_observation_inside_window(self.clock())
                validated = validate_m1_bar_response(
                    raw,
                    symbol=symbol,
                    expected_source=source,
                    observed_at_epoch=bar_observed_at,
                    requested_limit=self.policy.bar_limit,
                )
                last_epoch = int(base_last_bar[symbol])
                seen_epochs = self.ledger.bar_epoch_coverage_by_symbol[symbol]
                fresh_rows: list[M1ActivityBar] = []
                for bar in validated:
                    if seen_epochs.contains(bar.minute_epoch):
                        continue
                    if self.ledger.contains_gap(symbol, bar.minute_epoch):
                        continue
                    if bar.minute_epoch <= last_epoch:
                        late_events.append(
                            {
                                "symbol": symbol,
                                "minute_epoch": bar.minute_epoch,
                                "watermark_epoch": last_epoch,
                                "late_payload_sha256": canonical_sha256(bar.to_dict()),
                                "disposition": "permanent_gap_not_backfilled",
                                "baseline_eligible": False,
                            }
                        )
                        continue
                    fresh_rows.append(bar)
                candidate_bars[symbol] = tuple(fresh_rows)
                if fresh_rows:
                    final_last_bar[symbol] = fresh_rows[-1].minute_epoch

        before_get()
        final_state = self.client.get_state()
        cycle_completed_at = self._assert_observation_inside_window(self.clock())
        if cycle_completed_at < cycle_started_at:
            raise CollectionRefusal("collector_clock_regressed")
        if cycle_completed_at >= self.binding.end_epoch_exclusive:
            raise CollectionRefusal("prospective_window_closed_during_cycle")
        final_source = source_from_state(final_state, observed_at_epoch=cycle_completed_at)
        if final_source != source:
            raise CollectionRefusal("market_source_rollover_during_cycle")
        if pristine and cycle_completed_at > self.binding.t0_epoch + MAXIMUM_START_EDGE_LAG_SECONDS:
            raise CollectionRefusal("prospective_window_first_cycle_work_late")
        self._producer_monitor().assert_unchanged()

        gap_records, final_gap_anchor = self.ledger.prepare_gap_records(
            late_events,
            source_id=source.source_id,
            observed_at_epoch=cycle_completed_at,
            binding=self.binding,
        )
        flat_bars = [
            bar.to_dict()
            for symbol in SYMBOLS
            for bar in candidate_bars.get(symbol, ())
        ]
        quote_groups: dict[str, list[TickObservation]] = {}
        for quote in quotes:
            utc_hour = datetime.fromtimestamp(
                quote.transport_received_at_epoch, tz=UTC
            ).strftime("%Y%m%dT%H")
            quote_groups.setdefault(utc_hour, []).append(quote)
        bar_hour = datetime.fromtimestamp(cycle_completed_at, tz=UTC).strftime("%Y%m%dT%H")
        hours = sorted(set(quote_groups) | ({bar_hour} if flat_bars or gap_records else set()))
        if not hours:
            raise CollectionRefusal("capture_cycle_has_no_durable_observation")

        running_last_bar = dict(base_last_bar)
        running_tick_sequence = dict(base_tick_sequence)
        running_tick_transport = dict(base_tick_transport)
        running_snapshot_hash = dict(base_snapshot_hash)
        latest_entry: dict[str, Any] | None = None
        gap_record_hour = bar_hour if gap_records else ""
        part_count = len(hours)
        for part_index, utc_hour in enumerate(hours, start=1):
            hour_quotes = quote_groups.get(utc_hour, [])
            for quote in hour_quotes:
                running_tick_sequence[quote.symbol] = quote.observation_sequence
                running_tick_transport[quote.symbol] = quote.transport_received_at_epoch
                running_snapshot_hash[quote.symbol] = quote.snapshot_sha256
            hour_bars = flat_bars if utc_hour == bar_hour else []
            if hour_bars:
                running_last_bar = dict(final_last_bar)
            hour_gap_records = gap_records if utc_hour == gap_record_hour else []
            hour_gap_anchor = final_gap_anchor if utc_hour == hours[-1] else self.ledger.gap_anchor
            part_final = part_index == part_count
            part = {
                "reservation_sequence": reservation["reservation_sequence"],
                "reservation_sha256": reservation_hash,
                "part_index": part_index,
                "part_count": part_count,
                "final": part_final,
            }
            chunk = {
                "schema_version": CHUNK_SCHEMA_VERSION,
                "collector_schema_version": COLLECTOR_SCHEMA_VERSION,
                "collector_source_sha256": MODULE_SOURCE_SHA256,
                "collector_wrapper_source_sha256": SUPPORT_SHA256,
                "collector_base_source_sha256": BASE_SUPPORT_SHA256,
                "source_contract_id": SOURCE_CONTRACT_ID,
                "activity_metric_id": ACTIVITY_METRIC_ID,
                "scope_version": SCOPE_VERSION,
                "symbol_scope": list(SYMBOLS),
                "timeframe": TIMEFRAME,
                "minimum_m1_history_bars": MINIMUM_M1_BARS,
                "maximum_quote_gap_seconds": MAXIMUM_TICK_INTERVAL_SECS,
                "requested_bar_limit": self.policy.bar_limit,
                "configured_tick_interval_seconds": self.policy.tick_interval_secs,
                "utc_hour": utc_hour,
                "segment_index": candidate_segment,
                "collector_cycle_started_at_epoch": cycle_started_at,
                "collector_cycle_completed_at_epoch": cycle_completed_at,
                "observed_at_epoch": cycle_completed_at,
                **self.binding.chunk_fields(),
                "source": source.sanitized(),
                "bars": hour_bars,
                "quotes": [quote.to_dict() for quote in hour_quotes],
                "last_bar_epoch_by_symbol": dict(running_last_bar),
                "last_tick_sequence_by_symbol": dict(running_tick_sequence),
                "last_tick_transport_epoch_by_symbol": dict(running_tick_transport),
                "last_tick_snapshot_sha256_by_symbol": dict(running_snapshot_hash),
                **_anchor_fields(hour_gap_anchor),
                "late_gap_records": hour_gap_records,
                "cycle_reservation_sequence": reservation[
                    "reservation_sequence"
                ],
                "cycle_reservation_sha256": reservation_hash,
                "cycle_part_index": part_index,
                "cycle_part_count": part_count,
                "cycle_part_final": part_final,
                "cycle_reservation_parts": [part],
                "interrupted_cycle_gap_evidence": [],
                "collection_only": True,
                "evaluation_performed": False,
                "success_claim_authorized": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            emitted = self.ledger.append_cycle(chunk)
            if emitted is not None:
                latest_entry = emitted
            self.current_source_id = source.source_id
            self.segment_index = candidate_segment
            self.last_bar_epoch_by_symbol = dict(running_last_bar)
            self.last_tick_sequence_by_symbol = dict(running_tick_sequence)
            self.last_tick_transport_epoch_by_symbol = dict(running_tick_transport)
            self.last_tick_snapshot_sha256_by_symbol = dict(running_snapshot_hash)
        if pristine:
            durable_at = _positive_float(self.clock(), "collector_clock_invalid")
            self.receipt.commit(durable_at_epoch=durable_at)
        return latest_entry


_POST_WINDOW_FINALIZATION_FIELDS = frozenset(
    {
        "schema_version",
        "capture_integrity_contract_sha256",
        "collector_source_sha256",
        "collector_wrapper_source_sha256",
        "collector_base_source_sha256",
        "preregistration_body_sha256",
        "preregistration_artifact_sha256",
        "prospective_t0_utc_inclusive",
        "prospective_end_utc_exclusive",
        "upstream_producer_software_body_sha256",
        "bridge_ea_repository_source_identity",
        "bridge_ea_deployed_source_identity",
        "bridge_ea_deployed_ex4_identity",
        "producer_monitor_proof",
        "finalized_after_end_observed_at_epoch",
        "manifest_sequence",
        "manifest_entry_sha256",
        "manifest_size_bytes",
        "tail_registry_record_count",
        "tail_registry_head_sha256",
        "tail_registry_tail_sha256",
        "tail_registry_artifact_sha256",
        "tail_committed_state_sha256",
        "start_edge_receipt_artifact_sha256",
        "network_requests_performed",
        "collection_only",
        "evaluation_performed",
        "success_claim_authorized",
        "authority_granted",
        "activation_authorized",
        "order_authorized",
        "receipt_sha256",
    }
)


def _validated_post_window_finalization_receipt(
    value: Any,
    *,
    binding: ProspectiveBinding,
    view: _TailRegistryView,
    manifest: _ManifestProjection,
    start_receipt_artifact_sha256: str,
    producer_monitor_proof: Mapping[str, Any],
) -> dict[str, Any]:
    reason = "post_window_finalization_receipt_invalid"
    if not isinstance(value, Mapping) or set(value) != _POST_WINDOW_FINALIZATION_FIELDS:
        raise CollectionRefusal(reason)
    receipt = dict(value)
    body = dict(receipt)
    claimed = str(body.pop("receipt_sha256", "")).lower()
    producer_fields = binding.producer_receipt_fields()
    if (
        receipt.get("schema_version")
        != POST_WINDOW_FINALIZATION_SCHEMA_VERSION
        or receipt.get("capture_integrity_contract_sha256")
        != capture_integrity_contract_sha256()
        or receipt.get("collector_source_sha256") != MODULE_SOURCE_SHA256
        or receipt.get("collector_wrapper_source_sha256") != SUPPORT_SHA256
        or receipt.get("collector_base_source_sha256") != BASE_SUPPORT_SHA256
        or tuple(
            receipt.get(key)
            for key in (
                "preregistration_body_sha256",
                "preregistration_artifact_sha256",
                "prospective_t0_utc_inclusive",
                "prospective_end_utc_exclusive",
            )
        )
        != (
            binding.preregistration_body_sha256,
            binding.preregistration_artifact_sha256,
            binding.t0_utc,
            binding.end_utc_exclusive,
        )
        or any(receipt.get(key) != expected for key, expected in producer_fields.items())
        or receipt.get("producer_monitor_proof")
        != dict(producer_monitor_proof)
        or _positive_float(
            receipt.get("finalized_after_end_observed_at_epoch"), reason
        )
        < binding.end_epoch_exclusive
        or receipt.get("manifest_sequence") != manifest.sequence
        or receipt.get("manifest_entry_sha256") != manifest.entry_sha256
        or receipt.get("manifest_size_bytes") != manifest.size_bytes
        or receipt.get("tail_registry_record_count") != view.record_count
        or receipt.get("tail_registry_head_sha256") != view.head_sha256
        or receipt.get("tail_registry_tail_sha256") != view.tail_sha256
        or receipt.get("tail_registry_artifact_sha256") != view.artifact_sha256
        or receipt.get("tail_committed_state_sha256")
        != canonical_sha256(view.committed_state)
        or receipt.get("start_edge_receipt_artifact_sha256")
        != start_receipt_artifact_sha256
        or receipt.get("network_requests_performed") is not False
        or receipt.get("collection_only") is not True
        or any(
            receipt.get(key) is not False
            for key in (
                "evaluation_performed",
                "success_claim_authorized",
                "authority_granted",
                "activation_authorized",
                "order_authorized",
            )
        )
        or not _is_sha256(claimed)
        or canonical_sha256(body) != claimed
    ):
        raise CollectionRefusal(reason)
    return receipt


def finalize_capture_after_window(
    *,
    output_root: str | Path,
    preregistration_path: str | Path,
    bridge_ea_repository_source: str | Path,
    bridge_ea_deployed_source: str | Path,
    bridge_ea_deployed_ex4: str | Path,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Finalize integrity after end-exclusive without constructing a client."""

    binding = load_preregistration(
        preregistration_path,
        bridge_ea_repository_source=bridge_ea_repository_source,
        bridge_ea_deployed_source=bridge_ea_deployed_source,
        bridge_ea_deployed_ex4=bridge_ea_deployed_ex4,
    )
    observed_at = _positive_float(clock(), "collector_clock_invalid")
    if observed_at < binding.end_epoch_exclusive:
        raise CollectionRefusal("post_window_finalization_before_end")
    monitor = binding.producer_software_monitor
    if monitor is None:
        raise CollectionRefusal("upstream_producer_software_paths_required")
    producer_monitor_proof = monitor.current_proof()
    root = Path(output_root)
    with ExclusiveDataWriterLock(root) as writer_lock:
        ledger = ManifestLedger(root, writer_lock=writer_lock)
        ledger.recover_unresolved_cycle(
            binding=binding,
            recovered_at_epoch=observed_at,
        )
        ledger.finalize_active()
        if monitor.current_proof() != producer_monitor_proof:
            raise CollectionRefusal("upstream_producer_software_drift")
        StartEdgeDurabilityReceipt(
            root,
            binding=binding,
            ledger=ledger,
            writer_lock=writer_lock,
        )
        start_receipt_raw, _start_identity = _read_bounded_regular_file(
            ledger.root / START_EDGE_RECEIPT_FILENAME,
            maximum_bytes=MAXIMUM_START_EDGE_RECEIPT_BYTES,
            missing_reason="start_edge_durable_receipt_missing",
            invalid_reason="start_edge_durable_receipt_invalid",
        )
        start_receipt_artifact_sha256 = hashlib.sha256(
            start_receipt_raw
        ).hexdigest()
        view = _stream_tail_commitment_registry(ledger.tail_commitment_path)
        if (
            view.pending_record is not None
            or view.committed_state["unresolved_cycle_reservation_sha256"]
            != ZERO_SHA256
            or view.committed_state["attempt_failure_sha256"] != ZERO_SHA256
        ):
            raise CollectionRefusal("post_window_finalization_tail_unresolved")
        manifest = _stream_manifest_projection(ledger.root)
        receipt_path = ledger.root / POST_WINDOW_FINALIZATION_FILENAME
        if _path_lexists(receipt_path):
            raw, _identity = _read_bounded_regular_file(
                receipt_path,
                maximum_bytes=MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES,
                missing_reason="post_window_finalization_receipt_missing",
                invalid_reason="post_window_finalization_receipt_invalid",
            )
            existing = _strict_json_object(
                raw, reason="post_window_finalization_receipt_invalid"
            )
            if raw != canonical_json_bytes(existing) + b"\n":
                raise CollectionRefusal(
                    "post_window_finalization_receipt_invalid"
                )
            receipt = _validated_post_window_finalization_receipt(
                existing,
                binding=binding,
                view=view,
                manifest=manifest,
                start_receipt_artifact_sha256=start_receipt_artifact_sha256,
                producer_monitor_proof=producer_monitor_proof,
            )
        else:
            body = {
                "schema_version": POST_WINDOW_FINALIZATION_SCHEMA_VERSION,
                "capture_integrity_contract_sha256": (
                    capture_integrity_contract_sha256()
                ),
                "collector_source_sha256": MODULE_SOURCE_SHA256,
                "collector_wrapper_source_sha256": SUPPORT_SHA256,
                "collector_base_source_sha256": BASE_SUPPORT_SHA256,
                **binding.chunk_fields(),
                **binding.producer_receipt_fields(),
                "producer_monitor_proof": producer_monitor_proof,
                "finalized_after_end_observed_at_epoch": observed_at,
                "manifest_sequence": manifest.sequence,
                "manifest_entry_sha256": manifest.entry_sha256,
                "manifest_size_bytes": manifest.size_bytes,
                "tail_registry_record_count": view.record_count,
                "tail_registry_head_sha256": view.head_sha256,
                "tail_registry_tail_sha256": view.tail_sha256,
                "tail_registry_artifact_sha256": view.artifact_sha256,
                "tail_committed_state_sha256": canonical_sha256(
                    view.committed_state
                ),
                "start_edge_receipt_artifact_sha256": (
                    start_receipt_artifact_sha256
                ),
                "network_requests_performed": False,
                "collection_only": True,
                "evaluation_performed": False,
                "success_claim_authorized": False,
                "authority_granted": False,
                "activation_authorized": False,
                "order_authorized": False,
            }
            receipt = {**body, "receipt_sha256": canonical_sha256(body)}
            _validated_post_window_finalization_receipt(
                receipt,
                binding=binding,
                view=view,
                manifest=manifest,
                start_receipt_artifact_sha256=start_receipt_artifact_sha256,
                producer_monitor_proof=producer_monitor_proof,
            )
            payload = canonical_json_bytes(receipt) + b"\n"
            if len(payload) > MAXIMUM_POST_WINDOW_FINALIZATION_RECEIPT_BYTES:
                raise CollectionRefusal(
                    "post_window_finalization_receipt_too_large"
                )
            _durable_atomic_write_new(receipt_path, payload)
        if monitor.current_proof() != producer_monitor_proof:
            raise CollectionRefusal("upstream_producer_software_drift")
    return {"status": "finalized", **receipt}


def run_collection(
    *,
    collector: ProspectiveActivityCollector,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> int:
    binding = collector.binding
    now = _positive_float(clock(), "collector_clock_invalid")
    while now < binding.t0_epoch:
        sleep(min(1.0, binding.t0_epoch - now))
        now = _positive_float(clock(), "collector_clock_invalid")
    if now >= binding.end_epoch_exclusive:
        raise CollectionRefusal("prospective_window_closed")
    collector.assert_start_edge_open(now)
    next_bar_capture = now
    cycles = 0
    # Conservatively retain the first-cycle authentication challenge GET in
    # the deadline budget even after this process has proved authentication.
    tick_budget = 4 * collector.client.timeout_secs
    bar_budget = (4 + len(SYMBOLS)) * collector.client.timeout_secs
    while True:
        cycle_started = _positive_float(clock(), "collector_clock_invalid")
        if cycle_started >= binding.end_epoch_exclusive - tick_budget:
            break
        include_bars = collector.is_pristine or cycle_started >= next_bar_capture
        if include_bars and not collector.is_pristine and cycle_started >= binding.end_epoch_exclusive - bar_budget:
            include_bars = False
        collector.capture_cycle(include_bars=include_bars)
        cycles += 1
        if include_bars:
            next_bar_capture = cycle_started + collector.policy.bar_interval_secs
        current = _positive_float(clock(), "collector_clock_invalid")
        remaining = binding.end_epoch_exclusive - tick_budget - current
        if remaining <= 0.0:
            break
        sleep_for = min(
            max(0.0, collector.policy.tick_interval_secs - (current - cycle_started)),
            remaining,
        )
        if sleep_for > 0.0:
            sleep(sleep_for)
    collector.ledger.finalize_active()
    return cycles


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Collect restart-resilient prospective IG-MT4 M1 inputs with "
            "main-chain anchored permanent gaps."
        )
    )
    parser.add_argument("--base-url")
    parser.add_argument("--api-key-file")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--bridge-ea-repository-source", required=True)
    parser.add_argument("--bridge-ea-deployed-source", required=True)
    parser.add_argument("--bridge-ea-deployed-ex4", required=True)
    parser.add_argument("--tick-interval-secs", type=float, default=DEFAULT_TICK_INTERVAL_SECS)
    parser.add_argument("--bar-interval-secs", type=float, default=DEFAULT_BAR_INTERVAL_SECS)
    parser.add_argument("--bar-limit", type=int, default=DEFAULT_BAR_LIMIT)
    parser.add_argument("--http-timeout-secs", type=float, default=DEFAULT_HTTP_TIMEOUT_SECS)
    parser.add_argument("--rollover-mode", choices=("refuse",), default="refuse")
    parser.add_argument(
        "--finalize-after-window",
        action="store_true",
        help="Finalize a completed window offline; no client or network is constructed.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.finalize_after_window:
            result = finalize_capture_after_window(
                output_root=args.output_dir,
                preregistration_path=args.preregistration,
                bridge_ea_repository_source=args.bridge_ea_repository_source,
                bridge_ea_deployed_source=args.bridge_ea_deployed_source,
                bridge_ea_deployed_ex4=args.bridge_ea_deployed_ex4,
            )
            print(json.dumps(result, sort_keys=True))
            return 0
        if not args.base_url or not args.api_key_file:
            raise CollectionRefusal(
                "base_url_and_api_key_file_required_for_collection"
            )
        binding = load_preregistration(
            args.preregistration,
            bridge_ea_repository_source=args.bridge_ea_repository_source,
            bridge_ea_deployed_source=args.bridge_ea_deployed_source,
            bridge_ea_deployed_ex4=args.bridge_ea_deployed_ex4,
        )
        api_key = read_api_key_file(args.api_key_file)
        client = BridgeReadClient(
            base_url=args.base_url,
            api_key=api_key,
            timeout_secs=args.http_timeout_secs,
        )
        with ExclusiveDataWriterLock(args.output_dir) as writer_lock:
            ledger = ManifestLedger(args.output_dir, writer_lock=writer_lock)
            receipt = StartEdgeDurabilityReceipt(
                args.output_dir,
                binding=binding,
                ledger=ledger,
                writer_lock=writer_lock,
            )
            collector = ProspectiveActivityCollector(
                client=client,
                ledger=ledger,
                binding=binding,
                receipt=receipt,
                policy=CollectionPolicy(
                    bar_limit=args.bar_limit,
                    tick_interval_secs=args.tick_interval_secs,
                    bar_interval_secs=args.bar_interval_secs,
                    rollover_mode=args.rollover_mode,
                ),
            )
            now = time.time()
            while now < binding.t0_epoch:
                time.sleep(min(1.0, binding.t0_epoch - now))
                now = time.time()
            if now >= binding.end_epoch_exclusive:
                raise CollectionRefusal("prospective_window_closed")
            collector.assert_start_edge_open(now)
            cycles = run_collection(collector=collector)
    except CollectionRefusal as exc:
        print(f"capture refused: {exc}")
        return 2
    print(
        json.dumps(
            {
                "status": "collected",
                "collection_only": True,
                "evaluation_performed": False,
                "authority_granted": False,
                "order_authorized": False,
                "cycles": cycles,
                "capture_integrity_policy_id": CAPTURE_INTEGRITY_POLICY_ID,
                "preregistration_body_sha256": binding.preregistration_body_sha256,
                "manifest": str((Path(args.output_dir) / MANIFEST_FILENAME).resolve()),
                "start_edge_durable_receipt": str(
                    (Path(args.output_dir) / START_EDGE_RECEIPT_FILENAME).resolve()
                ),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
