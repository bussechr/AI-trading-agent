"""Count-corrected MTVCLC screen for the independent v5 successor.

The strategy, trade geometry, ledgers, and fixed gates remain the exact v3
screen.  Only the prospective multiplicity family advances after the failed
v4 attempt: 4,830 prior cells plus 44 current cells equals 4,874.  The v3
template and deterministic transforms are content-addressable public inputs;
this module has no live, broker, persistence, or authority surface.
"""

from __future__ import annotations

# AGENT: ROLE: pure 4,874-family MTVCLC post-window screen.
# AGENT: HANDSHAKE: exact v3 screen template -> counted multiplicity transforms.
# AGENT: ISOLATION: deterministic local source execution only.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


TOOL_PATH = Path(__file__).resolve()
V3_SCREEN_TEMPLATE_PATH = TOOL_PATH.with_name(
    "screen_mt4_tick_volume_close_location_continuation_replacement_v3.py"
)
SCREEN_REVISION = "fxstack.scalp.mtvclc_replacement_screen.v4"
DERIVATION_MODE = "exact_v3_screen_template_plus_counted_v4_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024
EXPECTED_V3_TEMPLATE_SHA256 = (
    "b2d0231413fe032516d09fc219a01175329f0409428f3d74f60f6df7191a2f66"
)
EXPECTED_V3_TEMPLATE_SIZE_BYTES = 21_086


class V4ScreenBootstrapRefusal(RuntimeError):
    """Raised before the exact count-corrected screen can be constructed."""


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
    candidate = path.absolute()
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
        raise V4ScreenBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V4ScreenBootstrapRefusal(reason)
    return bytes(raw), identities.pop()


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V4ScreenBootstrapRefusal(
            f"v3_screen_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _assert_pinned_v3_template(raw: bytes) -> None:
    if (
        len(raw) != EXPECTED_V3_TEMPLATE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != EXPECTED_V3_TEMPLATE_SHA256
    ):
        raise V4ScreenBootstrapRefusal("v3_screen_template_identity_invalid")


def _derive_v4_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V4ScreenBootstrapRefusal("v3_screen_template_encoding_invalid") from exc
    transforms = (
        (
            "IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_786",
            "IMMUTABLE_PRIOR_ATTEMPTED_CELLS = 4_830",
            "prior_cells",
        ),
        (
            "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_830",
            "IMMUTABLE_CUMULATIVE_ATTEMPTED_CELLS = 4_874",
            "cumulative_cells",
        ),
        (
            'manifest["win_probability_alpha_allocation"] = "one_sided_0.05_over_4830"',
            'manifest["win_probability_alpha_allocation"] = "one_sided_0.05_over_4874"',
            "wilson_alpha",
        ),
    )
    for old, new, label in transforms:
        source = _replace_once(source, old, new, label=label)
    return source.encode("utf-8")


_V3_TEMPLATE_RAW, _V3_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V3_SCREEN_TEMPLATE_PATH,
    reason="v3_screen_template_source_invalid",
)
_assert_pinned_v3_template(_V3_TEMPLATE_RAW)
_DERIVED_SOURCE = _derive_v4_implementation(_V3_TEMPLATE_RAW)
_IMPLEMENTATION_NAME = "_fxstack_mtvclc_replacement_screen_v4_impl"
_implementation = ModuleType(_IMPLEMENTATION_NAME)
_implementation.__file__ = str(TOOL_PATH)
_implementation.__package__ = __package__
_previous = sys.modules.get(_IMPLEMENTATION_NAME)
sys.modules[_IMPLEMENTATION_NAME] = _implementation
try:
    exec(  # noqa: S102 - exact template bytes plus counted deterministic transforms
        compile(_DERIVED_SOURCE, str(TOOL_PATH), "exec", dont_inherit=True),
        _implementation.__dict__,
    )
except Exception as exc:
    raise V4ScreenBootstrapRefusal("v4_screen_derived_source_import_invalid") from exc
finally:
    if _previous is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous

for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value


def __getattr__(name: str) -> Any:
    """Preserve the v3 screen's delegated constant and helper surface."""

    return getattr(_implementation, name)


def derivation_identity() -> dict[str, Any]:
    return {
        "screen_revision": SCREEN_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v3_template_filename": V3_SCREEN_TEMPLATE_PATH.name,
        "v3_template_sha256": hashlib.sha256(_V3_TEMPLATE_RAW).hexdigest(),
        "v3_template_size_bytes": len(_V3_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 3,
        "prior_attempted_cells": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells": 4_874,
        "authority_granted": False,
    }
