"""Offline two-phase release adapter for runtime-policy-bound v4 evidence.

The public validation and key-last issuance ceremony remain unchanged.  This
version only binds that ceremony to the v4 handoff and evaluator chain.  Module
import performs public descriptor reads only; a signing-key path is never
opened unless a future operator explicitly invokes the inherited issue phase
after every post-window public check passes.
"""

from __future__ import annotations

# AGENT: ROLE: isolated key-last validation-release adapter for v4 evidence.
# AGENT: HANDSHAKE: v4 handoff/evaluator artifacts -> inherited public ceremony.
# AGENT: ISOLATION: no key generation, activation, runtime, network, or broker path.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = Path(__file__).resolve()
V3_RELEASE_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "mtvclc_validation_release_v3.py"
)
V4_HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"
)
V4_EVALUATOR_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v4.py"
)
RELEASE_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_release_tool.v4"
DERIVATION_MODE = "exact_v3_release_template_plus_counted_literal_v4_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024


class V4ReleaseBootstrapRefusal(RuntimeError):
    """Raised before the inherited public release ceremony is safely bound."""


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
        raise V4ReleaseBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4ReleaseBootstrapRefusal(reason)
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
    return _read_exact_source(TOOL_PATH, reason="v4_release_source_invalid")


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V4ReleaseBootstrapRefusal(
            f"v3_release_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4ReleaseBootstrapRefusal(
            "v3_release_template_encoding_invalid"
        ) from exc
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
    return source.encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_RELEASE_TEMPLATE_PATH,
    reason="v3_release_template_source_invalid",
)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_runtime_bound_release_v4_impl"
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
    raise V4ReleaseBootstrapRefusal(
        "v4_release_derived_source_import_invalid"
    ) from exc
finally:
    if _previous_implementation is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous_implementation


_template_executed_source_identities = _implementation.executed_source_identities


def executed_source_identities() -> dict[str, dict[str, Any]]:
    identities = dict(_template_executed_source_identities())
    identities["release_v3_template_source"] = {
        "filename": V3_RELEASE_TEMPLATE_PATH.name,
        "sha256": hashlib.sha256(_V3_TEMPLATE_RAW).hexdigest(),
        "size_bytes": len(_V3_TEMPLATE_RAW),
    }
    return identities


_implementation.executed_source_identities = executed_source_identities
for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value

handoff = _implementation.handoff
sealer = _implementation.sealer
MTVCLCReleaseRefusal = _implementation.MTVCLCReleaseRefusal
executed_source_identities = executed_source_identities


def derivation_identity() -> dict[str, Any]:
    return {
        "tool_revision": RELEASE_TOOL_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_source_sha256": hashlib.sha256(
            _V3_TEMPLATE_RAW
        ).hexdigest(),
        "v3_template_source_size_bytes": len(_V3_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 2,
        "v4_handoff_filename": V4_HANDOFF_PATH.name,
        "v4_evaluator_filename": V4_EVALUATOR_PATH.name,
        "private_key_access_on_import": False,
        "authority_granted": False,
    }


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
