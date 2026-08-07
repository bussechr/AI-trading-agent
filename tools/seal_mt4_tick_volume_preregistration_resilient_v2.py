"""Build, but never implicitly launch, the audited MTVCLC gap-ledger attempt.

This independent sealer preserves the d80e and 07b failed captures, records
the crossed-T0 95306 first-cycle refusal as a zero-cell abandoned declaration,
and allocates a new 44-cell family after all 4,786 cells already attempted.
It binds the v3 collector contract with an immutable late-gap ledger and a
30-second start edge.  The tool has no capture, credential, network,
evaluation, activation, runtime, broker, or order path.
"""

from __future__ import annotations

# AGENT: ROLE: offline source-only sealer for the explicit-gap MTVCLC attempt.
# AGENT: HANDSHAKE: frozen inputs + failed lineage -> one authority-free declaration.
# AGENT: ISOLATION: local declaration inputs only; no capture, key, runtime, or
# broker access.

import argparse
import copy
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import os
from pathlib import Path
import stat
import sys
import time
from types import ModuleType
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOOL_PATH = Path(__file__).resolve()
BASE_SEALER_PATH = REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration.py"
SEALER_SUPPORT_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient.py"
)
COLLECTOR_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v2.py"
COLLECTOR_SUPPORT_PATH = REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient.py"
SCREEN_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation_replacement_v2.py"
)
SCREEN_SUPPORT_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation.py"
)
PINNED_SEALER_SUPPORT_SHA256 = (
    "94adcb20a3068b7c7a914072412f2b5bfc7050a1e8a6748ff385aa5931b08ab1"
)
PINNED_SEALER_SUPPORT_SIZE_BYTES = 35_609
PINNED_COLLECTOR_SUPPORT_SHA256 = (
    "b38e69933e4910956dc92477252d33f4d5a92458e70036520654bc53102357f5"
)
PINNED_COLLECTOR_SUPPORT_SIZE_BYTES = 77_496


