"""Offline handoff verifier for the independent MTVCLC v5 successor.

The exact gap-v3 inventory verifier is retained, but it is bound to the v4
collector adapter and v5 preregistration.  It performs no network access,
outcome evaluation, signing, runtime control, or broker action.
"""

from __future__ import annotations

# AGENT: ROLE: offline capture-to-research handoff for the v5 successor.
# AGENT: HANDSHAKE: v5 declaration + closed v4-adapter capture -> inventory.
# AGENT: ISOLATION: local immutable evidence only; every authority remains false.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = Path(__file__).resolve()
V3_HANDOFF_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"
)
V4_COLLECTOR_PATH = (
    REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"
)
V5_SEALER_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v5.py"
)
HANDOFF_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_handoff_tool.v5"
PROFILE_GAP_V5 = "gap_v5_runtime_policy_bound"
DERIVATION_MODE = "exact_v3_handoff_template_plus_counted_literal_v5_transforms"
EXPECTED_V3_TEMPLATE_SHA256 = (
    "01d863d7b2323748f8aafa7bccc20e28995efc8e5d7b2f1a62195c8d9a971a35"
)
EXPECTED_V3_TEMPLATE_SIZE_BYTES = 61_150
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024


class V5HandoffBootstrapRefusal(RuntimeError):
    """Raised before the pinned handoff implementation is safely available."""


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
        raise V5HandoffBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V5HandoffBootstrapRefusal(reason)
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
    return _read_exact_source(TOOL_PATH, reason="v5_handoff_source_invalid")


def _assert_pinned_v3_template(raw: bytes) -> None:
    if (
        len(raw) != EXPECTED_V3_TEMPLATE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != EXPECTED_V3_TEMPLATE_SHA256
    ):
        raise V5HandoffBootstrapRefusal("v3_handoff_template_identity_invalid")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V5HandoffBootstrapRefusal(
            f"v3_handoff_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _derive_v5_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V5HandoffBootstrapRefusal(
            "v3_handoff_template_encoding_invalid"
        ) from exc
    transforms = (
        (
            'REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v3.py"',
            'REPO_ROOT / "tools" / "capture_ig_mt4_m1_activity_resilient_v4.py"',
            "collector_path",
        ),
        (
            'REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v3.py"',
            'REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v5.py"',
            "sealer_path",
        ),
        (
            'PROFILE_GAP_V3 = "gap_v3_anchored"',
            f'PROFILE_GAP_V3 = "{PROFILE_GAP_V5}"',
            "handoff_profile",
        ),
        (
            'GUARD_IDENTITY_FILENAME = "collector-guard.identity.gap-v3.v1.json"',
            'GUARD_IDENTITY_FILENAME = "collector-guard.identity.gap-v5.v1.json"\n'
            'GUARD_IDENTITY_SCHEMA_VERSION = (\n'
            '    "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"\n'
            ')',
            "guard_identity_filename_and_schema",
        ),
        (
            'SUPERVISION_DIRECTORY = "supervision-gap-v3"',
            'SUPERVISION_DIRECTORY = "supervision-gap-v5"',
            "supervision_directory",
        ),
        (
            '    required = {\n'
            '        "collector_source_sha256": binding.collector_source_sha256,',
            '    required = {\n'
            '        "schema_version": GUARD_IDENTITY_SCHEMA_VERSION,\n'
            '        "collector_source_sha256": binding.collector_source_sha256,',
            "guard_identity_schema_validation",
        ),
    )
    for old, new, label in transforms:
        source = _replace_once(source, old, new, label=label)
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_HANDOFF_TEMPLATE_PATH,
    reason="v3_handoff_template_source_invalid",
)
_assert_pinned_v3_template(_V3_TEMPLATE_RAW)
_DERIVED_SOURCE = _derive_v5_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_runtime_bound_handoff_v5_impl"
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
    exec(  # noqa: S102 - pinned template bytes plus counted transforms
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V5HandoffBootstrapRefusal(
        "v5_handoff_derived_source_import_invalid"
    ) from exc
finally:
    if _previous is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous


_template_executed_source_identities = _implementation.executed_source_identities


def executed_source_identities() -> dict[str, dict[str, Any]]:
    identities = dict(_template_executed_source_identities())
    identities["handoff_v3_template_source"] = {
        "filename": V3_HANDOFF_TEMPLATE_PATH.name,
        "sha256": EXPECTED_V3_TEMPLATE_SHA256,
        "size_bytes": EXPECTED_V3_TEMPLATE_SIZE_BYTES,
    }
    return identities


_implementation.executed_source_identities = executed_source_identities
for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value

PROFILE_GAP_V5 = PROFILE_GAP_V5
PROFILE_REPLACEMENT = PROFILE_GAP_V5
PROFILE_GAP_V3 = PROFILE_GAP_V5
sealer = _implementation.sealer
collector = _implementation.collector
HandoffRefusal = _implementation.HandoffRefusal
_read_exact_source = _implementation._read_exact_source
_execute_exact_source = _implementation._execute_exact_source
load_preregistration = _implementation.load_preregistration
verify_capture_handoff = _implementation.verify_capture_handoff
load_handoff_artifact = _implementation.load_handoff_artifact
publish_handoff = _implementation.publish_handoff
executed_source_identities = executed_source_identities


def derivation_identity() -> dict[str, Any]:
    return {
        "tool_revision": HANDOFF_TOOL_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_source_sha256": EXPECTED_V3_TEMPLATE_SHA256,
        "v3_template_source_size_bytes": EXPECTED_V3_TEMPLATE_SIZE_BYTES,
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 6,
        "v4_collector_filename": V4_COLLECTOR_PATH.name,
        "v5_sealer_filename": V5_SEALER_PATH.name,
        "active_runtime_policy_required": True,
        "immediate_market_trade_required": True,
        "pending_orders_forbidden": True,
        "guard_identity_filename": "collector-guard.identity.gap-v5.v1.json",
        "guard_identity_schema_version": (
            "fxstack.mtvclc_collector_guard_identity.gap_v5.v1"
        ),
        "supervision_directory": "supervision-gap-v5",
        "outcome_evaluation_performed": False,
        "authority_granted": False,
    }


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
