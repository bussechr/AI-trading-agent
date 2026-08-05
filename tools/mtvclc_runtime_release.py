#!/usr/bin/env python3
"""Offline two-phase issuer for an MTVCLC IG-DEMO runtime release.

``prepare`` authenticates an already-sealed evidence-v3 bundle and its exact
v5 preregistration, measures the production engine, and writes a complete
unsigned outer-v2 release request.
``issue`` repeats every public check before its first private-key file access,
signs the exact certificate and revocation registry, and parity-checks the
finished bundle with the installed public-only runtime verifier.

This external tool has no key generation, settings, database, bridge, broker,
activation, deployment, or runtime-process surface.
"""

from __future__ import annotations

import argparse
import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import stat
import sys
import time
from typing import Any


# AGENT: ROLE: external-only MTVCLC runtime-release prepare/issue ceremony.
# AGENT: HANDSHAKE: exact v5 prereg + signed evidence-v3 + engine -> outer-v2 release.
# AGENT: ISOLATION: no key generation, live inputs, activation, or broker I/O.
REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.runtime import mtvclc_runtime_release as runtime_release  # noqa: E402
from fxstack.runtime import mtvclc_validation_evidence_v3 as evidence_v3  # noqa: E402
from fxstack.runtime.scalp_engine_identity import (  # noqa: E402
    ProductionScalpEngineIdentity,
    production_scalp_engine_identity,
)
from tools import (  # noqa: E402
    seal_mt4_tick_volume_preregistration_resilient_v5 as preregistration_v5,
)


ISSUANCE_REQUEST_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_release_issuance_request.v2"
)
ISSUANCE_REQUEST_SHA256_FIELD = "request_body_sha256"
MAXIMUM_JSON_BYTES = 64 * 1024 * 1024
MAXIMUM_KEY_BYTES = 64 * 1024

NO_ISSUANCE_AUTHORITY: dict[str, bool] = {
    "signature_authorized": False,
    "activation_authorized": False,
    "runtime_authorized": False,
    "entry_lane_authorized": False,
    "individual_trade_authorized": False,
    "registry_write_authorized": False,
    "broker_access_authorized": False,
    "broker_trade_authorized": False,
    "research_access_authorized": False,
    "real_account_authorized": False,
}

_REQUEST_FIELDS = {
    "schema_version",
    "prepared_at_epoch",
    "registry_mode",
    "release_public_key_id",
    "evidence_public_key_id",
    "validated_preregistration_binding",
    "evidence_bundle",
    "public_input_manifest",
    "engine_binding",
    "previous_registry_binding",
    "unsigned_certificate",
    "unsigned_revocation_registry",
    "authority",
    ISSUANCE_REQUEST_SHA256_FIELD,
}
_UNSIGNED_CERTIFICATE_FIELDS = {
    "schema_version",
    "generation_id",
    "strategy_id",
    "strategy_version",
    "config_id",
    "config_sha256",
    "evaluator_source_sha256",
    "engine_binding",
    "deployment",
    "execution_contract",
    "validated_preregistration_binding",
    "evidence_binding",
    "cost_binding",
    "qualification_surface",
    "authority_purpose",
    "authority",
    "issued_at_epoch",
    "expires_at_epoch",
    "signing_key_purpose",
    "signing_key_id",
    runtime_release.CERTIFICATE_SHA256_FIELD,
}
_UNSIGNED_REGISTRY_FIELDS = {
    "schema_version",
    "registry_generation_id",
    "registry_revision",
    "previous_registry_sha256",
    "issued_at_epoch",
    "expires_at_epoch",
    "active_certificate_sha256",
    "revoked_certificate_sha256s",
    "signing_key_purpose",
    "signing_key_id",
    runtime_release.REGISTRY_SHA256_FIELD,
}
_SIGNED_REGISTRY_FIELDS = {
    *_UNSIGNED_REGISTRY_FIELDS,
    runtime_release.REGISTRY_SIGNATURE_FIELD,
}
_RUNTIME_BUNDLE_FIELDS = {
    "schema_version",
    "evidence_bundle",
    "certificate",
    "revocation_registry",
    runtime_release.BUNDLE_SHA256_FIELD,
}


class MTVCLCRuntimeReleaseRefusal(RuntimeError):
    """Fail-closed refusal from the offline ceremony."""


@dataclass(frozen=True, slots=True)
class LoadedFile:
    path: Path
    raw: bytes
    sha256: str

    def identity(self) -> dict[str, Any]:
        return {
            "filename": self.path.name,
            "sha256": self.sha256,
            "size_bytes": len(self.raw),
        }


@dataclass(frozen=True, slots=True)
class PreviousRegistryState:
    registry_revision: int
    registry_sha256: str
    active_certificate_sha256: str
    revoked_certificate_sha256s: tuple[str, ...]
    loaded_file: LoadedFile


@dataclass(frozen=True, slots=True)
class PublicInputs:
    preregistration: dict[str, Any]
    validated_preregistration_binding: dict[str, Any]
    preregistration_file: LoadedFile
    evidence_bundle: dict[str, Any]
    evidence_certificate: dict[str, Any]
    evidence: dict[str, Any]
    evidence_verification: evidence_v3.MTVCLCValidationVerification
    evidence_bundle_file: LoadedFile
    evidence_public_key: Any
    evidence_public_key_file: LoadedFile
    release_public_key: Any
    release_public_key_file: LoadedFile
    engine_identity: ProductionScalpEngineIdentity
    previous_registry: PreviousRegistryState | None


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strict_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _is_reparse_point(path: Path) -> bool:
    marker = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    try:
        return bool(int(getattr(path.lstat(), "st_file_attributes", 0)) & marker)
    except OSError:
        return True


