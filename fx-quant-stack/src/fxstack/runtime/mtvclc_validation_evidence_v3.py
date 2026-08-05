"""Public-only verifier for 4,874-family MTVCLC evidence v3.

This successor keeps the exact ledger-authenticated v2 verifier and advances
only its public schema and multiplicity mathematics after the failed v4
prospective attempt.  It contains no issuer, private key, runtime-control,
database, broker, or immediate-trade surface.
"""

from __future__ import annotations

# AGENT: ROLE: installed public-only verifier for MTVCLC evidence v3.
# AGENT: HANDSHAKE: signed v3 bundle + public key + expectation -> public result.
# AGENT: ISOLATION: no issuer, private key, persistence, activation, or trading.

import hashlib
import os
from pathlib import Path
import stat
import sys
from types import ModuleType
from typing import Any


TOOL_PATH = Path(__file__).resolve()
V2_TEMPLATE_PATH = TOOL_PATH.with_name("mtvclc_validation_evidence_v2.py")
VERIFIER_REVISION = "fxstack.runtime.mtvclc_validation_evidence.v3"
DERIVATION_MODE = "exact_v2_public_verifier_plus_counted_v3_transforms"
_MAXIMUM_SOURCE_BYTES = 8 * 1024 * 1024
EXPECTED_V2_TEMPLATE_SHA256 = (
    "30885ff8c559d3785b91eae725aed7a4302e5fb28e798625de88e2358910a08e"
)
EXPECTED_V2_TEMPLATE_SIZE_BYTES = 24_400


class V3VerifierBootstrapRefusal(RuntimeError):
    """Raised before the count-corrected public verifier is available."""


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        int(value.st_dev),
        int(value.st_ino),
        int(value.st_size),
        int(value.st_mtime_ns),
        int(value.st_ctime_ns),
    )


def _read_exact_source(path: Path, *, reason: str) -> tuple[bytes, tuple[int, ...]]:
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
            or before_path.st_size > _MAXIMUM_SOURCE_BYTES
        ):
            raise OSError(reason)
        descriptor = os.open(candidate, flags)
        try:
            before_handle = os.fstat(descriptor)
            raw = os.read(descriptor, int(before_handle.st_size) + 1)
            after_handle = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        after_path = candidate.lstat()
    except OSError as exc:
        raise V3VerifierBootstrapRefusal(reason) from exc
    identities = {
        _stat_identity(value)
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise V3VerifierBootstrapRefusal(reason)
    return raw, identities.pop()


def _replace_once(source: str, old: str, new: str, *, label: str) -> str:
    if source.count(old) != 1:
        raise V3VerifierBootstrapRefusal(
            f"v2_verifier_template_transform_invalid:{label}"
        )
    return source.replace(old, new, 1)


def _assert_pinned_v2_template(raw: bytes) -> None:
    if (
        len(raw) != EXPECTED_V2_TEMPLATE_SIZE_BYTES
        or hashlib.sha256(raw).hexdigest() != EXPECTED_V2_TEMPLATE_SHA256
    ):
        raise V3VerifierBootstrapRefusal(
            "v2_public_verifier_template_identity_invalid"
        )


def _derive_v3_implementation(raw: bytes) -> bytes:
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise V3VerifierBootstrapRefusal("v2_verifier_template_encoding_invalid") from exc
    transforms = (
        (
            '    "fxstack.scalp.mtvclc_validation_certificate.v2"',
            '    "fxstack.scalp.mtvclc_validation_certificate.v3"',
            "certificate_schema",
        ),
        (
            'MTVCLC_VALIDATION_EVIDENCE_SCHEMA = "fxstack.scalp.mtvclc_validation_evidence.v2"',
            'MTVCLC_VALIDATION_EVIDENCE_SCHEMA = "fxstack.scalp.mtvclc_validation_evidence.v3"',
            "evidence_schema",
        ),
        (
            '    "fxstack.scalp.mtvclc_signed_evidence_bundle.v2"',
            '    "fxstack.scalp.mtvclc_signed_evidence_bundle.v3"',
            "bundle_schema",
        ),
        (
            "WILSON_FAMILY_ATTEMPTED_CELLS = 4_830",
            "WILSON_FAMILY_ATTEMPTED_CELLS = 4_874",
            "wilson_family",
        ),
        (
            'WILSON_ALPHA_ALLOCATION = "one_sided_0.05_over_4830"',
            'WILSON_ALPHA_ALLOCATION = "one_sided_0.05_over_4874"',
            "wilson_alpha",
        ),
        (
            '    "one_sided_wilson_family_adjusted_over_4830_attempted_cells"',
            '    "one_sided_wilson_family_adjusted_over_4874_attempted_cells"',
            "wilson_method",
        ),
        (
            '    "prior_attempted_cells_lower_bound": 4_786,',
            '    "prior_attempted_cells_lower_bound": 4_830,',
            "prior_cells",
        ),
        (
            '    "cumulative_attempted_cells_lower_bound": 4_830,',
            '    "cumulative_attempted_cells_lower_bound": 4_874,',
            "cumulative_cells",
        ),
    )
    for old, new, label in transforms:
        source = _replace_once(source, old, new, label=label)
    return source.encode("utf-8")


_V2_TEMPLATE_RAW, _V2_TEMPLATE_STAT_IDENTITY = _read_exact_source(
    V2_TEMPLATE_PATH,
    reason="v2_public_verifier_template_source_invalid",
)
_assert_pinned_v2_template(_V2_TEMPLATE_RAW)
_DERIVED_SOURCE = _derive_v3_implementation(_V2_TEMPLATE_RAW)
_IMPLEMENTATION_NAME = "_fxstack_mtvclc_validation_evidence_v3_impl"
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
    raise V3VerifierBootstrapRefusal(
        "v3_public_verifier_derived_source_import_invalid"
    ) from exc
finally:
    if _previous is None:
        sys.modules.pop(_IMPLEMENTATION_NAME, None)
    else:
        sys.modules[_IMPLEMENTATION_NAME] = _previous

for _name, _value in vars(_implementation).items():
    if _name.startswith("__") or _name in globals():
        continue
    globals()[_name] = _value


def derivation_identity() -> dict[str, Any]:
    return {
        "verifier_revision": VERIFIER_REVISION,
        "derivation_mode": DERIVATION_MODE,
        "v2_template_filename": V2_TEMPLATE_PATH.name,
        "v2_template_sha256": hashlib.sha256(_V2_TEMPLATE_RAW).hexdigest(),
        "v2_template_size_bytes": len(_V2_TEMPLATE_RAW),
        "derived_source_sha256": hashlib.sha256(_DERIVED_SOURCE).hexdigest(),
        "literal_transform_count": 8,
        "prior_attempted_cells": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells": 4_874,
        "private_key_or_runtime_authority_granted": False,
    }
