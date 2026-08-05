"""Seal an MTVCLC-runtime-bound prospective declaration without starting it.

This versioned boundary retains the already pinned gap-v3 collector wire
contract while replacing the stale production-policy context in the v3
sealer template with the active, pure :mod:`fxstack.strategy.mtvclc`
evaluator.  The template and every deterministic transform are themselves
content addressed.  No collection, outcome access, evaluation, signing,
runtime control, broker access, or trade execution is available here.
"""

from __future__ import annotations

# AGENT: ROLE: offline MTVCLC runtime-policy-bound prospective sealer v4.
# AGENT: HANDSHAKE: exact v3 collector contract + exact MTVCLC evaluator -> sealed declaration.
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
V4_HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"
)
V3_HANDOFF_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"
)
V4_EVALUATOR_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v4.py"
)
V3_EVALUATOR_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"
)
V4_RELEASE_PATH = REPO_ROOT / "tools" / "mtvclc_validation_release_v4.py"
V3_RELEASE_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"
)

V4_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_preregistration_tool.v4"
RUNTIME_POLICY_BINDING_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_policy_binding.v1"
)
COLLECTOR_WIRE_PROFILE = "gap_v3_source_pinned"
DERIVATION_MODE = "exact_v3_template_plus_counted_literal_v4_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024


class V4BootstrapRefusal(RuntimeError):
    """Raised before the inherited sealer contract is safely available."""


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
        raise V4BootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4BootstrapRefusal(reason)
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
    return _read_exact_source(TOOL_PATH, reason="v4_sealer_source_invalid")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V4BootstrapRefusal(f"v3_sealer_template_transform_invalid:{label}")
    return source.replace(old, new, 1)


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4BootstrapRefusal("v3_sealer_template_encoding_invalid") from exc
    source = _replace_once(
        source,
        '    FXSTACK_SRC / "fxstack" / "strategy" / "scalp_dislocation.py"\n',
        '    FXSTACK_SRC / "fxstack" / "strategy" / "mtvclc.py"\n',
        label="production_policy_path",
    )
    source = _replace_once(
        source,
        'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"',
        'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"',
        label="handoff_path",
    )
    source = _replace_once(
        source,
        'REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"',
        'REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v4.py"',
        label="evaluator_path",
    )
    source = _replace_once(
        source,
        'REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"',
        'REPO_ROOT / "tools" / "mtvclc_validation_release_v4.py"',
        label="release_path",
    )
    marker = "_base_sealer = _execute_exact_source(\n"
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
    source = _replace_once(
        source,
        marker,
        adapter + marker,
        label="base_sealer_policy_adapter",
    )
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_SEALER_TEMPLATE_PATH,
    reason="v3_sealer_template_source_invalid",
)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_runtime_bound_sealer_v4_impl"
_implementation = ModuleType(_IMPLEMENTATION_NAME)
_implementation.__file__ = str(TOOL_PATH)
_implementation.__package__ = ""
_implementation.__dict__["__fxstack_exact_source_path__"] = TOOL_PATH
_implementation.__dict__["__fxstack_exact_source_raw__"] = _SELF_RAW
_implementation.__dict__["__fxstack_exact_source_stat_identity__"] = (
    _SELF_STAT_IDENTITY
)
_previous_implementation = sys.modules.get(_IMPLEMENTATION_NAME)
sys.modules[_IMPLEMENTATION_NAME] = _implementation
try:
    exec(  # noqa: S102 - exact template bytes plus counted deterministic transforms
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V4BootstrapRefusal("v4_sealer_derived_source_import_invalid") from exc
finally:
    if _previous_implementation is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous_implementation


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
    }
)

_template_source_paths = _implementation._source_paths


def _v4_source_paths() -> dict[str, Path]:
    paths = dict(_template_source_paths())
    paths["sealer_v3_template_source"] = V3_SEALER_TEMPLATE_PATH
    paths["handoff_v3_template_source"] = V3_HANDOFF_TEMPLATE_PATH
    paths["evaluator_v3_template_source"] = V3_EVALUATOR_TEMPLATE_PATH
    paths["release_v3_template_source"] = V3_RELEASE_TEMPLATE_PATH
    return paths


_implementation._source_paths = _v4_source_paths
_template_execution_provenance_contract = (
    _implementation.execution_provenance_contract
)


def execution_provenance_contract() -> dict[str, Any]:
    contract = dict(_template_execution_provenance_contract())
    contract.update(
        {
            "execution_mode": DERIVATION_MODE,
            "v3_template_source_sha256": hashlib.sha256(
                _V3_TEMPLATE_RAW
            ).hexdigest(),
            "v3_template_source_size_bytes": len(_V3_TEMPLATE_RAW),
            "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
            "literal_transform_count": 5,
            "legacy_production_strategy_module_loaded": False,
            "active_runtime_policy_exact_source_executed": True,
        }
    )
    return contract


