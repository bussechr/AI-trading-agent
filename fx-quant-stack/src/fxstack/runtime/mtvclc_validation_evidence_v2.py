"""Public-only verifier for ledger-authenticated MTVCLC evidence v2.

The issuer remains external.  This installed module verifies an Ed25519-signed
bundle, recomputes the 4,830-family Wilson bounds, and requires an explicit
ledger-authentication claim produced by the versioned release verifier.  It
contains no private-key, research-input, activation, database, broker, or trade
surface.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import math
import os
from pathlib import Path
import stat
import sys

# AGENT: ROLE: installed public-only verifier for MTVCLC evidence v2.
# AGENT: HANDSHAKE: signed v2 bundle + public key + expectation -> public result.
# AGENT: ISOLATION: no issuer, private key, persistence, activation, or trading.
from collections.abc import Mapping
from copy import deepcopy
from statistics import NormalDist
from types import ModuleType
from typing import Any


_FXSTACK_ROOT = Path(__file__).resolve().parents[1]
_CATALOG_PATH = _FXSTACK_ROOT / "providers" / "ig_mt4_catalog.py"
_V1_PATH = Path(__file__).resolve().with_name("mtvclc_validation_evidence.py")


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
        (
            int(value.st_dev),
            int(value.st_ino),
            int(value.st_size),
            int(value.st_mtime_ns),
            int(value.st_ctime_ns),
        )
        for value in (before_path, before_handle, after_handle, after_path)
    }
    if len(identities) != 1 or len(raw) != before_handle.st_size:
        raise RuntimeError(reason)
    return raw, identities.pop()


@contextmanager
def _exact_dependency_bindings(
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
    installed = {**packages, **bindings}
    missing = object()
    previous = {name: sys.modules.get(name, missing) for name in installed}
    try:
        for name, module in sorted(
            installed.items(), key=lambda item: item[0].count(".")
        ):
            sys.modules[name] = module
            parent, _, child = name.rpartition(".")
            if parent:
                setattr(installed[parent], child, module)
        yield
    finally:
        for name in sorted(installed, key=lambda item: item.count("."), reverse=True):
            prior = previous[name]
            if prior is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = prior  # type: ignore[assignment]


def _execute_exact_source(
    *,
    raw: bytes,
    path: Path,
    module_name: str,
    injected: Mapping[str, ModuleType] | None = None,
) -> ModuleType:
    module = ModuleType(module_name)
    module.__file__ = str(path)
    module.__package__ = module_name.rpartition(".")[0]
    bindings = dict(injected or {})
    bindings[module_name] = module
    with _exact_dependency_bindings(bindings):
        exec(  # noqa: S102 - exact stable descriptor snapshot; never workspace pyc
            compile(raw, str(path), "exec", dont_inherit=True),
            module.__dict__,
        )
    return module


_catalog_raw, _CATALOG_STAT_IDENTITY = _read_exact_source(
    _CATALOG_PATH, reason="mtvclc_catalog_source_invalid"
)
_v1_raw, _V1_STAT_IDENTITY = _read_exact_source(
    _V1_PATH, reason="mtvclc_v1_verifier_source_invalid"
)
_catalog = _execute_exact_source(
    raw=_catalog_raw,
    path=_CATALOG_PATH,
    module_name="_fxstack_mtvclc_v2_exact_catalog",
)
_v1 = _execute_exact_source(
    raw=_v1_raw,
    path=_V1_PATH,
    module_name="_fxstack_mtvclc_v2_exact_v1",
    injected={"fxstack.providers.ig_mt4_catalog": _catalog},
)
IG_MT4_SCALP_SCOPE_VERSION = _catalog.IG_MT4_SCALP_SCOPE_VERSION
IG_MT4_SCALP_SYMBOLS = _catalog.IG_MT4_SCALP_SYMBOLS
IG_MT4_VENUE_ID = _catalog.IG_MT4_VENUE_ID
EXECUTED_DEPENDENCY_IDENTITIES: dict[str, dict[str, Any]] = {
    "scope_catalog_source": {
        "filename": _CATALOG_PATH.name,
        "sha256": hashlib.sha256(_catalog_raw).hexdigest(),
        "size_bytes": len(_catalog_raw),
    },
    "public_verifier_v1_support_source": {
        "filename": _V1_PATH.name,
        "sha256": hashlib.sha256(_v1_raw).hexdigest(),
        "size_bytes": len(_v1_raw),
    },
}

MTVCLC_STRATEGY_ID = _v1.MTVCLC_STRATEGY_ID
MTVCLC_STRATEGY_VERSION = _v1.MTVCLC_STRATEGY_VERSION
MTVCLC_CONFIG_ID = _v1.MTVCLC_CONFIG_ID
MTVCLC_SOURCE_CONTRACT_ID = _v1.MTVCLC_SOURCE_CONTRACT_ID
MTVCLC_ACTIVITY_METRIC_ID = _v1.MTVCLC_ACTIVITY_METRIC_ID
MTVCLC_ACCOUNT_MODE = _v1.MTVCLC_ACCOUNT_MODE

MTVCLC_VALIDATION_CERTIFICATE_SCHEMA = (
    "fxstack.scalp.mtvclc_validation_certificate.v2"
)
MTVCLC_VALIDATION_EVIDENCE_SCHEMA = "fxstack.scalp.mtvclc_validation_evidence.v2"
MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA = (
    "fxstack.scalp.mtvclc_signed_evidence_bundle.v2"
)
LEDGER_AUTHENTICATION_SCHEMA = (
    "fxstack.scalp.mtvclc_ledger_authentication.v1"
)
CERTIFICATE_SHA256_FIELD = _v1.CERTIFICATE_SHA256_FIELD
CERTIFICATE_SIGNATURE_FIELD = _v1.CERTIFICATE_SIGNATURE_FIELD
BUNDLE_SHA256_FIELD = _v1.BUNDLE_SHA256_FIELD

MAX_CERTIFICATE_VALIDITY_SECS = _v1.MAX_CERTIFICATE_VALIDITY_SECS
MIN_TRADES_PER_CELL = _v1.MIN_TRADES_PER_CELL
MIN_INDEPENDENT_DAYS_PER_CELL = _v1.MIN_INDEPENDENT_DAYS_PER_CELL
MIN_TOTAL_TRADES = _v1.MIN_TOTAL_TRADES
MIN_TOTAL_INDEPENDENT_DAYS = _v1.MIN_TOTAL_INDEPENDENT_DAYS
WIN_PROBABILITY_FAMILY_CONFIDENCE = 0.95
WILSON_FAMILY_ATTEMPTED_CELLS = 4_830
WILSON_ALPHA_ALLOCATION = "one_sided_0.05_over_4830"
WILSON_INTERVAL_METHOD = (
    "one_sided_wilson_family_adjusted_over_4830_attempted_cells"
)
_CELL_Z = NormalDist().inv_cdf(
    1.0
    - (1.0 - WIN_PROBABILITY_FAMILY_CONFIDENCE)
    / WILSON_FAMILY_ATTEMPTED_CELLS
)

EXPECTED_ATTEMPT_ACCOUNTING: dict[str, int] = {
    "prior_attempted_cells_lower_bound": 4_786,
    "current_attempted_cells": 44,
    "cumulative_attempted_cells_lower_bound": 4_830,
}
EXPECTED_EXECUTION_CONTRACT = dict(_v1.EXPECTED_EXECUTION_CONTRACT)
EXPECTED_SEALED_GATES = {
    **_v1.EXPECTED_SEALED_GATES,
    "cell_win_probability_interval": WILSON_INTERVAL_METHOD,
    "descriptive_df99_bonferroni_abs_t_threshold": 4.648050309953223,
}
NO_RUNTIME_AUTHORITY = dict(_v1.NO_RUNTIME_AUTHORITY)

MTVCLCValidationExpectation = _v1.MTVCLCValidationExpectation
MTVCLCValidationVerification = _v1.MTVCLCValidationVerification
canonical_json_bytes = _v1.canonical_json_bytes
canonical_sha256 = _v1.canonical_sha256
certificate_body_sha256 = _v1.certificate_body_sha256
bundle_body_sha256 = _v1.bundle_body_sha256
ed25519_public_key_id = _v1.ed25519_public_key_id


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _strict_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _is_sha256(value: Any) -> bool:
    text = str(value or "").lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def wilson_one_sided_lower(*, wins: int, trials: int) -> float:
    if trials <= 0 or wins < 0 or wins > trials:
        return 0.0
    point = wins / trials
    z_sq = _CELL_Z * _CELL_Z
    denominator = 1.0 + z_sq / trials
    center = point + z_sq / (2.0 * trials)
    radius = _CELL_Z * math.sqrt(
        point * (1.0 - point) / trials + z_sq / (4.0 * trials * trials)
    )
    return max(0.0, (center - radius) / denominator)


def _ledger_authentication_error(value: Any, *, total_trades: int) -> str:
    expected_fields = {
        "schema_version",
        "reservation_rows",
        "outcome_rows",
        "cell_rows",
        "duplicate_reservation_keys",
        "duplicate_outcome_keys",
        "missing_outcomes",
        "orphan_outcomes",
        "inconsistent_pairs",
        "empty_ledgers_rejected",
        "cell_summaries_recomputed_exclusively_from_ledgers",
        "screen_result_bundle_validated",
        "reservation_ledger_sha256",
        "outcome_ledger_sha256",
        "cell_ledger_sha256",
        "screen_source_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        return "mtvclc_evidence_ledger_authentication_scope_invalid"
    reservations = _strict_int(value.get("reservation_rows"), minimum=1)
    outcomes = _strict_int(value.get("outcome_rows"), minimum=1)
    cells = _strict_int(value.get("cell_rows"), minimum=1)
    if (
        value.get("schema_version") != LEDGER_AUTHENTICATION_SCHEMA
        or reservations != total_trades
        or outcomes != total_trades
        or cells != 44
        or any(
            _strict_int(value.get(field), minimum=0) != 0
            for field in (
                "duplicate_reservation_keys",
                "duplicate_outcome_keys",
                "missing_outcomes",
                "orphan_outcomes",
                "inconsistent_pairs",
            )
        )
        or value.get("empty_ledgers_rejected") is not True
        or value.get("cell_summaries_recomputed_exclusively_from_ledgers") is not True
        or value.get("screen_result_bundle_validated") is not True
        or any(
            not _is_sha256(value.get(field))
            for field in (
                "reservation_ledger_sha256",
                "outcome_ledger_sha256",
                "cell_ledger_sha256",
                "screen_source_sha256",
            )
        )
    ):
        return "mtvclc_evidence_ledger_authentication_invalid"
    return ""


def _legacy_projection(value: Mapping[str, Any]) -> dict[str, Any] | None:
    """Project only after independently checking all v2-specific mathematics."""

    projected = deepcopy(dict(value))
    projected.pop("ledger_authentication", None)
    projected["schema_version"] = _v1.MTVCLC_VALIDATION_EVIDENCE_SCHEMA
    projected["attempt_accounting"] = dict(_v1.EXPECTED_ATTEMPT_ACCOUNTING)
    projected["wilson_allocation"] = {
        "method": _v1.WILSON_INTERVAL_METHOD,
        "family_confidence": _v1.WIN_PROBABILITY_FAMILY_CONFIDENCE,
        "attempted_cells": _v1.WILSON_FAMILY_ATTEMPTED_CELLS,
        "alpha_allocation": _v1.WILSON_ALPHA_ALLOCATION,
    }
    projected["sealed_gates"] = dict(_v1.EXPECTED_SEALED_GATES)
    cells = projected.get("cells")
    if not isinstance(cells, list) or len(cells) != 44:
        return None
    for cell in cells:
        if not isinstance(cell, dict):
            return None
        reservations = _strict_int(cell.get("reservations"), minimum=1)
        wins = _strict_int(cell.get("wins"), minimum=0)
        recorded = _finite(cell.get("win_probability_wilson_lower"))
        if (
            reservations is None
            or wins is None
            or wins > reservations
            or recorded is None
        ):
            return None
        expected_v2 = wilson_one_sided_lower(wins=wins, trials=reservations)
        if not math.isclose(recorded, expected_v2, rel_tol=0.0, abs_tol=1e-15):
            return None
        cell["win_probability_wilson_lower"] = _v1.wilson_one_sided_lower(
            wins=wins, trials=reservations
        )
    return projected


def mtvclc_evidence_error(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "mtvclc_evidence_missing"
    expected_top = {
        "schema_version",
        "account_mode",
        "strategy",
        "scope",
        "execution_contract",
        "attempt_accounting",
        "wilson_allocation",
        "sealed_gates",
        "artifacts",
        "costs",
        "overall",
        "cells",
        "ledger_authentication",
        "authority",
    }
    if set(value) != expected_top:
        return "mtvclc_evidence_scope_invalid"
    if value.get("schema_version") != MTVCLC_VALIDATION_EVIDENCE_SCHEMA:
        return "mtvclc_evidence_schema_invalid"
    if value.get("attempt_accounting") != EXPECTED_ATTEMPT_ACCOUNTING:
        return "mtvclc_evidence_attempt_accounting_invalid"
    if value.get("wilson_allocation") != {
        "method": WILSON_INTERVAL_METHOD,
        "family_confidence": WIN_PROBABILITY_FAMILY_CONFIDENCE,
        "attempted_cells": WILSON_FAMILY_ATTEMPTED_CELLS,
        "alpha_allocation": WILSON_ALPHA_ALLOCATION,
    }:
        return "mtvclc_evidence_wilson_allocation_invalid"
    if value.get("sealed_gates") != EXPECTED_SEALED_GATES:
        return "mtvclc_evidence_sealed_gates_invalid"
    overall = value.get("overall")
    total_trades = (
        _strict_int(overall.get("total_trades"), minimum=1)
        if isinstance(overall, Mapping)
        else None
    )
    if total_trades is None:
        return "mtvclc_evidence_overall_gates_failed"
    ledger_error = _ledger_authentication_error(
        value.get("ledger_authentication"), total_trades=total_trades
    )
    if ledger_error:
        return ledger_error
    artifacts = value.get("artifacts")
    ledger = value.get("ledger_authentication")
    if not isinstance(artifacts, Mapping) or not isinstance(ledger, Mapping):
        return "mtvclc_evidence_artifact_scope_invalid"
    for artifact_field, ledger_field in (
        ("reservation_ledger_sha256", "reservation_ledger_sha256"),
        ("outcome_ledger_sha256", "outcome_ledger_sha256"),
        ("cell_ledger_sha256", "cell_ledger_sha256"),
    ):
        if artifacts.get(artifact_field) != ledger.get(ledger_field):
            return "mtvclc_evidence_ledger_artifact_binding_invalid"
    projected = _legacy_projection(value)
    if projected is None:
        return "mtvclc_evidence_cell_math_invalid"
    return _v1.mtvclc_evidence_error(projected)


def _verify_signature(certificate: Mapping[str, Any], public_key: Any) -> str:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return "signature_backend_unavailable"
    if not isinstance(public_key, Ed25519PublicKey):
        return "public_key_invalid"
    encoded = certificate.get(CERTIFICATE_SIGNATURE_FIELD)
    if not isinstance(encoded, str) or not encoded:
        return "signature_missing"
    try:
        signature = base64.b64decode(encoded, validate=True)
        material = {
            key: item
            for key, item in certificate.items()
            if key != CERTIFICATE_SIGNATURE_FIELD
        }
        public_key.verify(signature, canonical_json_bytes(material))
    except (InvalidSignature, TypeError, ValueError):
        return "signature_invalid"
    return ""


def verify_mtvclc_validation_evidence(
    *,
    bundle: Mapping[str, Any] | None,
    public_key: Any | None,
    expectation: MTVCLCValidationExpectation,
    now_epoch: float,
) -> MTVCLCValidationVerification:
    """Authenticate one v2 evidence-only bundle and recompute every public gate."""

    key_id = ed25519_public_key_id(public_key)
    authenticated = False
    cert_sha = ""
    evidence_sha = ""

    def result(
        reason: str,
        *,
        issued_at: float = 0.0,
        expires_at: float = 0.0,
        artifacts: Mapping[str, Any] | None = None,
    ) -> MTVCLCValidationVerification:
        valid = not reason
        return MTVCLCValidationVerification(
            valid=valid,
            reason=reason,
            errors=() if valid else (reason,),
            authenticated=authenticated,
            certificate_sha256=cert_sha,
            evidence_sha256=evidence_sha,
            signing_key_id=key_id,
            generation_id=expectation.generation_id,
            strategy_id=expectation.strategy_id,
            strategy_version=expectation.strategy_version,
            config_id=expectation.config_id,
            config_sha256=expectation.config_sha256,
            evaluator_source_sha256=expectation.evaluator_source_sha256,
            venue_id=IG_MT4_VENUE_ID if valid else "",
            account_mode=MTVCLC_ACCOUNT_MODE if valid else "",
            scope_version=IG_MT4_SCALP_SCOPE_VERSION if valid else "",
            symbol_scope=IG_MT4_SCALP_SYMBOLS if valid else (),
            issued_at_epoch=issued_at,
            expires_at_epoch=expires_at,
            artifact_sha256={
                str(key): str(item)
                for key, item in dict(artifacts or {}).items()
            }
            if valid
            else {},
        )

    expectation_error = expectation.validation_error()
    if expectation_error:
        return result(expectation_error)
    if not key_id:
        return result("mtvclc_public_key_unavailable")
    if not isinstance(bundle, Mapping) or set(bundle) != {
        "schema_version",
        "certificate",
        BUNDLE_SHA256_FIELD,
    }:
        return result("mtvclc_bundle_scope_invalid")
    if bundle.get("schema_version") != MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA:
        return result("mtvclc_bundle_schema_invalid")
    claimed_bundle_sha = str(bundle.get(BUNDLE_SHA256_FIELD) or "").lower()
    try:
        computed_bundle_sha = bundle_body_sha256(bundle)
    except (TypeError, ValueError):
        return result("mtvclc_bundle_body_invalid")
    if not _is_sha256(claimed_bundle_sha) or not hmac.compare_digest(
        claimed_bundle_sha, computed_bundle_sha
    ):
        return result("mtvclc_bundle_hash_invalid")
    raw_certificate = bundle.get("certificate")
    if not isinstance(raw_certificate, Mapping):
        return result("mtvclc_certificate_missing")
    certificate = dict(raw_certificate)
    expected_fields = {
        "schema_version",
        "generation_id",
        "strategy_id",
        "strategy_version",
        "config_id",
        "config_sha256",
        "evaluator_source_sha256",
        "venue_id",
        "account_mode",
        "scope_version",
        "symbol_scope",
        "issued_at_epoch",
        "expires_at_epoch",
        "signing_key_id",
        "evidence",
        "evidence_sha256",
        "authority",
        CERTIFICATE_SHA256_FIELD,
        CERTIFICATE_SIGNATURE_FIELD,
    }
    if set(certificate) != expected_fields:
        return result("mtvclc_certificate_scope_invalid")
    if certificate.get("signing_key_id") != key_id:
        return result("mtvclc_certificate_signing_key_mismatch")
    claimed_cert_sha = str(certificate.get(CERTIFICATE_SHA256_FIELD) or "").lower()
    try:
        computed_cert_sha = certificate_body_sha256(certificate)
    except (TypeError, ValueError):
        return result("mtvclc_certificate_body_invalid")
    if not _is_sha256(claimed_cert_sha) or not hmac.compare_digest(
        claimed_cert_sha, computed_cert_sha
    ):
        return result("mtvclc_certificate_hash_invalid")
    signature_error = _verify_signature(certificate, public_key)
    if signature_error:
        return result(f"mtvclc_certificate_{signature_error}")
    authenticated = True
    cert_sha = claimed_cert_sha
    if certificate.get("schema_version") != MTVCLC_VALIDATION_CERTIFICATE_SCHEMA:
        return result("mtvclc_certificate_schema_invalid")
    expected_claims = {
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_source_sha256": expectation.evaluator_source_sha256,
        "venue_id": IG_MT4_VENUE_ID,
        "account_mode": MTVCLC_ACCOUNT_MODE,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "authority": NO_RUNTIME_AUTHORITY,
    }
    for field_name, expected in expected_claims.items():
        if certificate.get(field_name) != expected:
            return result(f"mtvclc_certificate_{field_name}_mismatch")
    now = _finite(now_epoch)
    issued_at = _finite(certificate.get("issued_at_epoch"))
    expires_at = _finite(certificate.get("expires_at_epoch"))
    if now is None or now <= 0.0:
        return result("mtvclc_clock_invalid")
    if (
        issued_at is None
        or expires_at is None
        or issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at > MAX_CERTIFICATE_VALIDITY_SECS
    ):
        return result("mtvclc_certificate_time_window_invalid")
    if issued_at > now + 5.0:
        return result(
            "mtvclc_certificate_future_dated",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    if expires_at <= now:
        return result(
            "mtvclc_certificate_expired",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    evidence = certificate.get("evidence")
    if not isinstance(evidence, Mapping):
        return result("mtvclc_evidence_missing")
    try:
        computed_evidence_sha = canonical_sha256(evidence)
    except (TypeError, ValueError):
        return result("mtvclc_evidence_invalid")
    claimed_evidence_sha = str(certificate.get("evidence_sha256") or "").lower()
    if not _is_sha256(claimed_evidence_sha) or not hmac.compare_digest(
        claimed_evidence_sha, computed_evidence_sha
    ):
        return result("mtvclc_evidence_hash_invalid")
    evidence_sha = computed_evidence_sha
    error = mtvclc_evidence_error(evidence)
    if error:
        return result(error, issued_at=issued_at, expires_at=expires_at)
    strategy = evidence["strategy"]
    artifacts = evidence["artifacts"]
    if (
        strategy["config_sha256"] != expectation.config_sha256
        or artifacts["evaluator_source_sha256"]
        != expectation.evaluator_source_sha256
    ):
        return result(
            "mtvclc_evidence_expectation_mismatch",
            issued_at=issued_at,
            expires_at=expires_at,
        )
    return result("", issued_at=issued_at, expires_at=expires_at, artifacts=artifacts)


__all__ = [
    "BUNDLE_SHA256_FIELD",
    "CERTIFICATE_SHA256_FIELD",
    "CERTIFICATE_SIGNATURE_FIELD",
    "EXPECTED_ATTEMPT_ACCOUNTING",
    "EXPECTED_EXECUTION_CONTRACT",
    "EXPECTED_SEALED_GATES",
    "LEDGER_AUTHENTICATION_SCHEMA",
    "MAX_CERTIFICATE_VALIDITY_SECS",
    "MTVCLC_ACCOUNT_MODE",
    "MTVCLC_ACTIVITY_METRIC_ID",
    "MTVCLC_CONFIG_ID",
    "MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA",
    "MTVCLC_SOURCE_CONTRACT_ID",
    "MTVCLC_STRATEGY_ID",
    "MTVCLC_STRATEGY_VERSION",
    "MTVCLC_VALIDATION_CERTIFICATE_SCHEMA",
    "MTVCLC_VALIDATION_EVIDENCE_SCHEMA",
    "NO_RUNTIME_AUTHORITY",
    "MTVCLCValidationExpectation",
    "MTVCLCValidationVerification",
    "bundle_body_sha256",
    "canonical_json_bytes",
    "canonical_sha256",
    "certificate_body_sha256",
    "ed25519_public_key_id",
    "mtvclc_evidence_error",
    "verify_mtvclc_validation_evidence",
    "wilson_one_sided_lower",
]