def _read_stable_source(path: Path, reason: str) -> tuple[bytes, tuple[int, ...]]:
    candidate = path.absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
        if (
            candidate.is_symlink()
            or int(getattr(before_path, "st_file_attributes", 0)) & marker
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > 8 * 1024 * 1024
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
    except OSError as exc:  # pragma: no cover - import-time fail-closed boundary
        raise RuntimeError(reason) from exc
    try:
        before_handle = os.fstat(descriptor)
        if not stat.S_ISREG(before_handle.st_mode) or before_handle.st_size <= 0:
            raise RuntimeError(reason)
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            raw = handle.read(before_handle.st_size + 1)
        after_handle = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    try:
        after_path = candidate.lstat()
    except OSError as exc:  # pragma: no cover - concurrent replacement
        raise RuntimeError(reason) from exc
    identities = {
        (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
        )
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if (
        len(identities) != 1
        or len(raw) != before_handle.st_size
        or before_path.st_mode != before_handle.st_mode
    ):  # pragma: no cover - concurrent replacement
        raise RuntimeError(reason)
    return raw, identities.pop()


_sealer_support_bytes, SEALER_SUPPORT_STAT_IDENTITY = _read_stable_source(
    SEALER_SUPPORT_PATH,
    "sealer_support_source_changed_during_module_start",
)
SEALER_SUPPORT_SHA256 = hashlib.sha256(_sealer_support_bytes).hexdigest()
SEALER_SUPPORT_SIZE_BYTES = len(_sealer_support_bytes)
if (
    SEALER_SUPPORT_SHA256 != PINNED_SEALER_SUPPORT_SHA256
    or SEALER_SUPPORT_SIZE_BYTES != PINNED_SEALER_SUPPORT_SIZE_BYTES
):  # pragma: no cover - exact sealed support boundary
    raise RuntimeError("sealer_support_source_identity_mismatch")
_sealer_support_module_name = (
    "_fxstack_mtvclc_gap_sealer_support_" + SEALER_SUPPORT_SHA256[:16]
)
sealer_support = ModuleType(_sealer_support_module_name)
sealer_support.__file__ = str(SEALER_SUPPORT_PATH)
sealer_support.__package__ = ""
_missing_module = object()
_prior_support_module = sys.modules.get(
    _sealer_support_module_name,
    _missing_module,
)
sys.modules[_sealer_support_module_name] = sealer_support
try:
    exec(  # noqa: S102 - exact stable descriptor snapshot; never workspace pyc
        compile(
            _sealer_support_bytes,
            str(SEALER_SUPPORT_PATH),
            "exec",
            dont_inherit=True,
        ),
        sealer_support.__dict__,
    )
except Exception:
    raise
finally:
    if _prior_support_module is _missing_module:
        sys.modules.pop(_sealer_support_module_name, None)
    else:
        sys.modules[_sealer_support_module_name] = _prior_support_module
_sealer_support_after, _sealer_support_after_identity = _read_stable_source(
    SEALER_SUPPORT_PATH,
    "sealer_support_source_changed_during_module_start",
)
if (
    _sealer_support_after_identity != SEALER_SUPPORT_STAT_IDENTITY
    or _sealer_support_after != _sealer_support_bytes
):  # pragma: no cover - concurrent replacement
    raise RuntimeError("sealer_support_source_changed_during_module_start")
del _sealer_support_after
del _sealer_support_bytes

base = sealer_support.base

from fxstack.providers.ig_mt4_catalog import (  # noqa: E402
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
)
from fxstack.scalp import (  # noqa: E402,E501
    screen_mt4_tick_volume_close_location_continuation_replacement_v2 as replacement_screen,
)

# The private support copy supplies the already-audited deep static validator,
# while all family math is resolved against this new screen inside this process.
sealer_support.replacement_screen = replacement_screen


BASE_SEALER_SHA256 = "e4248f6f8484435a7e1bb4994363811edf32335ce04f31bfa25cbe844eba0ef1"
SCREEN_SUPPORT_SHA256 = (
    "7e3dd3e829a4429e1316be2925f17d6ec3c9c0e3257e529cefd153e660558059"
)
BASE_SEALER_SIZE_BYTES = 44_047
SCREEN_SUPPORT_SIZE_BYTES = 40_614

_collector_support_bytes, COLLECTOR_SUPPORT_STAT_IDENTITY = _read_stable_source(
    COLLECTOR_SUPPORT_PATH,
    "collector_support_source_changed_during_module_start",
)
COLLECTOR_SUPPORT_SHA256 = hashlib.sha256(_collector_support_bytes).hexdigest()
COLLECTOR_SUPPORT_SIZE_BYTES = len(_collector_support_bytes)
if (
    COLLECTOR_SUPPORT_SHA256 != PINNED_COLLECTOR_SUPPORT_SHA256
    or COLLECTOR_SUPPORT_SIZE_BYTES != PINNED_COLLECTOR_SUPPORT_SIZE_BYTES
):  # pragma: no cover - exact sealed support boundary
    raise RuntimeError("collector_support_source_identity_mismatch")
del _collector_support_bytes

PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_786
CURRENT_ATTEMPTED_CELLS = 44
CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_830
MAXIMUM_START_EDGE_LAG_SECONDS = 30.0
LATE_GAP_LEDGER_FILENAME = "late-unseen-bar-gaps.sha256.jsonl"

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

CROSSED_T0_REFUSED_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "95306e016d8af3128a26228857e6953691c0ac88e881f619e074db101c95d4a1"
    ),
    "artifact_file_sha256": (
        "21d2f17464067a7e8145c051c557f0c717a2ca123bec0a11d3de26528e0d6dee"
    ),
    "reason": "first_cycle_refused_bar_history_insufficient_direct_m1_after_t0",
    "eligible_observations_emitted": False,
    "manifest_entries_emitted": 0,
    "attempted_cells_increment": 0,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

FIRST_REPLACED_ATTEMPT = D80_FAILED_ATTEMPT
SECOND_REPLACED_ATTEMPT = SEVEN_B_FAILED_ATTEMPT
REPLACEMENT_LINEAGE: dict[str, Any] = {
    "replaces_preregistration_body_sha256": (
        CROSSED_T0_REFUSED_ATTEMPT["preregistration_body_sha256"]
    ),
    "replacement_reason": (
        "explicit_late_gap_ledger_and_start_edge_contract_before_evaluation"
    ),
    "old_window_restart_or_extension": False,
    "new_independent_window_required": True,
    "old_capture_used_for_signal_outcome_or_performance_selection": False,
    "old_attempt_counted_in_multiplicity_family": True,
}


class GapLedgerPreregistrationRefusal(RuntimeError):
    """Stable fail-closed refusal raised before publication."""


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
    )


