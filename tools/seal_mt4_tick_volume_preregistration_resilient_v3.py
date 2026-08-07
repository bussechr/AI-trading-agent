"""Seal the corrected, source-pinned MTVCLC gap-v3 declaration.

The crossed-T0 95306 declaration is an abandoned prior attempt and consumes
its 44 cells.  This sealer therefore freezes the next declared family at 4,786
prior cells plus 44 current cells, or 4,830 cumulatively.  Publication is
exclusive and is refused unless the target is durably visible at least ten
minutes before the future minute-aligned T0.

All active inputs are read through stable no-follow descriptors and rechecked
after publication.  The tool has no capture, network, credential, outcome,
issuer, activation, runtime, broker, or trade surface.
"""

from __future__ import annotations

# AGENT: ROLE: offline source-only sealer for the corrected gap-v3 attempt.
# AGENT: HANDSHAKE: stable frozen bytes -> future declaration -> exact v3 collector.
# AGENT: ISOLATION: local declaration inputs only; no live or authority surface.
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
import stat
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOOL_PATH = Path(__file__).resolve()
CATALOG_PATH = FXSTACK_SRC / "fxstack" / "providers" / "ig_mt4_catalog.py"
SCALP_ENGINE_IDENTITY_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "scalp_engine_identity.py"
)
SCREEN_BASE_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation.py"
)
SCREEN_V1_SUPPORT_PATH = SCREEN_BASE_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement.py"
)
SCREEN_V2_SUPPORT_PATH = SCREEN_BASE_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement_v2.py"
)
SCREEN_PATH = SCREEN_BASE_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement_v3.py"
)
ENTRY_SCHEMA_PATH = FXSTACK_SRC / "fxstack" / "schemas" / "entry.py"
ROLLOVER_GUARD_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "scalp_rollover_guard.py"
)
PRODUCTION_STRATEGY_PATH = (
    FXSTACK_SRC / "fxstack" / "strategy" / "scalp_dislocation.py"
)
BASE_SEALER_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration.py"
SEALER_V1_SUPPORT_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient.py"
)
SEALER_V2_SUPPORT_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v2.py"
)
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
COLLECTOR_BASE_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity.py"
COLLECTOR_RESILIENT_SUPPORT_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient.py"
)
COLLECTOR_V2_SUPPORT_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v2.py"
)
HANDOFF_PATH = REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"
LEGACY_HANDOFF_PATH = REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff.py"
EVALUATOR_PATH = REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"
LEGACY_EVALUATOR_PATH = REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window.py"
RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"
LEGACY_RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release.py"
PUBLIC_VERIFIER_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "mtvclc_validation_evidence_v2.py"
)
PUBLIC_VERIFIER_V1_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "mtvclc_validation_evidence.py"
)
BRIDGE_EA_REPOSITORY_SOURCE_PATH = REPO_ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"


@dataclass(frozen=True, slots=True)
class ExecutedSourceSnapshot:
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


def _source_path_is_reparse(path: Path, value: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(
        int(getattr(value, "st_file_attributes", 0)) & marker
    )


def _read_executed_source(
    path: str | Path,
    *,
    reason: str,
) -> ExecutedSourceSnapshot:
    candidate = Path(path).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            _source_path_is_reparse(candidate, before_path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > 8 * 1024 * 1024
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise RuntimeError(reason) from exc
    try:
        before_handle = os.fstat(descriptor)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(before_handle.st_size + 1)
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(reason) from exc
    identities = {
        _source_stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise RuntimeError(reason)
    return ExecutedSourceSnapshot(candidate, raw, identities.pop())


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
    previous = {name: sys.modules.get(name, missing) for name in installed}
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
    snapshot: ExecutedSourceSnapshot,
    *,
    module_name: str,
    injected_modules: Mapping[str, ModuleType] | None = None,
) -> ModuleType:
    module = ModuleType(module_name)
    module.__file__ = str(snapshot.path)
    module.__package__ = module_name.rpartition(".")[0]
    module.__dict__["__fxstack_exact_source_path__"] = snapshot.path
    module.__dict__["__fxstack_exact_source_raw__"] = snapshot.raw
    module.__dict__["__fxstack_exact_source_stat_identity__"] = (
        snapshot.stat_identity
    )
    bindings = dict(injected_modules or {})
    bindings[module_name] = module
    try:
        with _temporary_exact_modules(bindings):
            exec(  # noqa: S102 - exact stable descriptor snapshot; never workspace pyc
                compile(
                    snapshot.raw,
                    str(snapshot.path),
                    "exec",
                    dont_inherit=True,
                ),
                module.__dict__,
            )
    except Exception as exc:
        raise RuntimeError(f"exact_source_import_invalid:{snapshot.path.name}") from exc
    return module


def _self_executed_source_image() -> ExecutedSourceSnapshot:
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
        return ExecutedSourceSnapshot(TOOL_PATH, bound_raw, bound_identity)
    return _read_executed_source(TOOL_PATH, reason="sealer_source_invalid")


_SELF_SOURCE_IMAGE = _self_executed_source_image()
_CATALOG_SOURCE_IMAGE = _read_executed_source(
    CATALOG_PATH, reason="scope_catalog_source_invalid"
)
_ENGINE_IDENTITY_SOURCE_IMAGE = _read_executed_source(
    SCALP_ENGINE_IDENTITY_PATH, reason="scalp_engine_identity_source_invalid"
)
_SCREEN_BASE_SOURCE_IMAGE = _read_executed_source(
    SCREEN_BASE_PATH, reason="screen_base_source_invalid"
)
_SCREEN_V1_SOURCE_IMAGE = _read_executed_source(
    SCREEN_V1_SUPPORT_PATH, reason="screen_v1_source_invalid"
)
_SCREEN_V2_SOURCE_IMAGE = _read_executed_source(
    SCREEN_V2_SUPPORT_PATH, reason="screen_v2_source_invalid"
)
_SCREEN_V3_SOURCE_IMAGE = _read_executed_source(
    SCREEN_PATH, reason="screen_v3_source_invalid"
)
_ENTRY_SCHEMA_SOURCE_IMAGE = _read_executed_source(
    ENTRY_SCHEMA_PATH, reason="entry_schema_source_invalid"
)
_ROLLOVER_GUARD_SOURCE_IMAGE = _read_executed_source(
    ROLLOVER_GUARD_PATH, reason="rollover_guard_source_invalid"
)
_PRODUCTION_STRATEGY_SOURCE_IMAGE = _read_executed_source(
    PRODUCTION_STRATEGY_PATH, reason="production_strategy_source_invalid"
)
_BASE_SEALER_SOURCE_IMAGE = _read_executed_source(
    BASE_SEALER_PATH, reason="base_sealer_source_invalid"
)
_SEALER_V1_SOURCE_IMAGE = _read_executed_source(
    SEALER_V1_SUPPORT_PATH, reason="sealer_v1_source_invalid"
)
_SEALER_V2_SOURCE_IMAGE = _read_executed_source(
    SEALER_V2_SUPPORT_PATH, reason="sealer_v2_source_invalid"
)

_catalog = _execute_exact_source(
    _CATALOG_SOURCE_IMAGE, module_name="_fxstack_gap_v3_exact_catalog"
)
_engine_identity = _execute_exact_source(
    _ENGINE_IDENTITY_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_engine_identity",
)
_screen_base = _execute_exact_source(
    _SCREEN_BASE_SOURCE_IMAGE, module_name="_fxstack_gap_v3_exact_screen_base"
)
_screen_v1 = _execute_exact_source(
    _SCREEN_V1_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_screen_v1",
    injected_modules={
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation": (
            _screen_base
        )
    },
)
_screen_v2 = _execute_exact_source(
    _SCREEN_V2_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_screen_v2",
    injected_modules={
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation": (
            _screen_base
        )
    },
)
screen = _execute_exact_source(
    _SCREEN_V3_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_screen_v3",
    injected_modules={
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation": (
            _screen_base
        )
    },
)
_entry_schema = _execute_exact_source(
    _ENTRY_SCHEMA_SOURCE_IMAGE, module_name="_fxstack_gap_v3_exact_entry_schema"
)
_rollover_guard = _execute_exact_source(
    _ROLLOVER_GUARD_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_rollover_guard",
)
_production_strategy = _execute_exact_source(
    _PRODUCTION_STRATEGY_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_production_strategy",
    injected_modules={
        "fxstack.providers.ig_mt4_catalog": _catalog,
        "fxstack.schemas.entry": _entry_schema,
        "fxstack.runtime.scalp_rollover_guard": _rollover_guard,
    },
)
_base_sealer = _execute_exact_source(
    _BASE_SEALER_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_base_sealer",
    injected_modules={
        "fxstack.providers.ig_mt4_catalog": _catalog,
        "fxstack.runtime.scalp_engine_identity": _engine_identity,
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation": (
            _screen_base
        ),
        "fxstack.strategy.scalp_dislocation": _production_strategy,
    },
)
_v2 = _execute_exact_source(
    _SEALER_V2_SOURCE_IMAGE,
    module_name="_fxstack_gap_v3_exact_sealer_v2",
    injected_modules={
        "tools.seal_mt4_tick_volume_preregistration": _base_sealer,
        "fxstack.providers.ig_mt4_catalog": _catalog,
        "fxstack.scalp.screen_mt4_tick_volume_close_location_continuation": (
            _screen_base
        ),
        (
            "fxstack.scalp."
            "screen_mt4_tick_volume_close_location_continuation_replacement"
        ): _screen_v1,
        (
            "fxstack.scalp."
            "screen_mt4_tick_volume_close_location_continuation_replacement_v2"
        ): _screen_v2,
    },
)
if (
    _v2.base is not _base_sealer
    or _v2.replacement_screen is not _screen_v2
    or _v2.sealer_support.base is not _base_sealer
    or _v2.SEALER_SUPPORT_SHA256 != _SEALER_V1_SOURCE_IMAGE.sha256
    or _v2.SEALER_SUPPORT_SIZE_BYTES != _SEALER_V1_SOURCE_IMAGE.size_bytes
    or tuple(_v2.SEALER_SUPPORT_STAT_IDENTITY)
    != _SEALER_V1_SOURCE_IMAGE.stat_identity[:4]
):
    raise RuntimeError("sealer_v2_exact_dependency_binding_invalid")
