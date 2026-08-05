"""Seal the independent MTVCLC v5 successor without starting it.

The failed v4 attempt is counted exactly once.  This declaration therefore
freezes 4,830 prior cells plus 44 new cells, or 4,874 cumulatively, under a
new future T0 and independent 180-day half-open window.  It retains the
gap-v3 reservation/finalization wire semantics through the distinct v4
collector adapter, including its fail-closed first-cycle readiness loop.

No collection, outcome access, evaluation, signing, runtime control, broker
access, or immediate trade execution is available here.
"""

from __future__ import annotations

# AGENT: ROLE: offline MTVCLC v5 prospective sealer.
# AGENT: HANDSHAKE: v4 failure + exact v5 dependencies -> independent declaration.
# AGENT: ISOLATION: local descriptor reads and one explicit exclusive JSON publish only.

import argparse
from datetime import UTC, datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any, Mapping, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TOOL_PATH = Path(__file__).resolve()
V3_SEALER_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v3.py"
)
V5_HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v5.py"
)
V3_HANDOFF_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"
)
V5_EVALUATOR_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v5.py"
)
V3_EVALUATOR_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"
)
V5_RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release_v5.py"
V3_RELEASE_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"
)
V4_COLLECTOR_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"
)
V3_COLLECTOR_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"
)
V4_SCREEN_PATH = (
    FXSTACK_SRC
    / "fxstack"
    / "scalp"
    / "screen_mt4_tick_volume_close_location_continuation_replacement_v4.py"
)
V3_SCREEN_TEMPLATE_PATH = V4_SCREEN_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement_v3.py"
)
V3_PUBLIC_VERIFIER_PATH = (
    FXSTACK_SRC / "fxstack" / "runtime" / "mtvclc_validation_evidence_v3.py"
)
V2_PUBLIC_VERIFIER_TEMPLATE_PATH = V3_PUBLIC_VERIFIER_PATH.with_name(
    "mtvclc_validation_evidence_v2.py"
)

V5_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_preregistration_tool.v5"
RUNTIME_POLICY_BINDING_SCHEMA = "fxstack.scalp.mtvclc_runtime_policy_binding.v1"
COLLECTOR_WIRE_PROFILE = "gap_v3_source_pinned"
SUPERVISION_GUARD_IDENTITY_FILENAME = (
    "collector-guard.identity.gap-v5.v1.json"
)
SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION = (
    "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"
)
PRESERVATION_FILENAME_SCHEMA_VERSION = (
    "fxstack.scalp.mtvclc_preservation_filenames.v5"
)
DERIVATION_MODE = "exact_v3_template_plus_counted_literal_v5_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024
EXPECTED_V3_TEMPLATE_SHA256 = (
    "a1386ef1ee033750ed12299db34d1b7bc6b3d2bb8d4d6d996b218a342016ec02"
)
EXPECTED_V3_TEMPLATE_SIZE_BYTES = 63_997

PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_830
CURRENT_ATTEMPTED_CELLS = 44
CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_874

FAILED_V4_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
    ),
    "artifact_file_sha256": (
        "7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55"
    ),
    "attempt_failure_sha256": (
        "56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78"
    ),
    "reason": "unresolved_first_cycle_reservation_after_process_interruption",
    "eligible_observations_emitted": False,
    "manifest_entries_emitted": 0,
    "tail_commitment_file_size_bytes": 15_303,
    "tail_commitment_file_sha256": (
        "d770d66bd8cb138b6eb142063aa106788cb2e8be9d4479660ed82c26569b9eb8"
    ),
    "tail_commitment_last_write_utc": "2026-08-04T00:59:20.687771Z",
    "immutable_guard_identity_sha256": (
        "f90fea1134d431feb3ab8a0021bf54159bc249a477490284534d9c444b8c5cf3"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}


class V5BootstrapRefusal(RuntimeError):
    """Raised before the count-corrected inherited sealer is safely available."""


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _is_reparse(path: Path, value: os.stat_result) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return path.is_symlink() or bool(
        int(getattr(value, "st_file_attributes", 0)) & marker
    )