def _assert_exact_source(
    path: Path,
    *,
    expected_sha256: str,
    expected_size: int,
    expected_stat: tuple[int, ...] | None,
    reason: str,
) -> None:
    try:
        before = path.stat()
        raw = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise GapLedgerPreregistrationRefusal(reason) from exc
    if (
        _stat_identity(before) != _stat_identity(after)
        or (expected_stat is not None and _stat_identity(after) != expected_stat)
        or len(raw) != expected_size
        or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), expected_sha256)
    ):
        raise GapLedgerPreregistrationRefusal(reason)


def _assert_support_sources_unchanged() -> None:
    _assert_exact_source(
        BASE_SEALER_PATH,
        expected_sha256=BASE_SEALER_SHA256,
        expected_size=BASE_SEALER_SIZE_BYTES,
        expected_stat=None,
        reason="base_sealer_source_identity_mismatch",
    )
    _assert_exact_source(
        SCREEN_SUPPORT_PATH,
        expected_sha256=SCREEN_SUPPORT_SHA256,
        expected_size=SCREEN_SUPPORT_SIZE_BYTES,
        expected_stat=None,
        reason="screen_support_source_identity_mismatch",
    )
    _assert_exact_source(
        COLLECTOR_SUPPORT_PATH,
        expected_sha256=COLLECTOR_SUPPORT_SHA256,
        expected_size=COLLECTOR_SUPPORT_SIZE_BYTES,
        expected_stat=COLLECTOR_SUPPORT_STAT_IDENTITY,
        reason="collector_support_source_identity_mismatch",
    )
    _assert_exact_source(
        SEALER_SUPPORT_PATH,
        expected_sha256=SEALER_SUPPORT_SHA256,
        expected_size=SEALER_SUPPORT_SIZE_BYTES,
        expected_stat=SEALER_SUPPORT_STAT_IDENTITY,
        reason="sealer_support_source_identity_mismatch",
    )


def _identity(path: Path, *, label: str) -> dict[str, Any]:
    try:
        return base._file_identity(path, label=label)
    except base.PreregistrationRefusal as exc:
        raise GapLedgerPreregistrationRefusal(str(exc)) from None


def _identity_valid(value: Any, *, filename: str) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "filename",
        "sha256",
        "size_bytes",
    }:
        return False
    size = value.get("size_bytes")
    return bool(
        value.get("filename") == filename
        and base._is_sha256(value.get("sha256"))
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )


def capture_integrity_contract(collector_source_sha256: str) -> dict[str, Any]:
    return {
        "schema_version": "fxstack.scalp.mtvclc_capture_integrity_contract.v3",
        "contract_id": (
            "first_authenticated_finalized_observation_or_declared_absence_wins.v3"
        ),
        "collector_source_sha256": collector_source_sha256,
        "collector_support_source_sha256": COLLECTOR_SUPPORT_SHA256,
        "first_authenticated_finalized_observation_is_immutable": True,
        "covered_matching_later_overlap_is_ignored": True,
        "covered_revised_later_overlap_is_ignored": True,
        "covered_overlap_never_overwrites_or_duplicates_a_bar": True,
        "late_unseen_epoch_at_or_before_watermark_is_permanent_gap": True,
        "late_unseen_epoch_is_never_backfilled": True,
        "late_unseen_epoch_is_never_baseline_eligible": True,
        "late_gap_event_is_fsynced_hash_chained_and_immutable": True,
        "late_gap_ledger_filename": LATE_GAP_LEDGER_FILENAME,
        "late_gap_ledger_schema_version": (
            "fxstack.external_ig_mt4_m1_late_gap_ledger.v1"
        ),
        "durable_per_symbol_watermark_never_regresses": True,
        "market_source_id_rollover_refuses": True,
        "downtime_gaps_are_preserved": True,
        "maximum_start_edge_lag_seconds": MAXIMUM_START_EDGE_LAG_SECONDS,
        "start_edge_miss_refuses_before_collection": True,
        "t0_reset_forbidden": True,
        "capture_restart_does_not_restart_or_extend_the_experiment": True,
        "tick_interval_seconds": 2.0,
        "bar_interval_seconds": 60.0,
        "bar_limit": 400,
        "http_timeout_seconds": 5.0,
        "persistence_mode": (
            "fsynced_hash_chained_active_hour_journal_then_immutable_hour_chunk"
        ),
        "bootstrap_first_cycle_finalized_immediately": True,
        "active_hour_journal_filename": "active-hour.journal.sha256.jsonl",
        "portable_chunk_schema_version": (
            "fxstack.external_ig_mt4_m1_activity_chunk.v2"
        ),
        "portable_manifest_schema_version": (
            "fxstack.external_ig_mt4_m1_activity_manifest_entry.v2"
        ),
        "exclusive_output_data_writer_lock_required": True,
    }