_implementation.execution_provenance_contract = execution_provenance_contract

GapV4PreregistrationRefusal = _implementation.GapV3PreregistrationRefusal
base = _implementation.base
screen = _implementation.screen
_catalog = _implementation._catalog
SCREEN_PATH = _implementation.SCREEN_PATH
IG_MT4_SCALP_SCOPE_VERSION = _implementation.IG_MT4_SCALP_SCOPE_VERSION
IG_MT4_SCALP_SYMBOLS = _implementation.IG_MT4_SCALP_SYMBOLS
IG_MT4_VENUE_ID = _implementation.IG_MT4_VENUE_ID
BRIDGE_EA_REPOSITORY_SOURCE_PATH = (
    _implementation.BRIDGE_EA_REPOSITORY_SOURCE_PATH
)
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
FAILED_CROSSED_T0_ATTEMPT = _implementation.FAILED_CROSSED_T0_ATTEMPT
_source_paths = _v4_source_paths
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
        "source_identity": dict(
            source_identities["production_strategy_policy_source"]
        ),
        "strategy_id": policy.MTVCLC_STRATEGY_ID,
        "strategy_version": policy.MTVCLC_STRATEGY_VERSION,
        "config_id": policy.MTVCLC_CONFIG_ID,
        "config_sha256": policy.MTVCLC_CONFIG_SHA256,
        "evaluator_entrypoint": "evaluate_mtvclc",
        "ordered_symbols": list(policy.MTVCLC_V1_SYMBOLS),
        "ordered_cell_count": len(policy.MTVCLC_V1_SYMBOLS) * 2,
        "engine_identity_sha256": base.canonical_sha256(engine_identity),
        "entry_type": "immediate_market",
        "buy_price_basis": "authenticated_ask",
        "sell_price_basis": "authenticated_bid",
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
    """Build a v4 policy-bound declaration; never publish or collect implicitly."""

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
        raise GapV4PreregistrationRefusal("source_identities_invalid")
    runtime = identities.get("production_runtime_context")
    if not isinstance(runtime, dict) or not isinstance(
        runtime.get("engine_identity"), Mapping
    ):
        raise GapV4PreregistrationRefusal("production_engine_identity_invalid")
    policy = _implementation._production_strategy
    runtime.update(
        {
            "relationship": "active_runtime_policy_identity_context_only_no_authority",
            "active_strategy_family_context": policy.MTVCLC_STRATEGY_ID,
            "active_strategy_version_context": policy.MTVCLC_STRATEGY_VERSION,
            "active_policy_config_sha256_context": policy.MTVCLC_CONFIG_SHA256,
        }
    )
    payload["preregistration_tool_revision"] = V4_TOOL_REVISION
    payload["collector_wire_profile"] = COLLECTOR_WIRE_PROFILE
    payload["runtime_policy_binding"] = _runtime_policy_binding(
        source_identities=identities,
        engine_identity=runtime["engine_identity"],
    )
    payload["preregistration_body_sha256"] = base.canonical_sha256(payload)
    if not validate_preregistration(payload):
        raise GapV4PreregistrationRefusal("runtime_bound_v4_envelope_invalid")
    return payload


def validate_preregistration(payload: Mapping[str, Any]) -> bool:
    """Deep, filesystem-free validation of the additional v4 policy binding."""

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
    return bool(
        payload.get("preregistration_tool_revision") == V4_TOOL_REVISION
        and payload.get("collector_wire_profile") == COLLECTOR_WIRE_PROFILE
        and runtime_binding == expected_binding
        and isinstance(source_identity, Mapping)
        and source_identity.get("filename") == "mtvclc.py"
        and source_identity
        == _implementation._PRODUCTION_STRATEGY_SOURCE_IMAGE.identity()
        and runtime.get("relationship")
        == "active_runtime_policy_identity_context_only_no_authority"
        and runtime.get("active_strategy_family_context")
        == policy.MTVCLC_STRATEGY_ID
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
        and isinstance(authority, Mapping)
        and authority
        and not any(authority.values())
        and "sealer_v3_template_source" in identities
        and "handoff_v3_template_source" in identities
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
    """Exclusively publish after v4 and inherited gap-v3 checks both pass."""

    if not validate_preregistration(payload):
        raise GapV4PreregistrationRefusal("runtime_bound_v4_envelope_invalid")
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
            "Seal an authority-free MTVCLC runtime-policy-bound prospective "
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
    except (GapV4PreregistrationRefusal, V4BootstrapRefusal, OSError) as exc:
        print(f"runtime-bound v4 preregistration refused: {exc}", file=sys.stderr)
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
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