def _read_regular(
    path: str | Path, *, label: str, maximum_bytes: int
) -> LoadedFile:
    candidate = Path(path).expanduser().absolute()
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        before_path = candidate.lstat()
        if (
            candidate.is_symlink()
            or _is_reparse_point(candidate)
            or not stat.S_ISREG(before_path.st_mode)
            or before_path.st_size <= 0
            or before_path.st_size > maximum_bytes
        ):
            raise OSError(label)
        descriptor = os.open(candidate, flags)
    except OSError as exc:
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_file_invalid") from exc
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
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_file_changed") from exc
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
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_file_changed")
    return LoadedFile(
        path=candidate.resolve(strict=True),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _load_json_object(
    path: str | Path, *, label: str, maximum_bytes: int = MAXIMUM_JSON_BYTES
) -> tuple[LoadedFile, dict[str, Any]]:
    loaded = _read_regular(path, label=label, maximum_bytes=maximum_bytes)
    try:
        value = json.loads(loaded.raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_json_invalid") from exc
    if not isinstance(value, dict):
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_scope_invalid")
    try:
        expected_raw = evidence_v3.canonical_json_bytes(value) + b"\n"
    except (TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_json_invalid") from exc
    if not hmac.compare_digest(loaded.raw, expected_raw):
        raise MTVCLCRuntimeReleaseRefusal(f"{label}_canonical_bytes_invalid")
    return loaded, value


def _json_normalized(value: Any) -> Any:
    try:
        return json.loads(
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise MTVCLCRuntimeReleaseRefusal("json_normalization_invalid") from exc


def _load_and_validate_preregistration(
    *,
    path: str | Path,
    evidence: Mapping[str, Any],
    engine_identity: ProductionScalpEngineIdentity,
) -> tuple[LoadedFile, dict[str, Any], dict[str, Any]]:
    """Race-safely load the exact pretty-published v5 preregistration."""

    loaded = _read_regular(
        path, label="preregistration", maximum_bytes=MAXIMUM_JSON_BYTES
    )
    try:
        value = json.loads(loaded.raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise MTVCLCRuntimeReleaseRefusal("preregistration_json_invalid") from exc
    if not isinstance(value, dict):
        raise MTVCLCRuntimeReleaseRefusal("preregistration_scope_invalid")
    try:
        expected_raw = runtime_release._preregistration_artifact_bytes(value)
    except (TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal("preregistration_json_invalid") from exc
    if not hmac.compare_digest(loaded.raw, expected_raw):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_canonical_bytes_invalid"
        )
    try:
        valid_v5 = preregistration_v5.validate_preregistration(value)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_v5_validation_failed"
        ) from exc
    if not valid_v5:
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_v5_validation_failed"
        )
    claimed_body_sha = str(
        value.get("preregistration_body_sha256") or ""
    ).lower()
    try:
        computed_body_sha = runtime_release._preregistration_body_sha256(value)
    except (TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_body_invalid"
        ) from exc
    if not _is_sha256(claimed_body_sha) or not hmac.compare_digest(
        claimed_body_sha, computed_body_sha
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_body_sha256_mismatch"
        )
    expected_filename = (
        f"mtvclc_gap_v3_preregistration_{claimed_body_sha}.json"
    )
    if loaded.path.name != expected_filename:
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_content_addressed_filename_invalid"
        )
    binding = runtime_release._derived_validated_preregistration_binding(value)
    if binding is None:
        raise MTVCLCRuntimeReleaseRefusal(
            "validated_preregistration_binding_invalid"
        )
    if not hmac.compare_digest(
        str(binding.get("preregistration_artifact_sha256") or ""),
        loaded.sha256,
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_artifact_sha256_mismatch"
        )
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, Mapping):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_evidence_artifacts_invalid"
        )
    if not hmac.compare_digest(
        str(artifacts.get("preregistration_body_sha256") or "").lower(),
        claimed_body_sha,
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_evidence_body_sha256_mismatch"
        )
    if not hmac.compare_digest(
        str(artifacts.get("preregistration_artifact_sha256") or "").lower(),
        loaded.sha256,
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_evidence_artifact_sha256_mismatch"
        )
    expected_engine = _json_normalized(engine_identity.to_dict())
    sealed_engine = _json_normalized(binding["sealed_engine_identity"])
    if sealed_engine != expected_engine:
        raise MTVCLCRuntimeReleaseRefusal(
            "preregistration_production_engine_identity_mismatch"
        )
    return loaded, value, binding


def _load_public_key(
    path: str | Path, *, label: str
) -> tuple[LoadedFile, Any]:
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    loaded = _read_regular(path, label=label, maximum_bytes=MAXIMUM_KEY_BYTES)
    candidates: list[Any] = []
    for loader in (serialization.load_pem_public_key, serialization.load_ssh_public_key):
        try:
            candidates.append(loader(loaded.raw))
        except (TypeError, ValueError, UnsupportedAlgorithm):
            continue
    if len(loaded.raw) == 32:
        try:
            candidates.append(Ed25519PublicKey.from_public_bytes(loaded.raw))
        except ValueError:
            pass
    for candidate in candidates:
        if isinstance(candidate, Ed25519PublicKey):
            return loaded, candidate
    raise MTVCLCRuntimeReleaseRefusal(f"{label}_key_invalid")