def _abandoned_attempts() -> list[dict[str, Any]]:
    return [
        *(dict(row) for row in base.ABANDONED_PREREGISTRATION_AUDIT),
        dict(D80_FAILED_ATTEMPT),
        dict(SEVEN_B_FAILED_ATTEMPT),
        dict(CROSSED_T0_REFUSED_ATTEMPT),
    ]


def build_preregistration(
    *,
    cost_capture_json: str | Path,
    cost_capture_npz: str | Path,
    fee_attestation: str | Path,
    sealed_at: datetime,
    start_delay_seconds: int = base.DEFAULT_START_DELAY_SECONDS,
) -> dict[str, Any]:
    """Build a fresh declaration without reading any prospective capture."""

    _assert_support_sources_unchanged()
    collector_identity = _identity(COLLECTOR_PATH, label="gap_collector_source")
    try:
        payload = base.build_preregistration(
            cost_capture_json=cost_capture_json,
            cost_capture_npz=cost_capture_npz,
            fee_attestation=fee_attestation,
            sealed_at=sealed_at,
            start_delay_seconds=start_delay_seconds,
        )
    except base.PreregistrationRefusal as exc:
        raise GapLedgerPreregistrationRefusal(str(exc)) from None

    payload.pop("preregistration_body_sha256", None)
    payload["attempt_accounting"] = {
        "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
        "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
        "cumulative_attempted_cells_lower_bound": (
            CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
        ),
    }
    payload["abandoned_preregistrations"] = _abandoned_attempts()
    payload["replacement_lineage"] = dict(REPLACEMENT_LINEAGE)
    payload["capture_integrity_contract"] = capture_integrity_contract(
        str(collector_identity["sha256"])
    )

    strategy = payload.get("strategy")
    gates = payload.get("fixed_success_gates")
    identities = payload.get("source_identities")
    if not all(isinstance(value, dict) for value in (strategy, gates, identities)):
        raise GapLedgerPreregistrationRefusal("replacement_envelope_invalid")
    assert isinstance(strategy, dict)
    assert isinstance(gates, dict)
    assert isinstance(identities, dict)
    manifest = replacement_screen.attempt_manifest()
    strategy["attempt_manifest"] = manifest
    strategy["attempt_manifest_sha256"] = base.canonical_sha256(manifest)
    gates["descriptive_df99_bonferroni_abs_t_threshold"] = (
        replacement_screen.BONFERRONI_STUDENT_T_MIN_DF99_ABS_THRESHOLD
    )
    gates["cell_win_probability_interval"] = (
        "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
    )

    identities["collector_source"] = collector_identity
    identities["collector_support_source"] = _identity(
        COLLECTOR_SUPPORT_PATH,
        label="collector_support_source",
    )
    identities["screen_source"] = _identity(
        SCREEN_PATH,
        label="gap_screen_source",
    )
    identities["screen_support_source"] = _identity(
        SCREEN_SUPPORT_PATH,
        label="screen_support_source",
    )
    identities["sealer_source"] = _identity(
        TOOL_PATH,
        label="gap_sealer_source",
    )
    identities["sealer_support_source"] = _identity(
        SEALER_SUPPORT_PATH,
        label="sealer_support_source",
    )
    identities["base_sealer_source"] = _identity(
        BASE_SEALER_PATH,
        label="base_sealer_source",
    )
    _assert_support_sources_unchanged()
    payload["preregistration_body_sha256"] = base.canonical_sha256(payload)
    if not validate_preregistration(payload):
        raise GapLedgerPreregistrationRefusal("replacement_envelope_invalid")
    return payload