base = _v2.base
IG_MT4_SCALP_SCOPE_VERSION = _catalog.IG_MT4_SCALP_SCOPE_VERSION
IG_MT4_SCALP_SYMBOLS = _catalog.IG_MT4_SCALP_SYMBOLS
IG_MT4_VENUE_ID = _catalog.IG_MT4_VENUE_ID
SCALP_ENGINE_COMPONENTS = _engine_identity.SCALP_ENGINE_COMPONENTS
SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS = (
    _engine_identity.SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS
)

DECLARATION_REVISION = "fxstack.scalp.mtvclc_gap_v3_preregistration.v1"
CAPTURE_PROFILE_ID = "gap_v3_source_pinned"
PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_786
CURRENT_ATTEMPTED_CELLS = 44
CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_830
MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024
MINIMUM_PUBLICATION_LEAD_SECONDS = 600.0
DEFAULT_SUCCESSOR_START_DELAY_SECONDS = 900

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
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

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

PINNED_SUPPORT_IDENTITIES: dict[str, tuple[str, int]] = {
    COLLECTOR_PATH.name: (
        "61b6a48b9eee7a3dc5fd615da23779b0fa876fb34049c912b791cf761e287501",
        298_993,
    ),
    COLLECTOR_BASE_PATH.name: (
        "87af2452ae3f0f3964c35b2904ad35fda28f3675e1f614e941ee46fd693bda5d",
        80_405,
    ),
    COLLECTOR_RESILIENT_SUPPORT_PATH.name: (
        "b38e69933e4910956dc92477252d33f4d5a92458e70036520654bc53102357f5",
        77_496,
    ),
    SCREEN_BASE_PATH.name: (
        "7e3dd3e829a4429e1316be2925f17d6ec3c9c0e3257e529cefd153e660558059",
        40_614,
    ),
}
PINNED_CAPTURE_INTEGRITY_CONTRACT_SHA256 = (
    "6cb5995d019cafe622caf09f171e0a0caec58185f7c121365c4c778b643f15ee"
)