def _load_private_key(path: str | Path) -> Any:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    loaded = _read_regular(
        path, label="release_signing", maximum_bytes=MAXIMUM_KEY_BYTES
    )
    try:
        key = serialization.load_pem_private_key(loaded.raw, password=None)
    except (TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal("release_signing_key_invalid") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise MTVCLCRuntimeReleaseRefusal("release_signing_key_not_ed25519")
    return key


def _request_body_sha256(request: Mapping[str, Any]) -> str:
    return evidence_v3.canonical_sha256(
        {
            key: value
            for key, value in dict(request).items()
            if key != ISSUANCE_REQUEST_SHA256_FIELD
        }
    )


def _validate_output_target(path: str | Path) -> Path:
    target = Path(path).expanduser().absolute()
    if target.exists() or target.is_symlink():
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_output_exists")
    if (
        not target.parent.is_dir()
        or target.parent.is_symlink()
        or _is_reparse_point(target.parent)
    ):
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_output_parent_invalid")
    return target


def _write_new_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    target = _validate_output_target(path)
    encoded = evidence_v3.canonical_json_bytes(payload) + b"\n"
    temporary = target.parent / f".{target.name}.{os.getpid()}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_temporary_exists")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, target)
        temporary.unlink()
        target.chmod(stat.S_IREAD)
        if target.read_bytes() != encoded:
            raise MTVCLCRuntimeReleaseRefusal("runtime_release_reopen_mismatch")
    except MTVCLCRuntimeReleaseRefusal:
        raise
    except OSError as exc:
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_publish_failed") from exc
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass
    return target.resolve(strict=True)