def _read_exact_source(path: Path, *, reason: str) -> tuple[bytes, tuple[int, ...]]:
    candidate = path.expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            _is_reparse(candidate, before_path)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > _MAXIMUM_SOURCE_BYTES
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
        try:
            before_handle = os.fstat(descriptor)
            raw = bytearray()
            remaining = int(before_handle.st_size)
            while remaining:
                block = os.read(descriptor, min(1 << 20, remaining))
                if not block:
                    raise OSError(reason)
                raw.extend(block)
                remaining -= len(block)
            if os.read(descriptor, 1):
                raise OSError(reason)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = candidate.lstat()
    except OSError as exc:
        raise V5BootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V5BootstrapRefusal(reason)
    return bytes(raw), identities.pop()


def _self_source() -> tuple[bytes, tuple[int, ...]]:
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
        return bound_raw, bound_identity
    return _read_exact_source(TOOL_PATH, reason="v5_sealer_source_invalid")


def _replace_exact(
    source: str,
    old: str,
    new: str,
    *,
    label: str,
    count: int = 1,
) -> str:
    if source.count(old) != count:
        raise V5BootstrapRefusal(f"v3_sealer_template_transform_invalid:{label}")
    return source.replace(old, new, count)


def _assert_pinned_v3_template(raw: bytes) -> None:
    if (
        len(raw) != EXPECTED_V3_TEMPLATE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != EXPECTED_V3_TEMPLATE_SHA256
    ):
        raise V5BootstrapRefusal("v3_sealer_template_identity_invalid")


_FAILED_V4_LITERAL = '''FAILED_V4_ATTEMPT: dict[str, Any] = {
    "preregistration_body_sha256": (
        "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
    ),
    "artifact_file_sha256": (
        "7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55"
    ),
    "attempt_failure_sha256": (
        "56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78"
    ),
    "reason": "unresolved_first_cycle_reservation_after_process_interruption",
    "eligible_observations_emitted": False,
    "manifest_entries_emitted": 0,
    "tail_commitment_file_size_bytes": 15_303,
    "tail_commitment_file_sha256": (
        "d770d66bd8cb138b6eb142063aa106788cb2e8be9d4479660ed82c26569b9eb8"
    ),
    "tail_commitment_last_write_utc": "2026-08-04T00:59:20.687771Z",
    "immutable_guard_identity_sha256": (
        "f90fea1134d431feb3ab8a0021bf54159bc249a477490284534d9c444b8c5cf3"
    ),
    "attempted_cells_increment": 44,
    "signal_evaluation_performed": False,
    "outcome_evaluation_performed": False,
    "performance_statistics_computed": False,
    "success_claim_evaluated": False,
}

'''