_FIXED_FALSE_AUTHORITY = dict(base.FIXED_AUTHORITY_FLAGS)
COLLECTOR_CYCLE_REVALIDATION_CLAIMED = True
_REQUIRED_CAPTURE_TRUE_FIELDS = frozenset(
    {
        "bar_response_row_count_never_exceeds_sealed_limit",
        "bootstrap_first_cycle_finalized_immediately",
        "first_authenticated_finalized_observation_is_immutable",
        "covered_matching_later_overlap_is_ignored",
        "covered_revised_later_overlap_is_ignored",
        "covered_overlap_never_overwrites_or_duplicates_a_bar",
        "late_unseen_epoch_at_or_before_watermark_is_permanent_gap",
        "late_unseen_epoch_is_never_backfilled",
        "late_unseen_epoch_is_never_baseline_eligible",
        "late_gap_chain_embedded_in_every_cycle_journal_chunk_and_manifest",
        "late_gap_chain_count_root_tail_and_source_are_restart_anchors",
        (
            "late_gap_main_chain_deletion_truncation_or_rollback_relative_to_"
            "tail_commitment_refuses"
        ),
        "independently_mutable_late_gap_sidecar_forbidden",
        "gap_recovery_is_streamed_with_compact_epoch_intervals",
        "durable_per_symbol_watermark_never_regresses",
        "market_source_id_rollover_refuses",
        "gap_source_id_must_equal_capture_market_source_id_even_without_events",
        "downtime_gaps_are_preserved",
        "prospective_t0_is_utc_minute_aligned",
        "first_cycle_requires_complete_direct_m1_scope",
        "first_cycle_durable_commit_must_complete_by_t0_plus_30_seconds",
        "late_first_durability_is_permanent_attempt_refusal",
        "start_edge_miss_refuses_before_collection",
        "t0_reset_forbidden",
        "capture_restart_does_not_restart_or_extend_the_experiment",
        "bridge_ea_repository_and_deployed_source_must_match",
        "bridge_ea_repository_deployed_source_and_ex4_paths_are_pairwise_distinct",
        "bridge_ea_source_and_ex4_checked_before_and_after_every_capture_cycle",
        "bridge_ea_source_or_ex4_drift_refuses_collection",
        "chunk_read_size_is_bounded_before_allocation",
        "every_persisted_quote_observation_and_receipt_is_inside_window",
        "exclusive_output_data_writer_lock_required",
        "gap_v3_successor_envelope_and_4830_cell_family_required",
        "main_chain_rollback_or_deletion_is_detected_relative_to_tail_commitment",
        "manifest_append_never_rereads_or_replaces_complete_manifest",
        "output_ancestors_reparse_points_forbidden_before_and_after_resolve",
        "posix_parent_directory_fsync_after_namespace_publication",
        "preregistration_symlink_or_reparse_path_forbidden_before_resolve",
        "restart_manifest_and_journal_recovery_is_streaming_and_bounded",
        "root_chunk_hour_journal_manifest_and_tail_path_identities_are_stable",
        "tail_commitment_exact_pending_operation_is_idempotently_recovered",
        "tail_commitment_is_independent_append_only_hash_chain",
        "tail_commitment_path_and_handle_identity_are_stable",
        "tail_commitment_prepare_data_fsync_commit_ordering",
        "windows_namespace_publication_uses_movefileex_write_through",
    }
)
_REQUIRED_CAPTURE_FALSE_FIELDS = frozenset(
    {
        "coordinated_tail_commitment_and_data_rollback_detection_claimed",
        "windows_directory_fsync_claimed",
    }
)


class GapV3PreregistrationRefusal(RuntimeError):
    """Stable fail-closed refusal raised before a declaration can survive."""


@dataclass(frozen=True, slots=True)
class StableSnapshot:
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


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_reparse(path: Path) -> bool:
    try:
        value = path.lstat()
    except OSError:
        return True
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(int(getattr(value, "st_file_attributes", 0)) & marker)


def stable_snapshot(
    path: str | Path,
    *,
    reason: str,
    maximum_bytes: int = MAXIMUM_SOURCE_BYTES,
) -> StableSnapshot:
    """Read exact regular-file bytes through one no-follow descriptor."""

    candidate = Path(path).expanduser().absolute()
    if candidate.is_symlink() or _is_reparse(candidate):
        raise GapV3PreregistrationRefusal(reason)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise GapV3PreregistrationRefusal(reason) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
            raise GapV3PreregistrationRefusal(reason)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(maximum_bytes + 1)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if (
        len(raw) > maximum_bytes
        or len(raw) != before.st_size
        or _stat_identity(before) != _stat_identity(after)
    ):
        raise GapV3PreregistrationRefusal(reason)
    try:
        path_after = candidate.stat(follow_symlinks=False)
    except OSError as exc:
        raise GapV3PreregistrationRefusal(reason) from exc
    if _stat_identity(path_after) != _stat_identity(after):
        raise GapV3PreregistrationRefusal(reason)
    return StableSnapshot(candidate, raw, _stat_identity(after))


def _assert_same_snapshot(snapshot: StableSnapshot, *, reason: str) -> None:
    current = stable_snapshot(
        snapshot.path,
        reason=reason,
        maximum_bytes=max(MAXIMUM_SOURCE_BYTES, snapshot.size_bytes),
    )
    if (
        current.stat_identity != snapshot.stat_identity
        or current.raw != snapshot.raw
        or not hmac.compare_digest(current.sha256, snapshot.sha256)
    ):
        raise GapV3PreregistrationRefusal(reason)


def _assert_pinned_support(snapshot: StableSnapshot) -> None:
    expected = PINNED_SUPPORT_IDENTITIES.get(snapshot.path.name)
    if expected is None:
        return
    if snapshot.sha256 != expected[0] or snapshot.size_bytes != expected[1]:
        raise GapV3PreregistrationRefusal(
            f"trusted_support_identity_mismatch:{snapshot.path.name}"
        )


def _execute_collector(snapshot: StableSnapshot) -> ModuleType:
    name = "_fxstack_gap_v3_sealed_collector_" + snapshot.sha256[:16]
    try:
        module = _execute_exact_source(
            ExecutedSourceSnapshot(
                snapshot.path,
                snapshot.raw,
                snapshot.stat_identity,
            ),
            module_name=name,
        )
        if (
            module.MODULE_SOURCE_SHA256 != snapshot.sha256
            or module.MODULE_SOURCE_SIZE_BYTES != snapshot.size_bytes
        ):
            raise RuntimeError("collector_executed_source_identity_mismatch")
        return module
    except Exception as exc:
        raise GapV3PreregistrationRefusal("collector_source_import_invalid") from exc