def _documents_valid(value: Any) -> bool:
    if not isinstance(value, list) or len(value) != len(base.SOURCE_DOCUMENT_URLS):
        return False
    roles: set[str] = set()
    for document in value:
        if not isinstance(document, Mapping) or set(document) != {
            "filename",
            "sha256",
            "size_bytes",
            "role",
            "url",
            "retrieved_at_utc",
        }:
            return False
        role = str(document.get("role") or "")
        size = document.get("size_bytes")
        if (
            role in roles
            or role not in base.SOURCE_DOCUMENT_URLS
            or document.get("url") != base.SOURCE_DOCUMENT_URLS[role]
            or not str(document.get("filename") or "")
            or not base._is_sha256(document.get("sha256"))
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
        ):
            return False
        roles.add(role)
    return roles == set(base.SOURCE_DOCUMENT_URLS)


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Deep-validate an envelope without reading mutable source dependencies."""

    body = dict(payload)
    claimed = str(body.pop("preregistration_body_sha256", "")).lower()
    if not base._is_sha256(claimed) or not hmac.compare_digest(
        claimed,
        base.canonical_sha256(body),
    ):
        return False
    if (
        body.get("schema_version") != base.PREREGISTRATION_SCHEMA
        or body.get("research_only") is not True
        or body.get("authority") != base.FIXED_AUTHORITY_FLAGS
        or body.get("attempt_accounting")
        != {
            "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
            "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
            "cumulative_attempted_cells_lower_bound": (
                CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
            ),
        }
        or body.get("abandoned_preregistrations") != _abandoned_attempts()
        or body.get("replacement_lineage") != REPLACEMENT_LINEAGE
    ):
        return False

    translated = copy.deepcopy(body)
    translated_gates = translated.get("fixed_success_gates")
    if not isinstance(translated_gates, dict):
        return False
    if translated_gates.get("cell_win_probability_interval") != (
        "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
    ):
        return False
    # The frozen deep-validator support predates this new 44-cell allocation
    # and hard-codes only this display label.  Translate that label in the
    # private validation copy; the manifest, threshold, envelope, and the
    # actual published gate remain independently required at 4,830 above.
    translated_gates["cell_win_probability_interval"] = (
        "one_sided_wilson_family_adjusted_over_4786_attempted_cells"
    )
    if not sealer_support._validate_static_contract(translated):
        return False

    scope = body.get("scope")
    window = body.get("prospective_window")
    execution = body.get("execution_contract")
    identities = body.get("source_identities")
    if not all(
        isinstance(value, Mapping) for value in (scope, window, execution, identities)
    ):
        return False
    assert isinstance(window, Mapping)
    assert isinstance(identities, Mapping)

    expected_identity_names = {
        "collector_source",
        "collector_support_source",
        "screen_source",
        "screen_support_source",
        "sealer_source",
        "sealer_support_source",
        "base_sealer_source",
        "scope_catalog_source",
        "cost_capture",
        "fee_attestation",
        "production_runtime_context",
    }
    if set(identities) != expected_identity_names:
        return False
    collector = identities.get("collector_source")
    collector_support = identities.get("collector_support_source")
    screen = identities.get("screen_source")
    screen_support = identities.get("screen_support_source")
    sealer = identities.get("sealer_source")
    sealer_validator = identities.get("sealer_support_source")
    base_sealer = identities.get("base_sealer_source")
    catalog = identities.get("scope_catalog_source")
    collector_sha = (
        str(collector.get("sha256") or "") if isinstance(collector, Mapping) else ""
    )
    if (
        body.get("capture_integrity_contract")
        != capture_integrity_contract(collector_sha)
        or not _identity_valid(collector, filename=COLLECTOR_PATH.name)
        or not _identity_valid(
            collector_support,
            filename=COLLECTOR_SUPPORT_PATH.name,
        )
        or collector_support.get("sha256") != COLLECTOR_SUPPORT_SHA256
        or collector_support.get("size_bytes") != COLLECTOR_SUPPORT_SIZE_BYTES
        or not _identity_valid(screen, filename=SCREEN_PATH.name)
        or not _identity_valid(screen_support, filename=SCREEN_SUPPORT_PATH.name)
        or screen_support.get("sha256") != SCREEN_SUPPORT_SHA256
        or screen_support.get("size_bytes") != SCREEN_SUPPORT_SIZE_BYTES
        or not _identity_valid(sealer, filename=TOOL_PATH.name)
        or not _identity_valid(
            sealer_validator,
            filename=SEALER_SUPPORT_PATH.name,
        )
        or sealer_validator.get("sha256") != SEALER_SUPPORT_SHA256
        or sealer_validator.get("size_bytes") != SEALER_SUPPORT_SIZE_BYTES
        or not _identity_valid(base_sealer, filename=BASE_SEALER_PATH.name)
        or base_sealer.get("sha256") != BASE_SEALER_SHA256
        or base_sealer.get("size_bytes") != BASE_SEALER_SIZE_BYTES
        or not _identity_valid(catalog, filename="ig_mt4_catalog.py")
    ):
        return False

    cost = identities.get("cost_capture")
    if (
        not isinstance(cost, Mapping)
        or set(cost)
        != {
            "capture_json",
            "capture_mode",
            "capture_npz",
            "capture_payload_sha256",
            "scope_version",
            "venue_id",
        }
        or cost.get("capture_mode") != "authenticated_same_source_db_history"
        or cost.get("scope_version") != IG_MT4_SCALP_SCOPE_VERSION
        or cost.get("venue_id") != IG_MT4_VENUE_ID
        or not base._is_sha256(cost.get("capture_payload_sha256"))
        or not _identity_valid(
            cost.get("capture_json"),
            filename="ig_mt4_bid_ask_capture.json",
        )
        or not _identity_valid(
            cost.get("capture_npz"),
            filename="ig_mt4_bid_ask_samples.npz",
        )
    ):
        return False

    fee = identities.get("fee_attestation")
    if (
        not isinstance(fee, Mapping)
        or set(fee)
        != {
            "account_currency",
            "attestation",
            "attested_at_utc",
            "effective_at_utc",
            "operator_attestation_sha256",
            "source_documents",
        }
        or fee.get("account_currency") != "USD"
        or not _identity_valid(
            fee.get("attestation"),
            filename="mtvclc_fee_attestation.json",
        )
        or not base._is_sha256(fee.get("operator_attestation_sha256"))
        or not _documents_valid(fee.get("source_documents"))
    ):
        return False
    production = identities.get("production_runtime_context")
    if (
        not isinstance(production, Mapping)
        or production.get("relationship")
        != "context_only_successor_not_integrated_or_authorized"
    ):
        return False
    try:
        sealed = base._parse_utc_second(body.get("sealed_at_utc"), label="sealed")
        t0 = base._parse_utc_second(window.get("t0_utc_inclusive"), label="t0")
        end = base._parse_utc_second(window.get("end_utc_exclusive"), label="end")
    except base.PreregistrationRefusal:
        return False
    return bool(
        t0 > sealed and end - t0 == timedelta(days=base.PROSPECTIVE_WINDOW_DAYS)
    )


def atomic_publish(
    *,
    output_root: str | Path,
    payload: Mapping[str, Any],
    input_paths: Sequence[str | Path],
) -> Path:
    """Publish once by exclusive hard-link; this function is never implicit."""

    if not validate_preregistration(payload):
        raise GapLedgerPreregistrationRefusal("replacement_envelope_invalid")
    try:
        root = base._validate_output_root(output_root, input_paths=input_paths)
    except base.PreregistrationRefusal as exc:
        raise GapLedgerPreregistrationRefusal(str(exc)) from None
    digest = str(payload["preregistration_body_sha256"])
    target = root / f"mtvclc_gap_v3_preregistration_{digest}.json"
    if target.exists() or target.is_symlink() or base._is_reparse_point(target):
        raise GapLedgerPreregistrationRefusal("output_already_exists")
    encoded = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    temp = root / f".{target.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    published = False
    try:
        descriptor = os.open(temp, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, target)
        except FileExistsError as exc:
            raise GapLedgerPreregistrationRefusal("output_already_exists") from exc
        except OSError as exc:
            raise GapLedgerPreregistrationRefusal(
                "atomic_no_overwrite_publish_failed"
            ) from exc
        published = True
        if target.read_bytes() != encoded:
            raise GapLedgerPreregistrationRefusal("output_verification_failed")
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
        description="Build a new authority-free MTVCLC explicit-gap declaration."
    )
    parser.add_argument("--cost-capture-json", required=True)
    parser.add_argument("--cost-capture-npz", required=True)
    parser.add_argument("--fee-attestation", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument(
        "--start-delay-seconds",
        type=int,
        default=base.DEFAULT_START_DELAY_SECONDS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        payload = build_preregistration(
            cost_capture_json=args.cost_capture_json,
            cost_capture_npz=args.cost_capture_npz,
            fee_attestation=args.fee_attestation,
            sealed_at=datetime.now(UTC),
            start_delay_seconds=args.start_delay_seconds,
        )
        output = atomic_publish(
            output_root=args.output_root,
            payload=payload,
            input_paths=(
                args.cost_capture_json,
                args.cost_capture_npz,
                args.fee_attestation,
            ),
        )
    except GapLedgerPreregistrationRefusal as exc:
        print(f"gap-ledger preregistration refused: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "output": str(output),
                "preregistration_body_sha256": payload["preregistration_body_sha256"],
                "research_only": True,
                "authority_granted": False,
                "order_authorized": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