def _derive_v5_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V5BootstrapRefusal("v3_sealer_template_encoding_invalid") from exc
    transforms = (
        (
            '    FXSTACK_SRC / "fxstack" / "strategy" / "scalp_dislocation.py"\n',
            '    FXSTACK_SRC / "fxstack" / "strategy" / "mtvclc.py"\n',
            "production_policy_path",
            1,
        ),
        (
            'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"',
            'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v5.py"',
            "handoff_path",
            1,
        ),
        (
            'REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"',
            'REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v5.py"',
            "evaluator_path",
            1,
        ),
        (
            'REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"',
            'REPO_ROOT / "tools" / "mtvclc_validation_release_v5.py"',
            "release_path",
            1,
        ),
        (
            "screen_mt4_tick_volume_close_location_continuation_replacement_v3.py",
            "screen_mt4_tick_volume_close_location_continuation_replacement_v4.py",
            "screen_path",
            1,
        ),
        (
            "capture_ig_mt4_m1_activity_resilient_v3.py",
            "capture_ig_mt4_m1_activity_resilient_v4.py",
            "collector_path",
            1,
        ),
        (
            "mtvclc_validation_evidence_v2.py",
            "mtvclc_validation_evidence_v3.py",
            "public_verifier_path",
            1,
        ),
        (
            "PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_786",
            "PRIOR_ATTEMPTED_CELLS_LOWER_BOUND = 4_830",
            "prior_cells",
            1,
        ),
        (
            "CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_830",
            "CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND = 4_874",
            "cumulative_cells",
            1,
        ),
        (
            'CAPTURE_PROFILE_ID = "gap_v3_source_pinned"',
            'CAPTURE_PROFILE_ID = "gap_v3_source_pinned"\n'
            'SUPERVISION_GUARD_IDENTITY_FILENAME = (\n'
            '    "collector-guard.identity.gap-v5.v1.json"\n'
            ')\n'
            'SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION = (\n'
            '    "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"\n'
            ')',
            "guard_identity_constants",
            1,
        ),
        (
            "REPLACEMENT_LINEAGE: dict[str, Any] = {",
            _FAILED_V4_LITERAL + "REPLACEMENT_LINEAGE: dict[str, Any] = {",
            "failed_v4_record",
            1,
        ),
        (
            '    "replaces_preregistration_body_sha256": FAILED_CROSSED_T0_ATTEMPT[',
            '    "replaces_preregistration_body_sha256": FAILED_V4_ATTEMPT[',
            "replacement_target",
            1,
        ),
        (
            '        "new_source_pinned_upstream_producer_software_and_distinct_gap_v3_window"',
            '        "new_independent_window_after_failed_v4_first_cycle_reservation"',
            "replacement_reason",
            1,
        ),
        (
            "    expected[-1] = _corrected_crossed_attempt()\n    return expected",
            "    expected[-1] = _corrected_crossed_attempt()\n"
            "    expected.append(dict(FAILED_V4_ATTEMPT))\n"
            "    return expected",
            "expected_lineage_append",
            1,
        ),
        (
            '    abandoned[-1] = _corrected_crossed_attempt()\n'
            '    payload["abandoned_preregistrations"] = abandoned',
            '    abandoned[-1] = _corrected_crossed_attempt()\n'
            "    abandoned.append(dict(FAILED_V4_ATTEMPT))\n"
            '    payload["abandoned_preregistrations"] = abandoned',
            "built_lineage_append",
            1,
        ),
        (
            "gap_v3_successor_envelope_and_4830_cell_family_required",
            "gap_v3_successor_envelope_and_4874_cell_family_required",
            "capture_contract_family",
            1,
        ),
        (
            '        "schema_version": '
            '"fxstack.scalp.mtvclc_preservation_filenames.v3",\n'
            '        "capture_profile_id": CAPTURE_PROFILE_ID,',
            '        "schema_version": '
            '"fxstack.scalp.mtvclc_preservation_filenames.v5",\n'
            '        "capture_profile_id": CAPTURE_PROFILE_ID,\n'
            '        "guard_identity_filename": (\n'
            '            SUPERVISION_GUARD_IDENTITY_FILENAME\n'
            '        ),\n'
            '        "guard_identity_schema_version": (\n'
            '            SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION\n'
            '        ),',
            "guard_identity_preservation_contract",
            1,
        ),
        (
            '        and value.get("maximum_start_edge_lag_seconds") == 30.0',
            '        and value.get("supervision_guard_identity_filename")\n'
            '        == SUPERVISION_GUARD_IDENTITY_FILENAME\n'
            '        and value.get("supervision_guard_identity_schema_version")\n'
            '        == SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION\n'
            '        and value.get("v5_supervision_guard_identity_required") is True\n'
            '        and value.get("maximum_start_edge_lag_seconds") == 30.0',
            "guard_identity_capture_contract_validation",
            1,
        ),
        (
            "one_sided_wilson_family_adjusted_over_4830_attempted_cells",
            "one_sided_wilson_family_adjusted_over_4874_attempted_cells",
            "wilson_family",
            2,
        ),
        (
            "61b6a48b9eee7a3dc5fd615da23779b0fa876fb34049c912b791cf761e287501",
            "e5967da47a0087d14e0ca8a1d3747b81432f2765f56b4d8d50bbf85358beb631",
            "collector_source_digest",
            1,
        ),
        (
            "298_993",
            "28_794",
            "collector_source_size",
            1,
        ),
        (
            "6cb5995d019cafe622caf09f171e0a0caec58185f7c121365c4c778b643f15ee",
            "82e2b2740b884f64040cc5545c752aa3de9c7ebf0f543067711b978f79f9b648",
            "capture_contract_digest",
            1,
        ),
    )
    for old, new, label, count in transforms:
        source = _replace_exact(source, old, new, label=label, count=count)
    adapter_marker = "_base_sealer = _execute_exact_source(\n"
    adapter = '''class _MTVCLCPolicyCompatibility:
    def config_sha256(self) -> str:
        return str(_production_strategy.MTVCLC_CONFIG_SHA256)


_production_strategy.SCALP_DISLOCATION_STRATEGY_ID = (
    _production_strategy.MTVCLC_STRATEGY_ID
)
_production_strategy.SCALP_DISLOCATION_STRATEGY_VERSION = (
    _production_strategy.MTVCLC_STRATEGY_VERSION
)
_production_strategy.DislocationPolicy = _MTVCLCPolicyCompatibility


'''
    source = _replace_exact(
        source,
        adapter_marker,
        adapter + adapter_marker,
        label="base_sealer_policy_adapter",
    )
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_SEALER_TEMPLATE_PATH,
    reason="v3_sealer_template_source_invalid",
)
_assert_pinned_v3_template(_V3_TEMPLATE_RAW)
_DERIVED_SOURCE = _derive_v5_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_successor_sealer_v5_impl"
_implementation = ModuleType(_IMPLEMENTATION_NAME)
_implementation.__file__ = str(TOOL_PATH)
_implementation.__package__ = ""
_implementation.__dict__["__fxstack_exact_source_path__"] = TOOL_PATH
_implementation.__dict__["__fxstack_exact_source_raw__"] = _SELF_RAW
_implementation.__dict__["__fxstack_exact_source_stat_identity__"] = (
    _SELF_STAT_IDENTITY
)
_previous = sys.modules.get(_IMPLEMENTATION_NAME)
sys.modules[_IMPLEMENTATION_NAME] = _implementation
try:
    exec(  # noqa: S102 - exact template bytes plus counted deterministic transforms
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V5BootstrapRefusal("v5_sealer_derived_source_import_invalid") from exc
finally:
    if _previous is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous


_template_image = _implementation.ExecutedSourceSnapshot(
    V3_SEALER_TEMPLATE_PATH,
    _V3_TEMPLATE_RAW,
    _V3_TEMPLATE_STAT_IDENTITY,
)
_implementation._EXECUTED_SOURCE_IMAGES_BY_LABEL[
    "sealer_v3_template_source"
] = _template_image
_implementation.REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS = frozenset(
    set(_implementation.REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS)
    | {
        "sealer_v3_template_source",
        "handoff_v3_template_source",
        "evaluator_v3_template_source",
        "release_v3_template_source",
        "collector_v3_template_source",
        "screen_v3_template_source",
        "public_verifier_v2_template_source",
    }
)

_template_source_paths = _implementation._source_paths


def _v5_source_paths() -> dict[str, Path]:
    paths = dict(_template_source_paths())
    paths.update(
        {
            "sealer_v3_template_source": V3_SEALER_TEMPLATE_PATH,
            "handoff_v3_template_source": V3_HANDOFF_TEMPLATE_PATH,
            "evaluator_v3_template_source": V3_EVALUATOR_TEMPLATE_PATH,
            "release_v3_template_source": V3_RELEASE_TEMPLATE_PATH,
            "collector_v3_template_source": V3_COLLECTOR_TEMPLATE_PATH,
            "screen_v3_template_source": V3_SCREEN_TEMPLATE_PATH,
            "public_verifier_v2_template_source": (
                V2_PUBLIC_VERIFIER_TEMPLATE_PATH
            ),
        }
    )
    return paths


_implementation._source_paths = _v5_source_paths
_template_execution_provenance_contract = _implementation.execution_provenance_contract


def execution_provenance_contract() -> dict[str, Any]:
    contract = dict(_template_execution_provenance_contract())
    contract.update(
        {
            "execution_mode": DERIVATION_MODE,
            "v3_template_source_sha256": hashlib.sha256(_V3_TEMPLATE_RAW).hexdigest(),
            "v3_template_source_size_bytes": len(_V3_TEMPLATE_RAW),
            "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
            "literal_transform_count": 24,
            "legacy_production_strategy_module_loaded": False,
            "active_runtime_policy_exact_source_executed": True,
            "failed_v4_attempt_counted_once": True,
            "independent_future_t0_required": True,
            "first_cycle_readiness_retry_bound": True,
        }
    )
    return contract


_implementation.execution_provenance_contract = execution_provenance_contract

GapV5PreregistrationRefusal = _implementation.GapV3PreregistrationRefusal
base = _implementation.base
screen = _implementation.screen
_catalog = _implementation._catalog
SCREEN_PATH = _implementation.SCREEN_PATH
IG_MT4_SCALP_SCOPE_VERSION = _implementation.IG_MT4_SCALP_SCOPE_VERSION
IG_MT4_SCALP_SYMBOLS = _implementation.IG_MT4_SCALP_SYMBOLS
IG_MT4_VENUE_ID = _implementation.IG_MT4_VENUE_ID
BRIDGE_EA_REPOSITORY_SOURCE_PATH = _implementation.BRIDGE_EA_REPOSITORY_SOURCE_PATH
DEFAULT_SUCCESSOR_START_DELAY_SECONDS = (
    _implementation.DEFAULT_SUCCESSOR_START_DELAY_SECONDS
)
MINIMUM_PUBLICATION_LEAD_SECONDS = _implementation.MINIMUM_PUBLICATION_LEAD_SECONDS
REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS = (
    _implementation.REQUIRED_EXECUTABLE_SOURCE_IDENTITY_LABELS
)
executed_source_identities = _implementation.executed_source_identities
stable_snapshot = _implementation.stable_snapshot
_read_executed_source = _implementation._read_executed_source
_execute_exact_source = _implementation._execute_exact_source
_input_snapshots = _implementation._input_snapshots
_recheck_all = _implementation._recheck_all
_expected_abandoned_attempts = _implementation._expected_abandoned_attempts
_source_paths = _v5_source_paths
_template_validate_preregistration = _implementation.validate_preregistration
_template_build_preregistration = _implementation.build_preregistration
_template_atomic_publish = _implementation.atomic_publish


def _runtime_policy_binding(
    *, source_identities: Mapping[str, Any], engine_identity: Mapping[str, Any]
) -> dict[str, Any]:
    policy = _implementation._production_strategy
    return {
        "schema_version": RUNTIME_POLICY_BINDING_SCHEMA,
        "source_identity_label": "production_strategy_policy_source",
        "source_identity": dict(source_identities["production_strategy_policy_source"]),
        "strategy_id": policy.MTVCLC_STRATEGY_ID,
        "strategy_version": policy.MTVCLC_STRATEGY_VERSION,
        "config_id": policy.MTVCLC_CONFIG_ID,
        "config_sha256": policy.MTVCLC_CONFIG_SHA256,
        "evaluator_entrypoint": "evaluate_mtvclc",
        "ordered_symbols": list(policy.MTVCLC_V1_SYMBOLS),
        "ordered_cell_count": len(policy.MTVCLC_V1_SYMBOLS) * 2,
        "engine_identity_sha256": base.canonical_sha256(engine_identity),
        "entry_type": "immediate_market",
        "immediate_market_trade": True,
        "buy_price_basis": "authenticated_ask",
        "sell_price_basis": "authenticated_bid",
        "pending_orders_forbidden": True,
        "pending_trades_forbidden": True,
        "prospective_outcome_evaluation_not_before_sealed_end": True,
        "runtime_or_broker_authority_granted": False,
    }


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
    """Build the v5 declaration in memory; never publish or collect implicitly."""

    payload = _template_build_preregistration(
        cost_capture_json=cost_capture_json,
        cost_capture_npz=cost_capture_npz,
        fee_attestation=fee_attestation,
        bridge_ea_deployed_source=bridge_ea_deployed_source,
        bridge_ea_deployed_ex4=bridge_ea_deployed_ex4,
        sealed_at=sealed_at,
        start_delay_seconds=start_delay_seconds,
    )
    payload.pop("preregistration_body_sha256", None)
    identities = payload.get("source_identities")
    if not isinstance(identities, dict):
        raise GapV5PreregistrationRefusal("source_identities_invalid")
    runtime = identities.get("production_runtime_context")
    if not isinstance(runtime, dict) or not isinstance(
        runtime.get("engine_identity"), Mapping
    ):
        raise GapV5PreregistrationRefusal("production_engine_identity_invalid")
    policy = _implementation._production_strategy
    runtime.update(
        {
            "relationship": "active_runtime_policy_identity_context_only_no_authority",
            "active_strategy_family_context": policy.MTVCLC_STRATEGY_ID,
            "active_strategy_version_context": policy.MTVCLC_STRATEGY_VERSION,
            "active_policy_config_sha256_context": policy.MTVCLC_CONFIG_SHA256,
        }
    )
    payload["preregistration_tool_revision"] = V5_TOOL_REVISION
    payload["collector_wire_profile"] = COLLECTOR_WIRE_PROFILE
    payload["runtime_policy_binding"] = _runtime_policy_binding(
        source_identities=identities,
        engine_identity=runtime["engine_identity"],
    )
    payload["preregistration_body_sha256"] = base.canonical_sha256(payload)
    if not validate_preregistration(payload):
        raise GapV5PreregistrationRefusal("runtime_bound_v5_envelope_invalid")
    return payload


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Deep, filesystem-free validation of the v5 successor envelope."""

    if not _template_validate_preregistration(payload):
        return False
    identities = payload.get("source_identities")
    runtime_binding = payload.get("runtime_policy_binding")
    if not isinstance(identities, Mapping) or not isinstance(
        runtime_binding, Mapping
    ):
        return False
    runtime = identities.get("production_runtime_context")
    source_identity = identities.get("production_strategy_policy_source")
    policy = _implementation._production_strategy
    if not isinstance(runtime, Mapping) or not isinstance(
        runtime.get("engine_identity"), Mapping
    ):
        return False
    expected_binding = _runtime_policy_binding(
        source_identities=identities,
        engine_identity=runtime["engine_identity"],
    )
    scope = payload.get("scope")
    window = payload.get("prospective_window")
    authority = payload.get("authority")
    accounting = payload.get("attempt_accounting")
    abandoned = payload.get("abandoned_preregistrations")
    capture_contract = payload.get("capture_integrity_contract")
    preservation_contract = payload.get("preservation_filename_contract")
    return bool(
        payload.get("preregistration_tool_revision") == V5_TOOL_REVISION
        and payload.get("collector_wire_profile") == COLLECTOR_WIRE_PROFILE
        and accounting
        == {
            "prior_attempted_cells_lower_bound": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
            "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
            "cumulative_attempted_cells_lower_bound": (
                CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND
            ),
        }
        and isinstance(abandoned, list)
        and len(abandoned) == 6
        and abandoned[-1] == FAILED_V4_ATTEMPT
        and runtime_binding == expected_binding
        and isinstance(source_identity, Mapping)
        and source_identity.get("filename") == "mtvclc.py"
        and source_identity == _implementation._PRODUCTION_STRATEGY_SOURCE_IMAGE.identity()
        and runtime.get("relationship")
        == "active_runtime_policy_identity_context_only_no_authority"
        and runtime.get("active_strategy_family_context") == policy.MTVCLC_STRATEGY_ID
        and runtime.get("active_strategy_version_context")
        == policy.MTVCLC_STRATEGY_VERSION
        and runtime.get("active_policy_config_sha256_context")
        == policy.MTVCLC_CONFIG_SHA256
        and isinstance(scope, Mapping)
        and scope.get("ordered_symbols") == list(policy.MTVCLC_V1_SYMBOLS)
        and len(scope.get("cell_order", [])) == 44
        and isinstance(window, Mapping)
        and window.get("interim_signal_or_outcome_evaluation_forbidden") is True
        and window.get("early_success_forbidden") is True
        and window.get("no_optional_extension_or_restart_after_failure") is True
        and isinstance(capture_contract, Mapping)
        and capture_contract.get("first_cycle_pre_t0_snapshot_retried_under_same_reservation")
        is True
        and capture_contract.get("first_cycle_readiness_re_reservation_forbidden")
        is True
        and capture_contract.get("first_cycle_readiness_retry_seconds") == 1.0
        and capture_contract.get("maximum_start_edge_lag_seconds") == 30.0
        and capture_contract.get("first_cycle_readiness_proof_committed_in_every_cycle_part")
        is True
        and capture_contract.get("supervision_guard_identity_filename")
        == SUPERVISION_GUARD_IDENTITY_FILENAME
        and capture_contract.get("supervision_guard_identity_schema_version")
        == SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION
        and capture_contract.get("v5_supervision_guard_identity_required") is True
        and isinstance(preservation_contract, Mapping)
        and preservation_contract.get("schema_version")
        == PRESERVATION_FILENAME_SCHEMA_VERSION
        and preservation_contract.get("guard_identity_filename")
        == SUPERVISION_GUARD_IDENTITY_FILENAME
        and preservation_contract.get("guard_identity_schema_version")
        == SUPERVISION_GUARD_IDENTITY_SCHEMA_VERSION
        and isinstance(authority, Mapping)
        and authority
        and not any(authority.values())
        and "collector_v3_template_source" in identities
        and "screen_v3_template_source" in identities
        and "public_verifier_v2_template_source" in identities
        and all(
            not (
                isinstance(value, Mapping)
                and value.get("filename") == "scalp_dislocation.py"
            )
            for value in identities.values()
        )
    )


def atomic_publish(
    *,
    output_root: str | Path,
    payload: Mapping[str, Any],
    input_paths: Sequence[str | Path],
    source_snapshots: Mapping[str, Any],
    input_snapshots: Mapping[str, Any],
    clock: Any = __import__("time").time,
) -> Path:
    """Exclusively publish only after every v5 and gap-v3-wire check passes."""

    if not validate_preregistration(payload):
        raise GapV5PreregistrationRefusal("runtime_bound_v5_envelope_invalid")
    return _template_atomic_publish(
        output_root=output_root,
        payload=payload,
        input_paths=input_paths,
        source_snapshots=source_snapshots,
        input_snapshots=input_snapshots,
        clock=clock,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Seal an authority-free independent MTVCLC v5 prospective "
            "declaration without starting collection."
        )
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
    except (GapV5PreregistrationRefusal, V5BootstrapRefusal, OSError) as exc:
        print(f"runtime-bound v5 preregistration refused: {exc}", file=sys.stderr)
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
                "collection_started": False,
                "immediate_market_buy_sell_only": True,
                "pending_trades_forbidden": True,
                "prior_attempted_cells": PRIOR_ATTEMPTED_CELLS_LOWER_BOUND,
                "current_attempted_cells": CURRENT_ATTEMPTED_CELLS,
                "cumulative_attempted_cells": CUMULATIVE_ATTEMPTED_CELLS_LOWER_BOUND,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