def _capture_contract_valid(value: Any, *, collector_sha256: str) -> bool:
    if not isinstance(value, Mapping):
        return False
    return bool(
        value.get("schema_version")
        == "fxstack.scalp.mtvclc_capture_integrity_contract.v5"
        and value.get("contract_id")
        == (
            "first_authenticated_finalized_observation_or_declared_absence_wins_"
            "with_main_chain_anchors_and_pre_get_reservations.v5"
        )
        and base.canonical_sha256(dict(value))
        == PINNED_CAPTURE_INTEGRITY_CONTRACT_SHA256
        and str(value.get("collector_source_sha256") or "").lower()
        == collector_sha256
        and collector_sha256
        == PINNED_SUPPORT_IDENTITIES[COLLECTOR_PATH.name][0]
        and value.get("collector_wrapper_source_sha256")
        == PINNED_SUPPORT_IDENTITIES[COLLECTOR_RESILIENT_SUPPORT_PATH.name][0]
        and value.get("collector_wrapper_source_size_bytes")
        == PINNED_SUPPORT_IDENTITIES[COLLECTOR_RESILIENT_SUPPORT_PATH.name][1]
        and value.get("collector_base_source_sha256")
        == PINNED_SUPPORT_IDENTITIES[COLLECTOR_BASE_PATH.name][0]
        and value.get("collector_base_source_size_bytes")
        == PINNED_SUPPORT_IDENTITIES[COLLECTOR_BASE_PATH.name][1]
        and value.get("maximum_start_edge_lag_seconds") == 30.0
        and value.get("tick_interval_seconds") == 2.0
        and value.get("bar_interval_seconds") == 60.0
        and value.get("bar_limit") == 400
        and value.get("http_timeout_seconds") == 5.0
        and all(value.get(field) is True for field in _REQUIRED_CAPTURE_TRUE_FIELDS)
        and all(value.get(field) is False for field in _REQUIRED_CAPTURE_FALSE_FIELDS)
        and value.get("late_gap_record_schema_version")
        == "fxstack.external_ig_mt4_m1_late_gap_record.v2"
        and value.get("tail_commitment_filename")
        == "capture-tail-commitments.v2.jsonl"
        and value.get("tail_commitment_schema_version")
        == "fxstack.external_ig_mt4_m1_tail_commitment.v2"
        and value.get("pre_get_cycle_reservation_schema_version")
        == "fxstack.external_ig_mt4_m1_cycle_reservation.v1"
        and value.get("interrupted_cycle_gap_schema_version")
        == "fxstack.external_ig_mt4_m1_interrupted_cycle_gap.v1"
        and value.get("post_window_finalization_filename")
        == "post-window-finalization-receipt.v1.json"
        and value.get("post_window_finalization_schema_version")
        == "fxstack.external_ig_mt4_m1_post_window_finalization.v1"
        and value.get("reservation_is_fsynced_through_tail_wal_before_first_get")
        is True
        and value.get("reservation_is_asserted_current_immediately_before_every_get")
        is True
        and value.get("one_durable_pending_reservation_spans_entire_network_cycle")
        is True
        and value.get("post_window_finalization_is_integrity_only_and_network_free")
        is True
        and value.get("post_window_finalization_emits_no_evaluation_or_authority")
        is True
        and value.get(
            "full_tail_validation_required_when_writer_absent_stale_"
            "restarted_or_post_window"
        )
        is True
        and value.get("persistence_mode")
        == (
            "tail_wal_prepare_then_fsynced_active_journal_or_immutable_chunk_"
            "and_append_only_manifest_then_tail_wal_commit"
        )
        and value.get("active_hour_journal_filename")
        == "active-hour.journal.sha256.jsonl"
        and value.get("portable_chunk_schema_version")
        == "fxstack.external_ig_mt4_m1_activity_chunk.v2"
        and value.get("portable_manifest_schema_version")
        == "fxstack.external_ig_mt4_m1_activity_manifest_entry.v2"
        and value.get("start_edge_durable_receipt_filename")
        == "start-edge-durable-receipt.v1.json"
        and value.get("start_edge_durable_receipt_schema_version")
        == "fxstack.external_ig_mt4_m1_start_edge_durable_receipt.v1"
    )


def _identity_valid(value: Any, *, filename: str) -> bool:
    return bool(
        isinstance(value, Mapping)
        and set(value) == {"filename", "sha256", "size_bytes"}
        and value.get("filename") == filename
        and base._is_sha256(value.get("sha256"))
        and isinstance(value.get("size_bytes"), int)
        and not isinstance(value.get("size_bytes"), bool)
        and value.get("size_bytes") > 0
    )


_EXECUTED_SOURCE_IMAGES_BY_LABEL: dict[str, ExecutedSourceSnapshot] = {
    "sealer_source": _SELF_SOURCE_IMAGE,
    "scope_catalog_source": _CATALOG_SOURCE_IMAGE,
    "scalp_engine_identity_source": _ENGINE_IDENTITY_SOURCE_IMAGE,
    "screen_support_source": _SCREEN_BASE_SOURCE_IMAGE,
    "sealer_v1_screen_support_source": _SCREEN_V1_SOURCE_IMAGE,
    "sealer_v2_screen_support_source": _SCREEN_V2_SOURCE_IMAGE,
    "screen_source": _SCREEN_V3_SOURCE_IMAGE,
    "production_entry_schema_source": _ENTRY_SCHEMA_SOURCE_IMAGE,
    "production_rollover_guard_source": _ROLLOVER_GUARD_SOURCE_IMAGE,
    "production_strategy_policy_source": _PRODUCTION_STRATEGY_SOURCE_IMAGE,
    "sealer_base_source": _BASE_SEALER_SOURCE_IMAGE,
    "sealer_v1_support_source": _SEALER_V1_SOURCE_IMAGE,
    "sealer_v2_support_source": _SEALER_V2_SOURCE_IMAGE,
}


def executed_source_identities() -> dict[str, dict[str, Any]]:
    """Return identities of every local module executed during v3 sealer boot."""

    return {
        label: snapshot.identity()
        for label, snapshot in _EXECUTED_SOURCE_IMAGES_BY_LABEL.items()
    }


REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS = frozenset(
    set(_EXECUTED_SOURCE_IMAGES_BY_LABEL)
    | {
        "sealer_source",
        "collector_source",
        "collector_base_source",
        "collector_support_source",
        "handoff_verifier_source",
        "legacy_handoff_support_source",
        "evaluator_source",
        "legacy_evaluator_support_source",
        "release_source",
        "legacy_release_support_source",
        "public_verifier_source",
        "public_verifier_v1_support_source",
    }
)


def execution_provenance_contract() -> dict[str, Any]:
    return {
        "schema_version": "fxstack.scalp.mtvclc_execution_provenance.v1",
        "required_local_executable_source_identities": sorted(
            REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS
        ),
        "execution_mode": "stable_descriptor_snapshot_exec_compile_exact_path",
        "workspace_pyc_execution_allowed": False,
        "dependency_modules_injected_from_exact_snapshots": True,
        "temporary_sys_modules_bindings_restored": True,
        "executed_source_bytes_rechecked_before_publication": True,
        "authority_or_evidence_dependency_omission_allowed": False,
    }


def _source_paths() -> dict[str, Path]:
    paths = {
        "collector_source": COLLECTOR_PATH,
        "collector_base_source": COLLECTOR_BASE_PATH,
        "collector_support_source": COLLECTOR_RESILIENT_SUPPORT_PATH,
        "sealer_v2_collector_support_source": COLLECTOR_V2_SUPPORT_PATH,
        "screen_source": SCREEN_PATH,
        "screen_support_source": SCREEN_BASE_PATH,
        "sealer_v1_screen_support_source": SCREEN_V1_SUPPORT_PATH,
        "sealer_v2_screen_support_source": SCREEN_V2_SUPPORT_PATH,
        "sealer_source": TOOL_PATH,
        "sealer_base_source": BASE_SEALER_PATH,
        "sealer_v1_support_source": SEALER_V1_SUPPORT_PATH,
        "sealer_v2_support_source": SEALER_V2_SUPPORT_PATH,
        "scope_catalog_source": CATALOG_PATH,
        "scalp_engine_identity_source": SCALP_ENGINE_IDENTITY_PATH,
        "production_entry_schema_source": ENTRY_SCHEMA_PATH,
        "production_rollover_guard_source": ROLLOVER_GUARD_PATH,
        "production_strategy_policy_source": PRODUCTION_STRATEGY_PATH,
        "handoff_verifier_source": HANDOFF_PATH,
        "legacy_handoff_support_source": LEGACY_HANDOFF_PATH,
        "evaluator_source": EVALUATOR_PATH,
        "legacy_evaluator_support_source": LEGACY_EVALUATOR_PATH,
        "release_source": RELEASE_PATH,
        "legacy_release_support_source": LEGACY_RELEASE_PATH,
        "public_verifier_source": PUBLIC_VERIFIER_PATH,
        "public_verifier_v1_support_source": PUBLIC_VERIFIER_V1_PATH,
    }
    package_root = FXSTACK_SRC / "fxstack"
    for relative in SCALP_ENGINE_COMPONENTS:
        component_root = (
            REPO_ROOT
            if relative in SCALP_ENGINE_REQUIRED_BRIDGE_COMPONENTS
            else package_root
        )
        paths[f"production_engine_component:{relative}"] = component_root / relative
    return paths