def _verify_signature(
    *, payload: Mapping[str, Any], signature_field: str, public_key: Any, reason: str
) -> None:
    from cryptography.exceptions import InvalidSignature

    encoded = payload.get(signature_field)
    if not isinstance(encoded, str) or not encoded:
        raise MTVCLCRuntimeReleaseRefusal(f"{reason}_signature_missing")
    material = {
        key: value for key, value in payload.items() if key != signature_field
    }
    try:
        signature = base64.b64decode(encoded, validate=True)
        public_key.verify(signature, evidence_v3.canonical_json_bytes(material))
    except (InvalidSignature, TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal(f"{reason}_signature_invalid") from exc


def _load_previous_registry(
    *, path: str | Path, public_key: Any, generation_id: str, now_epoch: float
) -> PreviousRegistryState:
    loaded, payload = _load_json_object(path, label="previous_registry")
    previous_certificate_sha = ""
    if payload.get("schema_version") == runtime_release.MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA:
        if set(payload) != _RUNTIME_BUNDLE_FIELDS:
            raise MTVCLCRuntimeReleaseRefusal("previous_release_bundle_scope_invalid")
        claimed_bundle_sha = str(
            payload.get(runtime_release.BUNDLE_SHA256_FIELD) or ""
        ).lower()
        try:
            computed_bundle_sha = runtime_release.release_bundle_body_sha256(payload)
        except (TypeError, ValueError) as exc:
            raise MTVCLCRuntimeReleaseRefusal(
                "previous_release_bundle_body_invalid"
            ) from exc
        if not _is_sha256(claimed_bundle_sha) or not hmac.compare_digest(
            claimed_bundle_sha, computed_bundle_sha
        ):
            raise MTVCLCRuntimeReleaseRefusal("previous_release_bundle_hash_invalid")
        certificate = payload.get("certificate")
        if not isinstance(certificate, Mapping):
            raise MTVCLCRuntimeReleaseRefusal(
                "previous_release_certificate_missing"
            )
        previous_certificate_sha = str(
            certificate.get(runtime_release.CERTIFICATE_SHA256_FIELD) or ""
        ).lower()
        if (
            not _is_sha256(previous_certificate_sha)
            or certificate.get("signing_key_id")
            != evidence_v3.ed25519_public_key_id(public_key)
            or certificate.get("signing_key_purpose")
            != runtime_release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE
            or certificate.get("generation_id") != generation_id
            or not hmac.compare_digest(
                previous_certificate_sha,
                runtime_release.release_certificate_body_sha256(certificate),
            )
        ):
            raise MTVCLCRuntimeReleaseRefusal(
                "previous_release_certificate_invalid"
            )
        _verify_signature(
            payload=certificate,
            signature_field=runtime_release.CERTIFICATE_SIGNATURE_FIELD,
            public_key=public_key,
            reason="previous_release_certificate",
        )
        registry_value = payload.get("revocation_registry")
    else:
        registry_value = payload
    if not isinstance(registry_value, Mapping):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_missing")
    registry = dict(registry_value)
    if set(registry) != _SIGNED_REGISTRY_FIELDS:
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_scope_invalid")
    key_id = evidence_v3.ed25519_public_key_id(public_key)
    if (
        registry.get("schema_version")
        != runtime_release.MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA
        or registry.get("registry_generation_id") != generation_id
        or registry.get("signing_key_purpose")
        != runtime_release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE
        or registry.get("signing_key_id") != key_id
    ):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_identity_invalid")
    claimed_sha = str(registry.get(runtime_release.REGISTRY_SHA256_FIELD) or "").lower()
    try:
        computed_sha = runtime_release.release_registry_body_sha256(registry)
    except (TypeError, ValueError) as exc:
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_body_invalid") from exc
    if not _is_sha256(claimed_sha) or not hmac.compare_digest(
        claimed_sha, computed_sha
    ):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_hash_invalid")
    _verify_signature(
        payload=registry,
        signature_field=runtime_release.REGISTRY_SIGNATURE_FIELD,
        public_key=public_key,
        reason="previous_registry",
    )
    revision = _strict_int(registry.get("registry_revision"), minimum=1)
    issued_at = _finite(registry.get("issued_at_epoch"))
    expires_at = _finite(registry.get("expires_at_epoch"))
    previous_sha = str(registry.get("previous_registry_sha256") or "").lower()
    if (
        revision is None
        or issued_at is None
        or expires_at is None
        or issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at
        > runtime_release.MAXIMUM_REGISTRY_VALIDITY_SECONDS
        or issued_at > now_epoch + 5.0
        or not _is_sha256(previous_sha)
        or (revision == 1 and previous_sha != "0" * 64)
    ):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_metadata_invalid")
    active = str(registry.get("active_certificate_sha256") or "").lower()
    revoked_raw = registry.get("revoked_certificate_sha256s")
    if not isinstance(revoked_raw, list):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_revocations_invalid")
    revoked = [str(item or "").lower() for item in revoked_raw]
    if (
        not _is_sha256(active)
        or active in revoked
        or any(not _is_sha256(item) for item in revoked)
        or revoked != sorted(revoked)
        or len(revoked) != len(set(revoked))
        or (previous_certificate_sha and active != previous_certificate_sha)
    ):
        raise MTVCLCRuntimeReleaseRefusal("previous_registry_revocations_invalid")
    return PreviousRegistryState(
        registry_revision=revision,
        registry_sha256=claimed_sha,
        active_certificate_sha256=active,
        revoked_certificate_sha256s=tuple(revoked),
        loaded_file=loaded,
    )


def _load_and_verify_evidence(
    *, bundle_path: str | Path, public_key: Any, now_epoch: float
) -> tuple[
    LoadedFile,
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    evidence_v3.MTVCLCValidationVerification,
]:
    loaded, bundle = _load_json_object(bundle_path, label="evidence_bundle")
    raw_certificate = bundle.get("certificate")
    if not isinstance(raw_certificate, Mapping):
        raise MTVCLCRuntimeReleaseRefusal("evidence_certificate_missing")
    certificate = dict(raw_certificate)
    raw_evidence = certificate.get("evidence")
    if not isinstance(raw_evidence, Mapping):
        raise MTVCLCRuntimeReleaseRefusal("evidence_payload_missing")
    evidence = dict(raw_evidence)
    generation_id = str(certificate.get("generation_id") or "").strip()
    evaluator_sha = str(certificate.get("evaluator_source_sha256") or "").lower()
    config_sha = str(certificate.get("config_sha256") or "").lower()
    if (
        not generation_id
        or config_sha != runtime_release.PRODUCTION_MTVCLC_CONFIG_SHA256
        or not _is_sha256(evaluator_sha)
    ):
        raise MTVCLCRuntimeReleaseRefusal("evidence_identity_invalid")
    expectation = evidence_v3.MTVCLCValidationExpectation(
        generation_id=generation_id,
        strategy_id=evidence_v3.MTVCLC_STRATEGY_ID,
        strategy_version=evidence_v3.MTVCLC_STRATEGY_VERSION,
        config_id=evidence_v3.MTVCLC_CONFIG_ID,
        config_sha256=config_sha,
        evaluator_source_sha256=evaluator_sha,
    )
    verification = evidence_v3.verify_mtvclc_validation_evidence(
        bundle=bundle,
        public_key=public_key,
        expectation=expectation,
        now_epoch=now_epoch,
    )
    if not verification.valid or not verification.authenticated:
        raise MTVCLCRuntimeReleaseRefusal(
            f"evidence_verification_failed:{verification.reason or 'unauthenticated'}"
        )
    return loaded, bundle, certificate, evidence, verification


def _registry_mode_error(
    *, previous_registry_path: str | Path | None, bootstrap_registry: bool
) -> str:
    if bootstrap_registry == bool(previous_registry_path):
        return "requires_exactly_one_of_previous_registry_or_bootstrap"
    return ""


def _load_public_inputs(
    *,
    preregistration_path: str | Path,
    evidence_bundle_path: str | Path,
    evidence_public_key_path: str | Path,
    release_public_key_path: str | Path,
    previous_registry_path: str | Path | None,
    bootstrap_registry: bool,
    package_root: str | Path | None,
    repository_root: str | Path | None,
    now_epoch: float,
) -> PublicInputs:
    mode_error = _registry_mode_error(
        previous_registry_path=previous_registry_path,
        bootstrap_registry=bootstrap_registry,
    )
    if mode_error:
        raise MTVCLCRuntimeReleaseRefusal(mode_error)
    evidence_key_file, evidence_public_key = _load_public_key(
        evidence_public_key_path, label="evidence_verification"
    )
    release_key_file, release_public_key = _load_public_key(
        release_public_key_path, label="release_verification"
    )
    evidence_key_id = evidence_v3.ed25519_public_key_id(evidence_public_key)
    release_key_id = evidence_v3.ed25519_public_key_id(release_public_key)
    if not evidence_key_id or not release_key_id:
        raise MTVCLCRuntimeReleaseRefusal("public_key_identity_invalid")
    if hmac.compare_digest(evidence_key_id, release_key_id):
        raise MTVCLCRuntimeReleaseRefusal("release_and_evidence_keys_not_distinct")
    (
        evidence_file,
        evidence_bundle,
        evidence_certificate,
        evidence,
        evidence_verification,
    ) = _load_and_verify_evidence(
        bundle_path=evidence_bundle_path,
        public_key=evidence_public_key,
        now_epoch=now_epoch,
    )
    selected_package_root = Path(
        package_root or (FXSTACK_SRC / "fxstack")
    ).resolve(strict=True)
    selected_repository_root = Path(repository_root or REPO_ROOT).resolve(strict=True)
    engine_identity = production_scalp_engine_identity(
        package_root=selected_package_root,
        repository_root=selected_repository_root,
    )
    (
        preregistration_file,
        preregistration,
        validated_preregistration_binding,
    ) = _load_and_validate_preregistration(
        path=preregistration_path,
        evidence=evidence,
        engine_identity=engine_identity,
    )
    previous = (
        _load_previous_registry(
            path=str(previous_registry_path),
            public_key=release_public_key,
            generation_id=evidence_verification.generation_id,
            now_epoch=now_epoch,
        )
        if previous_registry_path is not None
        else None
    )
    return PublicInputs(
        preregistration=preregistration,
        validated_preregistration_binding=validated_preregistration_binding,
        preregistration_file=preregistration_file,
        evidence_bundle=evidence_bundle,
        evidence_certificate=evidence_certificate,
        evidence=evidence,
        evidence_verification=evidence_verification,
        evidence_bundle_file=evidence_file,
        evidence_public_key=evidence_public_key,
        evidence_public_key_file=evidence_key_file,
        release_public_key=release_public_key,
        release_public_key_file=release_key_file,
        engine_identity=engine_identity,
        previous_registry=previous,
    )


def _public_manifest(inputs: PublicInputs) -> dict[str, Any]:
    manifest = {
        "preregistration": inputs.preregistration_file.identity(),
        "evidence_bundle": inputs.evidence_bundle_file.identity(),
        "evidence_public_key": inputs.evidence_public_key_file.identity(),
        "release_public_key": inputs.release_public_key_file.identity(),
    }
    if inputs.previous_registry is not None:
        manifest["previous_registry"] = (
            inputs.previous_registry.loaded_file.identity()
        )
    return manifest


def _build_unsigned_request(
    *,
    inputs: PublicInputs,
    issued_at_epoch: float,
    expires_at_epoch: float,
    validation_now_epoch: float,
) -> dict[str, Any]:
    issued_at = _finite(issued_at_epoch)
    expires_at = _finite(expires_at_epoch)
    now = _finite(validation_now_epoch)
    if (
        issued_at is None
        or expires_at is None
        or now is None
        or issued_at <= 0.0
        or expires_at <= issued_at
        or expires_at - issued_at
        > runtime_release.MAXIMUM_RELEASE_VALIDITY_SECONDS
        or issued_at > now + 5.0
        or expires_at <= now
        or expires_at > inputs.evidence_verification.expires_at_epoch
    ):
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_time_window_invalid")
    release_key_id = evidence_v3.ed25519_public_key_id(inputs.release_public_key)
    evidence_key_id = evidence_v3.ed25519_public_key_id(inputs.evidence_public_key)
    expectation = runtime_release.MTVCLCRuntimeReleaseExpectation(
        generation_id=inputs.evidence_verification.generation_id,
        config_sha256=inputs.evidence_verification.config_sha256,
        evaluator_source_sha256=(
            inputs.evidence_verification.evaluator_source_sha256
        ),
        engine_sha256=inputs.engine_identity.engine_sha256,
        engine_component_sha256=inputs.engine_identity.component_sha256,
    )
    expectation_error = expectation.validation_error()
    if expectation_error:
        raise MTVCLCRuntimeReleaseRefusal(expectation_error)
    engine_binding = runtime_release._expected_engine_binding(expectation)
    evidence_binding = runtime_release._derived_evidence_binding(
        evidence_bundle=inputs.evidence_bundle,
        evidence_certificate=inputs.evidence_certificate,
        verification=inputs.evidence_verification,
    )
    cost_binding = runtime_release._derived_cost_binding(inputs.evidence)
    if cost_binding is None:
        raise MTVCLCRuntimeReleaseRefusal("runtime_release_cost_binding_invalid")
    qualification_surface = runtime_release._derived_qualification_surface(
        inputs.evidence, cost_binding=cost_binding
    )
    if qualification_surface is None:
        raise MTVCLCRuntimeReleaseRefusal(
            "runtime_release_qualification_surface_invalid"
        )
    certificate: dict[str, Any] = {
        "schema_version": runtime_release.MTVCLC_RUNTIME_RELEASE_CERTIFICATE_SCHEMA,
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_source_sha256": expectation.evaluator_source_sha256,
        "engine_binding": engine_binding,
        "deployment": dict(runtime_release.EXPECTED_DEPLOYMENT),
        "execution_contract": dict(
            runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT
        ),
        "validated_preregistration_binding": dict(
            inputs.validated_preregistration_binding
        ),
        "evidence_binding": evidence_binding,
        "cost_binding": cost_binding,
        "qualification_surface": qualification_surface,
        "authority_purpose": (
            runtime_release.MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE
        ),
        "authority": dict(runtime_release.RUNTIME_RELEASE_AUTHORITY),
        "issued_at_epoch": issued_at,
        "expires_at_epoch": expires_at,
        "signing_key_purpose": runtime_release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE,
        "signing_key_id": release_key_id,
    }
    preregistration_error = runtime_release._validated_preregistration_error(
        preregistration=inputs.preregistration,
        binding=inputs.validated_preregistration_binding,
        evidence=inputs.evidence,
        certificate=certificate,
        expectation=expectation,
    )
    if preregistration_error:
        raise MTVCLCRuntimeReleaseRefusal(preregistration_error)
    certificate[runtime_release.CERTIFICATE_SHA256_FIELD] = (
        runtime_release.release_certificate_body_sha256(certificate)
    )
    certificate_sha = str(
        certificate[runtime_release.CERTIFICATE_SHA256_FIELD]
    )
    if inputs.previous_registry is None:
        registry_mode = "bootstrap"
        revision = 1
        previous_registry_sha = "0" * 64
        revoked: list[str] = []
        previous_binding = {
            "mode": "bootstrap",
            "registry_generation_id": expectation.generation_id,
            "registry_revision": 0,
            "registry_sha256": "0" * 64,
            "active_certificate_sha256": "",
            "revoked_certificate_sha256s": [],
        }
    else:
        registry_mode = "rotate"
        revision = inputs.previous_registry.registry_revision + 1
        previous_registry_sha = inputs.previous_registry.registry_sha256
        revoked = list(inputs.previous_registry.revoked_certificate_sha256s)
        previous_active = inputs.previous_registry.active_certificate_sha256
        if previous_active != certificate_sha and previous_active not in revoked:
            revoked.append(previous_active)
        revoked.sort()
        previous_binding = {
            "mode": "rotate",
            "registry_generation_id": expectation.generation_id,
            "registry_revision": inputs.previous_registry.registry_revision,
            "registry_sha256": inputs.previous_registry.registry_sha256,
            "active_certificate_sha256": (
                inputs.previous_registry.active_certificate_sha256
            ),
            "revoked_certificate_sha256s": list(
                inputs.previous_registry.revoked_certificate_sha256s
            ),
        }
    registry: dict[str, Any] = {
        "schema_version": runtime_release.MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA,
        "registry_generation_id": expectation.generation_id,
        "registry_revision": revision,
        "previous_registry_sha256": previous_registry_sha,
        "issued_at_epoch": issued_at,
        "expires_at_epoch": expires_at,
        "active_certificate_sha256": certificate_sha,
        "revoked_certificate_sha256s": revoked,
        "signing_key_purpose": runtime_release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE,
        "signing_key_id": release_key_id,
    }
    registry[runtime_release.REGISTRY_SHA256_FIELD] = (
        runtime_release.release_registry_body_sha256(registry)
    )
    request: dict[str, Any] = {
        "schema_version": ISSUANCE_REQUEST_SCHEMA,
        "prepared_at_epoch": issued_at,
        "registry_mode": registry_mode,
        "release_public_key_id": release_key_id,
        "evidence_public_key_id": evidence_key_id,
        "validated_preregistration_binding": dict(
            inputs.validated_preregistration_binding
        ),
        "evidence_bundle": inputs.evidence_bundle,
        "public_input_manifest": _public_manifest(inputs),
        "engine_binding": engine_binding,
        "previous_registry_binding": previous_binding,
        "unsigned_certificate": certificate,
        "unsigned_revocation_registry": registry,
        "authority": dict(NO_ISSUANCE_AUTHORITY),
    }
    request[ISSUANCE_REQUEST_SHA256_FIELD] = _request_body_sha256(request)
    return request


def _validate_request_shape(request: Mapping[str, Any]) -> None:
    if set(request) != _REQUEST_FIELDS:
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_scope_invalid")
    if (
        request.get("schema_version") != ISSUANCE_REQUEST_SCHEMA
        or request.get("authority") != NO_ISSUANCE_AUTHORITY
        or request.get("registry_mode") not in {"bootstrap", "rotate"}
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_identity_invalid")
    claimed_sha = str(request.get(ISSUANCE_REQUEST_SHA256_FIELD) or "").lower()
    if not _is_sha256(claimed_sha) or not hmac.compare_digest(
        claimed_sha, _request_body_sha256(request)
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_hash_invalid")
    certificate = request.get("unsigned_certificate")
    registry = request.get("unsigned_revocation_registry")
    preregistration_binding = request.get("validated_preregistration_binding")
    if (
        not isinstance(certificate, Mapping)
        or set(certificate) != _UNSIGNED_CERTIFICATE_FIELDS
        or not isinstance(registry, Mapping)
        or set(registry) != _UNSIGNED_REGISTRY_FIELDS
        or not isinstance(preregistration_binding, Mapping)
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_unsigned_scope_invalid")
    if (
        certificate.get("validated_preregistration_binding")
        != preregistration_binding
        or runtime_release._derived_validated_preregistration_binding(
            preregistration_binding.get("preregistration", {})
        )
        != preregistration_binding
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "issuance_request_preregistration_binding_invalid"
        )
    if not hmac.compare_digest(
        str(certificate.get(runtime_release.CERTIFICATE_SHA256_FIELD) or ""),
        runtime_release.release_certificate_body_sha256(certificate),
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_certificate_hash_invalid")
    if not hmac.compare_digest(
        str(registry.get(runtime_release.REGISTRY_SHA256_FIELD) or ""),
        runtime_release.release_registry_body_sha256(registry),
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_registry_hash_invalid")


def prepare_issuance_request(
    *,
    preregistration_path: str | Path,
    evidence_bundle_path: str | Path,
    evidence_public_key_path: str | Path,
    release_public_key_path: str | Path,
    output_path: str | Path,
    previous_registry_path: str | Path | None = None,
    bootstrap_registry: bool = False,
    package_root: str | Path | None = None,
    repository_root: str | Path | None = None,
    validity_secs: float = 3_600.0,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any], ProductionScalpEngineIdentity]:
    """Authenticate all public inputs and publish an unsigned exact request."""

    now = _finite(time.time() if now_epoch is None else now_epoch)
    validity = _finite(validity_secs)
    if (
        now is None
        or now <= 0.0
        or validity is None
        or validity <= 0.0
        or validity > runtime_release.MAXIMUM_RELEASE_VALIDITY_SECONDS
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_validity_invalid")
    _validate_output_target(output_path)
    inputs = _load_public_inputs(
        preregistration_path=preregistration_path,
        evidence_bundle_path=evidence_bundle_path,
        evidence_public_key_path=evidence_public_key_path,
        release_public_key_path=release_public_key_path,
        previous_registry_path=previous_registry_path,
        bootstrap_registry=bootstrap_registry,
        package_root=package_root,
        repository_root=repository_root,
        now_epoch=now,
    )
    request = _build_unsigned_request(
        inputs=inputs,
        issued_at_epoch=now,
        expires_at_epoch=now + validity,
        validation_now_epoch=now,
    )
    _validate_request_shape(request)
    output = _write_new_json(output_path, request)
    return output, request, inputs.engine_identity


def _load_request(path: str | Path) -> tuple[LoadedFile, dict[str, Any]]:
    loaded, request = _load_json_object(path, label="issuance_request")
    _validate_request_shape(request)
    return loaded, request


def _sign_unsigned_payload(
    *, payload: Mapping[str, Any], signature_field: str, signing_key: Any
) -> dict[str, Any]:
    signed = dict(payload)
    signature = signing_key.sign(evidence_v3.canonical_json_bytes(signed))
    signed[signature_field] = base64.b64encode(signature).decode("ascii")
    return signed


def issue_runtime_release_bundle(
    *,
    request_path: str | Path,
    preregistration_path: str | Path,
    evidence_bundle_path: str | Path,
    evidence_public_key_path: str | Path,
    release_public_key_path: str | Path,
    release_signing_key_path: str | Path,
    output_path: str | Path,
    previous_registry_path: str | Path | None = None,
    bootstrap_registry: bool = False,
    package_root: str | Path | None = None,
    repository_root: str | Path | None = None,
    now_epoch: float | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Repeat every public check, then and only then open the private key."""

    _request_file, request = _load_request(request_path)
    now = _finite(time.time() if now_epoch is None else now_epoch)
    if now is None or now <= 0.0:
        raise MTVCLCRuntimeReleaseRefusal("issuance_clock_invalid")
    prepared_at = _finite(request.get("prepared_at_epoch"))
    unsigned_certificate = request.get("unsigned_certificate")
    if prepared_at is None or not isinstance(unsigned_certificate, Mapping):
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_time_invalid")
    expires_at = _finite(unsigned_certificate.get("expires_at_epoch"))
    if expires_at is None:
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_time_invalid")
    _validate_output_target(output_path)
    inputs = _load_public_inputs(
        preregistration_path=preregistration_path,
        evidence_bundle_path=evidence_bundle_path,
        evidence_public_key_path=evidence_public_key_path,
        release_public_key_path=release_public_key_path,
        previous_registry_path=previous_registry_path,
        bootstrap_registry=bootstrap_registry,
        package_root=package_root,
        repository_root=repository_root,
        now_epoch=now,
    )
    rebuilt = _build_unsigned_request(
        inputs=inputs,
        issued_at_epoch=prepared_at,
        expires_at_epoch=expires_at,
        validation_now_epoch=now,
    )
    if request != rebuilt:
        raise MTVCLCRuntimeReleaseRefusal("issuance_request_public_revalidation_failed")

    # This is deliberately the first call that receives or opens the supplied
    # private-key path. All evidence, public keys, engine bytes, request bytes,
    # output safety, and prior registry state have been checked again above.
    signing_key = _load_private_key(release_signing_key_path)
    signing_key_id = evidence_v3.ed25519_public_key_id(signing_key.public_key())
    release_key_id = evidence_v3.ed25519_public_key_id(inputs.release_public_key)
    if not signing_key_id or not hmac.compare_digest(
        signing_key_id, release_key_id
    ):
        raise MTVCLCRuntimeReleaseRefusal("issuance_release_keypair_mismatch")
    certificate = _sign_unsigned_payload(
        payload=rebuilt["unsigned_certificate"],
        signature_field=runtime_release.CERTIFICATE_SIGNATURE_FIELD,
        signing_key=signing_key,
    )
    registry = _sign_unsigned_payload(
        payload=rebuilt["unsigned_revocation_registry"],
        signature_field=runtime_release.REGISTRY_SIGNATURE_FIELD,
        signing_key=signing_key,
    )
    bundle: dict[str, Any] = {
        "schema_version": runtime_release.MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA,
        "evidence_bundle": inputs.evidence_bundle,
        "certificate": certificate,
        "revocation_registry": registry,
    }
    bundle[runtime_release.BUNDLE_SHA256_FIELD] = (
        runtime_release.release_bundle_body_sha256(bundle)
    )
    expectation = runtime_release.MTVCLCRuntimeReleaseExpectation(
        generation_id=inputs.evidence_verification.generation_id,
        config_sha256=inputs.evidence_verification.config_sha256,
        evaluator_source_sha256=(
            inputs.evidence_verification.evaluator_source_sha256
        ),
        engine_sha256=inputs.engine_identity.engine_sha256,
        engine_component_sha256=inputs.engine_identity.component_sha256,
    )
    anchor = (
        runtime_release.MTVCLCRuntimeReleaseRegistryAnchor(
            generation_id=expectation.generation_id,
            registry_revision=inputs.previous_registry.registry_revision,
            registry_sha256=inputs.previous_registry.registry_sha256,
        )
        if inputs.previous_registry is not None
        else None
    )
    parity = runtime_release.verify_mtvclc_runtime_release(
        bundle=bundle,
        release_public_key=inputs.release_public_key,
        evidence_public_key=inputs.evidence_public_key,
        expectation=expectation,
        now_epoch=now,
        registry_anchor=anchor,
    )
    if not parity.valid or not parity.authenticated or not parity.revocation_verified:
        raise MTVCLCRuntimeReleaseRefusal(
            f"runtime_verifier_parity_failed:{parity.reason or 'unauthenticated'}"
        )
    if (
        parity.release_bundle_sha256
        != bundle[runtime_release.BUNDLE_SHA256_FIELD]
        or parity.certificate_sha256
        != certificate[runtime_release.CERTIFICATE_SHA256_FIELD]
        or parity.evidence_bundle_sha256
        != inputs.evidence_bundle[evidence_v3.BUNDLE_SHA256_FIELD]
        or parity.evidence_certificate_sha256
        != inputs.evidence_verification.certificate_sha256
        or parity.evidence_sha256 != inputs.evidence_verification.evidence_sha256
        or parity.signing_key_id != release_key_id
        or parity.evidence_signing_key_id
        != inputs.evidence_verification.signing_key_id
        or parity.registry_sha256 != registry[runtime_release.REGISTRY_SHA256_FIELD]
        or parity.qualification_surface_sha256
        != certificate["qualification_surface"]["surface_sha256"]
        or parity.engine_sha256 != inputs.engine_identity.engine_sha256
    ):
        raise MTVCLCRuntimeReleaseRefusal(
            "runtime_verifier_parity_identity_mismatch"
        )
    output = _write_new_json(output_path, bundle)
    return output, bundle


# Concise public phase aliases for programmatic offline ceremonies.
prepare = prepare_issuance_request
issue = issue_runtime_release_bundle


def _add_public_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preregistration", required=True)
    parser.add_argument("--evidence-bundle", required=True)
    parser.add_argument("--evidence-verification-key-file", required=True)
    parser.add_argument("--release-verification-key-file", required=True)
    parser.add_argument("--package-root", default=str(FXSTACK_SRC / "fxstack"))
    parser.add_argument("--repository-root", default=str(REPO_ROOT))
    registry = parser.add_mutually_exclusive_group(required=True)
    registry.add_argument("--previous-registry")
    registry.add_argument("--bootstrap-registry", action="store_true")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare or issue an MTVCLC IG-DEMO runtime release on an offline "
            "release host. This command cannot activate or execute trades."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser(
        "prepare", help="authenticate public inputs and write an unsigned request"
    )
    _add_public_inputs(prepare_parser)
    prepare_parser.add_argument("--validity-secs", type=float, default=3_600.0)
    prepare_parser.add_argument("--output", required=True)
    issue_parser = commands.add_parser(
        "issue", help="repeat public checks and explicitly sign the exact request"
    )
    _add_public_inputs(issue_parser)
    issue_parser.add_argument("--request", required=True)
    issue_parser.add_argument("--release-signing-key-file", required=True)
    issue_parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        common = {
            "preregistration_path": args.preregistration,
            "evidence_bundle_path": args.evidence_bundle,
            "evidence_public_key_path": args.evidence_verification_key_file,
            "release_public_key_path": args.release_verification_key_file,
            "previous_registry_path": args.previous_registry,
            "bootstrap_registry": bool(args.bootstrap_registry),
            "package_root": args.package_root,
            "repository_root": args.repository_root,
        }
        if args.command == "prepare":
            output, request, engine = prepare_issuance_request(
                **common,
                validity_secs=args.validity_secs,
                output_path=args.output,
            )
            print(
                json.dumps(
                    {
                        "status": "prepared",
                        "output": str(output),
                        "request_body_sha256": request[
                            ISSUANCE_REQUEST_SHA256_FIELD
                        ],
                        "engine_sha256": engine.engine_sha256,
                        "registry_mode": request["registry_mode"],
                        "authority": dict(NO_ISSUANCE_AUTHORITY),
                    },
                    sort_keys=True,
                )
            )
            return 0
        output, bundle = issue_runtime_release_bundle(
            **common,
            request_path=args.request,
            release_signing_key_path=args.release_signing_key_file,
            output_path=args.output,
        )
        certificate = bundle["certificate"]
        registry = bundle["revocation_registry"]
        print(
            json.dumps(
                {
                    "status": "issued",
                    "output": str(output),
                    "bundle_body_sha256": bundle[
                        runtime_release.BUNDLE_SHA256_FIELD
                    ],
                    "certificate_body_sha256": certificate[
                        runtime_release.CERTIFICATE_SHA256_FIELD
                    ],
                    "registry_revision": registry["registry_revision"],
                    "immediate_market_buy_sell_only": True,
                    "pending_trades_forbidden": True,
                    "individual_trade_authorized": False,
                    "broker_trade_authorized": False,
                },
                sort_keys=True,
            )
        )
        return 0
    except (MTVCLCRuntimeReleaseRefusal, OSError, RuntimeError, ValueError) as exc:
        print(f"MTVCLC runtime release refused: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "ISSUANCE_REQUEST_SCHEMA",
    "ISSUANCE_REQUEST_SHA256_FIELD",
    "MTVCLCRuntimeReleaseRefusal",
    "NO_ISSUANCE_AUTHORITY",
    "issue",
    "issue_runtime_release_bundle",
    "main",
    "prepare",
    "prepare_issuance_request",
]
