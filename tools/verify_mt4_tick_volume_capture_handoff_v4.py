"""Offline handoff verifier for an MTVCLC-runtime-bound v4 declaration.

The row, manifest, reservation-WAL, finalization, and restart-continuity
checks remain the pinned gap-v3 collector checks.  This boundary additionally
requires the v4 sealer's exact active ``fxstack.strategy.mtvclc`` policy
binding before any completed capture could be inventoried.  Importing or
running the tool never starts collection and never evaluates outcomes.
"""

from __future__ import annotations

# AGENT: ROLE: offline runtime-policy-bound capture-to-research handoff v4.
# AGENT: HANDSHAKE: v4 declaration + closed gap-v3 capture -> authority-free inventory.
# AGENT: ISOLATION: no network, credentials, outcomes, signing, runtime, or broker surface.

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
V4_SEALER_PATH = (
    REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v4.py"
)
HANDOFF_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_handoff_tool.v4"
PROFILE_GAP_V4 = "gap_v4_runtime_policy_bound"
DERIVATION_MODE = "exact_v3_handoff_template_plus_counted_literal_v4_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024


class V4HandoffBootstrapRefusal(RuntimeError):
    """Raised before the inherited handoff verifier can be bound safely."""


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
        raise V4HandoffBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4HandoffBootstrapRefusal(reason)
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
    return _read_exact_source(TOOL_PATH, reason="v4_handoff_source_invalid")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V4HandoffBootstrapRefusal(
            f"v3_handoff_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4HandoffBootstrapRefusal(
            "v3_handoff_template_encoding_invalid"
        ) from exc
    source = _replace_once(
        source,
        'REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v3.py"',
        'REPO_ROOT / "tools" / "seal_mt4_tick_volume_preregistration_resilient_v4.py"',
        label="sealer_path",
    )
    source = _replace_once(
        source,
        'PROFILE_GAP_V3 = "gap_v3_anchored"',
        f'PROFILE_GAP_V3 = "{PROFILE_GAP_V4}"',
        label="handoff_profile",
    )
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_HANDOFF_TEMPLATE_PATH,
    reason="v3_handoff_template_source_invalid",
)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_runtime_bound_handoff_v4_impl"
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
    raise V4HandoffBootstrapRefusal(
        "v4_handoff_derived_source_import_invalid"
    ) from exc
finally:
    if _previous_implementation is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous_implementation


_template_executed_source_identities = _implementation.executed_source_identities


def executed_source_identities() -> dict[str, dict[str, Any]]:
    identities = dict(_template_executed_source_identities())
    identities["handoff_v3_template_source"] = {
        "filename": V3_HANDOFF_TEMPLATE_PATH.name,
        "sha256": hashlib.sha256(_V3_TEMPLATE_RAW).hexdigest(),
        "size_bytes": len(_V3_TEMPLATE_RAW),
    }
    return identities


_implementation.executed_source_identities = executed_source_identities

# Re-export the mature streaming verifier surface.  The few v4-owned names
# above remain authoritative when a name overlaps.
for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value

PROFILE_GAP_V4 = PROFILE_GAP_V4
PROFILE_REPLACEMENT = PROFILE_GAP_V4
PROFILE_GAP_V3 = PROFILE_GAP_V4
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
    """Describe the exact, reproducible v4 handoff implementation derivation."""

    return {
        "tool_revision": HANDOFF_TOOL_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_source_sha256": hashlib.sha256(
            _V3_TEMPLATE_RAW
        ).hexdigest(),
        "v3_template_source_size_bytes": len(_V3_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 2,
        "v4_sealer_filename": V4_SEALER_PATH.name,
        "active_runtime_policy_required": True,
        "outcome_evaluation_performed": False,
        "authority_granted": False,
    }


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