def _normalized_source_sha256(snapshot: StableSnapshot) -> str:
    try:
        text = snapshot.raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GapV3PreregistrationRefusal(
            f"production_engine_component_invalid:{snapshot.path.name}"
        ) from exc
    normalized = text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


_FIXED_INPUT_SNAPSHOT_NAMES = frozenset(
    {
        "cost_capture_json",
        "cost_capture_npz",
        "fee_attestation",
        "bridge_ea_deployed_source",
        "bridge_ea_deployed_ex4",
    }
)
_FEE_SOURCE_DOCUMENT_SNAPSHOT_PREFIX = "fee_source_document:"


def _fee_source_document_snapshot_key(role: str) -> str:
    return f"{_FEE_SOURCE_DOCUMENT_SNAPSHOT_PREFIX}{role}"


def _fee_source_document_paths(
    attestation_snapshot: StableSnapshot,
) -> dict[str, Path]:
    """Resolve the exact local documents named by stable attestation bytes."""

    try:
        decoded = json.loads(attestation_snapshot.raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GapV3PreregistrationRefusal(
            "fee_source_document_snapshot_contract_invalid"
        ) from exc
    raw_documents = (
        decoded.get("source_documents") if isinstance(decoded, Mapping) else None
    )
    if not isinstance(raw_documents, list):
        raise GapV3PreregistrationRefusal(
            "fee_source_document_snapshot_contract_invalid"
        )
    documents_by_role: dict[str, Mapping[str, Any]] = {}
    required_row_fields = {
        "role",
        "url",
        "path",
        "sha256",
        "retrieved_at_utc",
    }
    for row in raw_documents:
        if not isinstance(row, Mapping) or set(row) != required_row_fields:
            raise GapV3PreregistrationRefusal(
                "fee_source_document_snapshot_contract_invalid"
            )
        role = str(row.get("role") or "")
        if role in documents_by_role:
            raise GapV3PreregistrationRefusal(
                "fee_source_document_snapshot_contract_invalid"
            )
        documents_by_role[role] = row
    if set(documents_by_role) != set(base.SOURCE_DOCUMENT_URLS):
        raise GapV3PreregistrationRefusal(
            "fee_source_document_snapshot_contract_invalid"
        )
    try:
        root = attestation_snapshot.path.resolve(strict=True).parent
    except OSError as exc:
        raise GapV3PreregistrationRefusal(
            "fee_source_document_snapshot_contract_invalid"
        ) from exc
    paths: dict[str, Path] = {}
    for role in base.SOURCE_DOCUMENT_URLS:
        relative = Path(str(documents_by_role[role].get("path") or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise GapV3PreregistrationRefusal(
                "fee_source_document_snapshot_contract_invalid"
            )
        paths[_fee_source_document_snapshot_key(role)] = root / relative
    return paths


def _input_snapshots(
    *,
    cost_capture_json: str | Path,
    cost_capture_npz: str | Path,
    fee_attestation: str | Path,
    bridge_ea_deployed_source: str | Path,
    bridge_ea_deployed_ex4: str | Path,
) -> tuple[dict[str, StableSnapshot], dict[str, StableSnapshot]]:
    sources = {
        name: stable_snapshot(path, reason=f"active_source_invalid:{name}")
        for name, path in _source_paths().items()
    }
    for snapshot in sources.values():
        _assert_pinned_support(snapshot)
    for label, executed in _EXECUTED_SOURCE_IMAGES_BY_LABEL.items():
        sealed = sources.get(label)
        if (
            sealed is None
            or sealed.path != executed.path
            or sealed.stat_identity != executed.stat_identity
            or sealed.raw != executed.raw
            or not hmac.compare_digest(sealed.sha256, executed.sha256)
        ):
            raise GapV3PreregistrationRefusal(
                f"executed_source_changed_before_seal:{label}"
            )
    inputs = {
        "cost_capture_json": stable_snapshot(
            cost_capture_json,
            reason="cost_capture_json_changed_or_invalid",
            maximum_bytes=64 * 1024 * 1024,
        ),
        "cost_capture_npz": stable_snapshot(
            cost_capture_npz,
            reason="cost_capture_npz_changed_or_invalid",
            maximum_bytes=512 * 1024 * 1024,
        ),
        "fee_attestation": stable_snapshot(
            fee_attestation,
            reason="fee_attestation_changed_or_invalid",
            maximum_bytes=16 * 1024 * 1024,
        ),
        "bridge_ea_deployed_source": stable_snapshot(
            bridge_ea_deployed_source,
            reason="bridge_ea_deployed_source_changed_or_invalid",
            maximum_bytes=16 * 1024 * 1024,
        ),
        "bridge_ea_deployed_ex4": stable_snapshot(
            bridge_ea_deployed_ex4,
            reason="bridge_ea_deployed_ex4_changed_or_invalid",
            maximum_bytes=64 * 1024 * 1024,
        ),
    }
    for name, path in _fee_source_document_paths(inputs["fee_attestation"]).items():
        inputs[name] = stable_snapshot(
            path,
            reason=f"{name}_changed_or_invalid",
            maximum_bytes=base.MAXIMUM_INPUT_BYTES,
        )
    repository_ea = sources["production_engine_component:MQL4/Experts/BridgeEA.mq4"]
    deployed_ea = inputs["bridge_ea_deployed_source"]
    deployed_ex4 = inputs["bridge_ea_deployed_ex4"]
    if (
        deployed_ea.path.name != "BridgeEA.mq4"
        or deployed_ex4.path.name != "BridgeEA.ex4"
        or os.path.normcase(str(repository_ea.path))
        == os.path.normcase(str(deployed_ea.path))
        or repository_ea.raw != deployed_ea.raw
        or repository_ea.sha256 != deployed_ea.sha256
    ):
        raise GapV3PreregistrationRefusal("bridge_ea_deployment_identity_mismatch")
    return sources, inputs


def _recheck_all(
    sources: Mapping[str, StableSnapshot], inputs: Mapping[str, StableSnapshot]
) -> None:
    for name, snapshot in {**sources, **inputs}.items():
        _assert_same_snapshot(snapshot, reason=f"sealed_input_changed:{name}")


def _producer_software_contract(
    *,
    repository_source: Mapping[str, Any],
    deployed_source: Mapping[str, Any],
    deployed_ex4: Mapping[str, Any],
) -> dict[str, Any]:
    body = {
        "schema_version": "fxstack.scalp.mtvclc_upstream_producer_software.v1",
        "repository_source": dict(repository_source),
        "deployed_source": dict(deployed_source),
        "deployed_ex4": dict(deployed_ex4),
        "repository_and_deployed_source_bytes_identical_at_seal_time": True,
        "all_three_identities_rechecked_before_publication": True,
        "collector_cycle_revalidation_claimed": COLLECTOR_CYCLE_REVALIDATION_CLAIMED,
        "runtime_or_broker_authority_derived_from_identity": False,
    }
    return {**body, "producer_software_body_sha256": base.canonical_sha256(body)}


def preservation_filename_contract() -> dict[str, Any]:
    return {
        "schema_version": "fxstack.scalp.mtvclc_preservation_filenames.v3",
        "capture_profile_id": CAPTURE_PROFILE_ID,
        "preregistration_filename_template": (
            "mtvclc_gap_v3_preregistration_{preregistration_body_sha256}.json"
        ),
        "capture_root_name_template": (
            "mtvclc_prospective_capture_gap_v3_{preregistration_body_sha256_prefix16}"
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


def _corrected_crossed_attempt() -> dict[str, Any]:
    return dict(FAILED_CROSSED_T0_ATTEMPT)


def _expected_abandoned_attempts() -> list[dict[str, Any]]:
    expected = _v2._abandoned_attempts()
    if not expected or expected[-1] != _v2.CROSSED_T0_REFUSED_ATTEMPT:
        raise GapV3PreregistrationRefusal("crossed_t0_lineage_missing")
    expected[-1] = _corrected_crossed_attempt()
    return expected


def build_preregistration(
    *,
    cost_capture_json: str | Path,
    cost_capture_npz: str | Path,
    fee_attestation: str | Path,
    bridge_ea_deployed_source: str | Path,
    bridge_ea_deployed_ex4: str | Path,
    sealed_at: datetime,
    start_delay_seconds: int = DEFAULT_SUCCESSOR_START_DELAY_SECONDS,
) -> dict[str, Any]:
    """Build from stable bytes without publishing or starting collection."""

    sources, inputs = _input_snapshots(
        cost_capture_json=cost_capture_json,
        cost_capture_npz=cost_capture_npz,
        fee_attestation=fee_attestation,
        bridge_ea_deployed_source=bridge_ea_deployed_source,
        bridge_ea_deployed_ex4=bridge_ea_deployed_ex4,
    )
    collector_module = _execute_collector(sources["collector_source"])
    try:
        contract = collector_module.expected_capture_integrity_contract()
    except Exception as exc:
        raise GapV3PreregistrationRefusal("collector_contract_unavailable") from exc
    if not _capture_contract_valid(
        contract, collector_sha256=sources["collector_source"].sha256
    ):
        raise GapV3PreregistrationRefusal("collector_contract_invalid")
    try:
        payload = _v2.build_preregistration(
            cost_capture_json=cost_capture_json,
            cost_capture_npz=cost_capture_npz,
            fee_attestation=fee_attestation,
            sealed_at=sealed_at,
            start_delay_seconds=start_delay_seconds,
        )
    except _v2.GapLedgerPreregistrationRefusal as exc:
        raise GapV3PreregistrationRefusal(str(exc)) from None

    payload.pop("preregistration_body_sha256", None)
    payload["declaration_revision"] = DECLARATION_REVISION
    payload["capture_profile_id"] = CAPTURE_PROFILE_ID
    payload["attempt_accounting"] = {
        "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
        "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        ),
    }
    abandoned = list(payload.get("abandoned_preregistrations") or [])
    if abandoned != _v2._abandoned_attempts():
        raise GapV3PreregistrationRefusal("crossed_t0_lineage_missing")
    abandoned[-1] = _corrected_crossed_attempt()
    payload["abandoned_preregistrations"] = abandoned
    payload["replacement_lineage"] = dict(REPLACEMENT_LINEAGE)
    payload["capture_integrity_contract"] = dict(contract)
    payload["execution_provenance_contract"] = execution_provenance_contract()
    payload["preservation_filename_contract"] = preservation_filename_contract()

    strategy = payload.get("strategy")
    gates = payload.get("fixed_success_gates")
    if not isinstance(strategy, dict) or not isinstance(gates, dict):
        raise GapV3PreregistrationRefusal("strategy_envelope_invalid")
    manifest = screen.attempt_manifest()
    strategy["attempt_manifest"] = manifest
    strategy["attempt_manifest_sha256"] = base.canonical_sha256(manifest)
    gates["descriptive_df99_bonferroni_abs_t_threshold"] = (
        screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
    )
    gates["cell_win_probability_interval"] = (
        "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
    )
    identities = payload.get("source_identities")
    if not isinstance(identities, dict):
        raise GapV3PreregistrationRefusal("source_identities_invalid")
    preserved_cost = identities.get("cost_capture")
    preserved_fee = identities.get("fee_attestation")
    preserved_runtime = identities.get("production_runtime_context")
    identities.clear()
    identities.update({name: snapshot.identity() for name, snapshot in sources.items()})
    identities["bridge_ea_deployed_source"] = inputs[
        "bridge_ea_deployed_source"
    ].identity()
    identities["bridge_ea_deployed_ex4"] = inputs[
        "bridge_ea_deployed_ex4"
    ].identity()
    identities["cost_capture"] = preserved_cost
    identities["fee_attestation"] = preserved_fee
    identities["production_runtime_context"] = preserved_runtime

    if not isinstance(preserved_runtime, Mapping):
        raise GapV3PreregistrationRefusal("production_engine_identity_invalid")
    engine_identity = preserved_runtime.get("engine_identity")
    component_rows = (
        engine_identity.get("component_sha256")
        if isinstance(engine_identity, Mapping)
        else None
    )
    expected_components = {
        relative: _normalized_source_sha256(
            sources[f"production_engine_component:{relative}"]
        )
        for relative in SCALP_ENGINE_COMPONENTS
    }
    if (
        not isinstance(component_rows, list)
        or component_rows
        != [
            [relative, expected_components[relative]]
            for relative in SCALP_ENGINE_COMPONENTS
        ]
    ):
        raise GapV3PreregistrationRefusal("production_engine_identity_invalid")

    repository_ea_identity = identities[
        "production_engine_component:MQL4/Experts/BridgeEA.mq4"
    ]
    payload["upstream_producer_software"] = _producer_software_contract(
        repository_source=repository_ea_identity,
        deployed_source=identities["bridge_ea_deployed_source"],
        deployed_ex4=identities["bridge_ea_deployed_ex4"],
    )

    _recheck_all(sources, inputs)
    payload["preregistration_body_sha256"] = base.canonical_sha256(payload)
    if not validate_preregistration(payload):
        raise GapV3PreregistrationRefusal("gap_v3_envelope_invalid")
    _recheck_all(sources, inputs)
    return payload


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Pure deep validation; this function performs no filesystem access."""

    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not base._is_sha256(claimed) or not hmac.compare_digest(
        claimed, base.canonical_sha256(body)
    ):
        return False
    accounting = {
        "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
        "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        ),
    }
    abandoned = body.get("abandoned_preregistrations")
    scope = body.get("scope")
    execution = body.get("execution_contract")
    window = body.get("prospective_window")
    strategy = body.get("strategy")
    gates = body.get("fixed_success_gates")
    identities = body.get("source_identities")
    if (
        body.get("declaration_revision") != DECLARATION_REVISION
        or body.get("capture_profile_id") != CAPTURE_PROFILE_ID
        or body.get("schema_version") != base.PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != _FIXED_FALSE_AUTHORITY
        or body.get("attempt_accounting") != accounting
        or not isinstance(abandoned, list)
        or abandoned != _expected_abandoned_attempts()
        or body.get("replacement_lineage") != REPLACEMENT_LINEAGE
        or body.get("execution_provenance_contract")
        != execution_provenance_contract()
        or not isinstance(scope, Mapping)
        or scope.get("venue_id") != IG_MT4_VENUE_ID
        or scope.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or scope.get("ordered_symbols") != list(IG_MT4_SCALP_SYMBOLS)
        or "XRPUSD" in scope.get("ordered_symbols", [])
        or not isinstance(execution, Mapping)
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or not isinstance(strategy, Mapping)
        or strategy.get("attempt_manifest") != screen.attempt_manifest()
        or strategy.get("attempt_manifest_sha256")
        != base.canonical_sha256(screen.attempt_manifest())
        or not isinstance(gates, Mapping)
        or gates.get("cell_win_probability_interval")
        != "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
        or body.get("preservation_filename_contract")
        != preservation_filename_contract()
        or not isinstance(identities, Mapping)
    ):
        return False
    expected_sources = _source_paths()
    if (
        not REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS.issubset(expected_sources)
        or not set(_EXECUTED_SOURCE_IMAGES_BY_LABEL).issubset(
            REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS
        )
    ):
        return False
    expected_identity_names = set(expected_sources) | {
        "bridge_ea_deployed_source",
        "bridge_ea_deployed_ex4",
        "cost_capture",
        "fee_attestation",
        "production_runtime_context",
    }
    if set(identities) != expected_identity_names:
        return False
    for name, path in expected_sources.items():
        if not _identity_valid(identities.get(name), filename=path.name):
            return False
    for name, (digest, size) in PINNED_SUPPORT_IDENTITIES.items():
        key = next(
            (label for label, path in expected_sources.items() if path.name == name),
            "",
        )
        identity = identities.get(key)
        if (
            not isinstance(identity, Mapping)
            or identity.get("sha256") != digest
            or identity.get("size_bytes") != size
        ):
            return False
    collector_identity = identities.get("collector_source")
    contract = body.get("capture_integrity_contract")
    if not isinstance(collector_identity, Mapping) or not _capture_contract_valid(
        contract,
        collector_sha256=str(collector_identity.get("sha256") or ""),
    ):
        return False
    repository_ea = identities.get(
        "production_engine_component:MQL4/Experts/BridgeEA.mq4"
    )
    deployed_ea = identities.get("bridge_ea_deployed_source")
    deployed_ex4 = identities.get("bridge_ea_deployed_ex4")
    producer_contract = body.get("upstream_producer_software")
    if (
        not _identity_valid(repository_ea, filename="BridgeEA.mq4")
        or not _identity_valid(deployed_ea, filename="BridgeEA.mq4")
        or not _identity_valid(deployed_ex4, filename="BridgeEA.ex4")
        or repository_ea != deployed_ea
        or producer_contract
        != _producer_software_contract(
            repository_source=repository_ea,
            deployed_source=deployed_ea,
            deployed_ex4=deployed_ex4,
        )
    ):
        return False
    cost = identities.get("cost_capture")
    fee = identities.get("fee_attestation")
    if (
        not isinstance(cost, Mapping)
        or not _identity_valid(
            cost.get("capture_json"), filename="ig_mt4_bid_ask_capture.json"
        )
        or not _identity_valid(
            cost.get("capture_npz"), filename="ig_mt4_bid_ask_samples.npz"
        )
        or not isinstance(fee, Mapping)
        or not _identity_valid(
            fee.get("attestation"), filename="mtvclc_fee_attestation.json"
        )
    ):
        return False
    if not isinstance(window, Mapping):
        return False
    try:
        sealed = base._parse_utc_second(body.get("sealed_at_utc"), label="sealed")
        t0 = base._parse_utc_second(window.get("t0_utc_inclusive"), label="t0")
        end = base._parse_utc_second(window.get("end_utc_exclusive"), label="end")
    except base.PreregistrationRefusal:
        return False
    return bool(
        t0 > sealed
        and (t0 - sealed).total_seconds() >= MINIMUM_PUBLICATION_LEAD_SECONDS
        and t0.second == 0
        and t0.microsecond == 0
        and end - t0 == timedelta(days=base.PROSPECTIVE_WINDOW_DAYS)
        and window.get("early_success_forbidden") is True
    )


def _parse_t0_epoch(payload: Mapping[str, Any]) -> float:
    window = payload.get("prospective_window")
    if not isinstance(window, Mapping):
        raise GapV3PreregistrationRefusal("prospective_window_invalid")
    try:
        t0 = base._parse_utc_second(window.get("t0_utc_inclusive"), label="t0")
    except base.PreregistrationRefusal as exc:
        raise GapV3PreregistrationRefusal("prospective_window_invalid") from exc
    if t0.second != 0 or t0.microsecond != 0:
        raise GapV3PreregistrationRefusal("t0_not_minute_aligned")
    return t0.timestamp()


def _assert_publication_snapshots_bound(
    payload: Mapping[str, Any],
    *,
    source_snapshots: Mapping[str, StableSnapshot],
    input_snapshots: Mapping[str, StableSnapshot],
) -> None:
    identities = payload.get("source_identities")
    expected_source_names = set(_source_paths())
    expected_input_names = set(_FIXED_INPUT_SNAPSHOT_NAMES) | {
        _fee_source_document_snapshot_key(role)
        for role in base.SOURCE_DOCUMENT_URLS
    }
    if (
        not isinstance(identities, Mapping)
        or set(source_snapshots) != expected_source_names
        or set(input_snapshots) != expected_input_names
        or any(
            identities.get(name) != snapshot.identity()
            for name, snapshot in source_snapshots.items()
        )
        or identities.get("bridge_ea_deployed_source")
        != input_snapshots["bridge_ea_deployed_source"].identity()
        or identities.get("bridge_ea_deployed_ex4")
        != input_snapshots["bridge_ea_deployed_ex4"].identity()
        or os.path.normcase(
            str(
                source_snapshots[
                    "production_engine_component:MQL4/Experts/BridgeEA.mq4"
                ].path
            )
        )
        == os.path.normcase(str(input_snapshots["bridge_ea_deployed_source"].path))
    ):
        raise GapV3PreregistrationRefusal("publication_snapshot_binding_invalid")
    cost = identities.get("cost_capture")
    fee = identities.get("fee_attestation")
    if (
        not isinstance(cost, Mapping)
        or cost.get("capture_json")
        != input_snapshots["cost_capture_json"].identity()
        or cost.get("capture_npz") != input_snapshots["cost_capture_npz"].identity()
        or not isinstance(fee, Mapping)
        or fee.get("attestation") != input_snapshots["fee_attestation"].identity()
    ):
        raise GapV3PreregistrationRefusal("publication_snapshot_binding_invalid")
    raw_fee_documents = fee.get("source_documents")
    if not isinstance(raw_fee_documents, list):
        raise GapV3PreregistrationRefusal("publication_snapshot_binding_invalid")
    fee_documents_by_role = {
        str(row.get("role") or ""): row
        for row in raw_fee_documents
        if isinstance(row, Mapping)
    }
    attestation_snapshot = input_snapshots.get("fee_attestation")
    if not isinstance(attestation_snapshot, StableSnapshot):
        raise GapV3PreregistrationRefusal("publication_snapshot_binding_invalid")
    try:
        fee_source_paths = _fee_source_document_paths(attestation_snapshot)
    except GapV3PreregistrationRefusal:
        raise GapV3PreregistrationRefusal(
            "publication_snapshot_binding_invalid"
        ) from None
    if set(fee_documents_by_role) != set(base.SOURCE_DOCUMENT_URLS):
        raise GapV3PreregistrationRefusal("publication_snapshot_binding_invalid")
    for role in base.SOURCE_DOCUMENT_URLS:
        name = _fee_source_document_snapshot_key(role)
        snapshot = input_snapshots.get(name)
        document = fee_documents_by_role[role]
        if (
            not isinstance(snapshot, StableSnapshot)
            or os.path.normcase(str(snapshot.path))
            != os.path.normcase(str(fee_source_paths[name].absolute()))
            or {
                "filename": document.get("filename"),
                "sha256": document.get("sha256"),
                "size_bytes": document.get("size_bytes"),
            }
            != snapshot.identity()
        ):
            raise GapV3PreregistrationRefusal(
                "publication_snapshot_binding_invalid"
            )


def atomic_publish(
    *,
    output_root: str | Path,
    payload: Mapping[str, Any],
    input_paths: Sequence[str | Path],
    source_snapshots: Mapping[str, StableSnapshot],
    input_snapshots: Mapping[str, StableSnapshot],
    clock: Any = time.time,
) -> Path:
    """Publish exactly once and prove completion at least ten minutes before T0."""

    if not validate_preregistration(payload):
        raise GapV3PreregistrationRefusal("gap_v3_envelope_invalid")
    _assert_publication_snapshots_bound(
        payload,
        source_snapshots=source_snapshots,
        input_snapshots=input_snapshots,
    )
    _recheck_all(source_snapshots, input_snapshots)
    t0_epoch = _parse_t0_epoch(payload)
    try:
        now = float(clock())
    except (TypeError, ValueError, OverflowError) as exc:
        raise GapV3PreregistrationRefusal("publication_clock_invalid") from exc
    if not math.isfinite(now) or now >= t0_epoch - MINIMUM_PUBLICATION_LEAD_SECONDS:
        raise GapV3PreregistrationRefusal("publication_lead_time_insufficient")
    try:
        root = base._validate_output_root(output_root, input_paths=input_paths)
    except base.PreregistrationRefusal as exc:
        raise GapV3PreregistrationRefusal(str(exc)) from None
    digest = str(payload["preregistration_body_sha256"])
    target = root / f"mtvclc_gap_v3_preregistration_{digest}.json"
    if target.exists() or target.is_symlink() or base._is_reparse_point(target):
        raise GapV3PreregistrationRefusal("output_already_exists")
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temp = root / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    published = False
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        if float(clock()) >= t0_epoch - MINIMUM_PUBLICATION_LEAD_SECONDS:
            raise GapV3PreregistrationRefusal("publication_lead_time_insufficient")
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise GapV3PreregistrationRefusal("output_already_exists") from exc
        except OSError as exc:
            raise GapV3PreregistrationRefusal(
                "atomic_no_overwrite_publish_failed"
            ) from exc
        published = True
        actual = stable_snapshot(
            target,
            reason="output_verification_failed",
            maximum_bytes=len(encoded),
        )
        if actual.raw != encoded:
            raise GapV3PreregistrationRefusal("output_verification_failed")
        _recheck_all(source_snapshots, input_snapshots)
        if float(clock()) >= t0_epoch - MINIMUM_PUBLICATION_LEAD_SECONDS:
            raise GapV3PreregistrationRefusal("publication_lead_time_insufficient")
        temp.unlink()
        os.chmod(target, 0o400)
    except Exception:
        if published:
            try:
                os.chmod(target, 0o600)
                target.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        if temp.exists():
            try:
                os.chmod(temp, 0o600)
                temp.unlink(missing_ok=True)
            except OSError:
                pass
    return target


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seal a corrected authority-free MTVCLC gap-v3 declaration."
    )
    parser.add_argument("--cost-capture-json", required=True)
    parser.add_argument("--cost-capture-npz", required=True)
    parser.add_argument("--fee-attestation", required=True)
    parser.add_argument("--bridge-ea-deployed-source", required=True)
    parser.add_argument("--bridge-ea-deployed-ex4", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--start-delay-seconds",
        type=int,
        default=DEFAULT_SUCCESSOR_START_DELAY_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        sources, inputs = _input_snapshots(
            cost_capture_json=args.cost_capture_json,
            cost_capture_npz=args.cost_capture_npz,
            fee_attestation=args.fee_attestation,
            bridge_ea_deployed_source=args.bridge_ea_deployed_source,
            bridge_ea_deployed_ex4=args.bridge_ea_deployed_ex4,
        )
        payload = build_preregistration(
            cost_capture_json=args.cost_capture_json,
            cost_capture_npz=args.cost_capture_npz,
            fee_attestation=args.fee_attestation,
            bridge_ea_deployed_source=args.bridge_ea_deployed_source,
            bridge_ea_deployed_ex4=args.bridge_ea_deployed_ex4,
            sealed_at=datetime.now(UTC),
            start_delay_seconds=args.start_delay_seconds,
        )
        _recheck_all(sources, inputs)
        output = atomic_publish(
            output_root=args.output_root,
            payload=payload,
            input_paths=(
                args.cost_capture_json,
                args.cost_capture_npz,
                args.fee_attestation,
                args.bridge_ea_deployed_source,
                args.bridge_ea_deployed_ex4,
            ),
            source_snapshots=sources,
            input_snapshots=inputs,
        )
    except GapV3PreregistrationRefusal as exc:
        print(f"gap-v3 preregistration refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "preregistration_body_sha256": payload[
                    "preregistration_body_sha256"
                ],
                "research_only": True,
                "authority_granted": False,
                "immediate_market_buy_sell_only": True,
                "pending_trades_forbidden": True,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
