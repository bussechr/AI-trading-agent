"""Versioned offline evaluator for a runtime-policy-bound v4 capture.

This file changes no screen, outcome, ledger, or gate arithmetic.  It binds
the established post-window evaluator to the v4 handoff, which first proves
the active ``fxstack.strategy.mtvclc`` policy identity.  Evaluation remains
impossible before the sealed end and this module has no live or authority
surface.
"""

from __future__ import annotations

# AGENT: ROLE: isolated post-window evaluator adapter for v4-bound evidence.
# AGENT: HANDSHAKE: exact v4 handoff -> unchanged v3 screen/ledger evaluator.
# AGENT: ISOLATION: immutable local files only; no network, signing, or runtime.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL_PATH = Path(__file__).resolve()
V3_EVALUATOR_TEMPLATE_PATH = (
    REPO_ROOT / "tools" / "evaluate_mt4_tick_volume_post_window_v3.py"
)
V4_HANDOFF_PATH = (
    REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"
)
EVALUATOR_TOOL_REVISION = "fxstack.scalp.mtvclc_runtime_bound_evaluator_tool.v4"
DERIVATION_MODE = "exact_v3_evaluator_template_plus_counted_literal_v4_transform"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024


class V4EvaluatorBootstrapRefusal(RuntimeError):
    """Raised before the inherited offline evaluator is safely available."""


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
        raise V4EvaluatorBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4EvaluatorBootstrapRefusal(reason)
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
    return _read_exact_source(TOOL_PATH, reason="v4_evaluator_source_invalid")


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4EvaluatorBootstrapRefusal(
            "v3_evaluator_template_encoding_invalid"
        ) from exc
    old = 'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v3.py"'
    new = 'REPO_ROOT / "tools" / "verify_mt4_tick_volume_capture_handoff_v4.py"'
    if source.count(old) != 1:
        raise V4EvaluatorBootstrapRefusal(
            "v3_evaluator_template_transform_invalid:handoff_path"
        )
    return source.replace(old, new, 1).encode("utf-8")


_SELF_RAW, _SELF_STAT_IDENTITY = _self_source()
_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_EVALUATOR_TEMPLATE_PATH,
    reason="v3_evaluator_template_source_invalid",
)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)

_IMPLEMENTATION_NAME = "_fxstack_mtvclc_runtime_bound_evaluator_v4_impl"
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
    exec(  # noqa: S102 - exact template bytes plus one counted literal transform
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V4EvaluatorBootstrapRefusal(
        "v4_evaluator_derived_source_import_invalid"
    ) from exc
finally:
    if _previous_implementation is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous_implementation


_template_executed_source_identities = _implementation.executed_source_identities


def executed_source_identities() -> dict[str, dict[str, Any]]:
    identities = dict(_template_executed_source_identities())
    identities["evaluator_v3_template_source"] = {
        "filename": V3_EVALUATOR_TEMPLATE_PATH.name,
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
EvaluationRefusal = _implementation.EvaluationRefusal
executed_source_identities = executed_source_identities
evaluate_post_window = _implementation.evaluate_post_window


def derivation_identity() -> dict[str, Any]:
    return {
        "tool_revision": EVALUATOR_TOOL_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_source_sha256": hashlib.sha256(
            _V3_TEMPLATE_RAW
        ).hexdigest(),
        "v3_template_source_size_bytes": len(_V3_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 1,
        "v4_handoff_filename": V4_HANDOFF_PATH.name,
        "early_evaluation_allowed": False,
        "authority_granted": False,
    }


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
