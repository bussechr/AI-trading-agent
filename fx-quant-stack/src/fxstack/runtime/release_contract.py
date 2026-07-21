"""Production-safe release identity and immutable evidence reader.

This module intentionally imports no training, backtest, research, or harness
package. Offline producers may perform richer economic checks before an
external signer witnesses the bundle; the production runtime verifies the
exact signed bytes, identities, gate results, and referenced object hashes.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


EVIDENCE_IDENTITY_SCHEMA = "phase5_release_evidence_identity_v1"
PHASE5_BUNDLE_SCHEMA = "phase5_gate_bundle_v2"
_SHA256_HEX_LEN = 64
_AUTHORITY_GATES = (
    "research_gate",
    "economic_gate",
    "operational_gate",
    "shadow_gate",
    "canary_gate",
)
_REQUIRED_FINALIZATION = {
    "release_validation_bundle",
    "finalization:fast_shadow",
    "finalization:long_shadow",
    "finalization:rollback_evidence",
    "finalization:blockers",
}
_REQUIRED_PROMOTION_COMPONENTS = {
    "swing_xgb",
    "intraday_xgb",
    "meta",
    "exit",
    "reversal_failure",
    "reversal_opportunity",
}


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == _SHA256_HEX_LEN and all(
        char in "0123456789abcdef" for char in text
    )


def read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, dict) else {}


def mapping_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        dict(value or {}),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _has_reparse_or_symlink(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        attributes = int(getattr(path.stat(), "st_file_attributes", 0) or 0)
    except OSError:
        return True
    return bool(attributes & 0x400)


def resolve_contained_evidence_ref(
    *,
    evidence_root: str | Path,
    reference: str,
) -> Path | None:
    """Resolve one portable relative ref without allowing path substitution."""

    root = Path(evidence_root)
    ref_text = str(reference or "").strip().replace("\\", "/")
    ref_path = Path(ref_text)
    if (
        not ref_text
        or ref_path.is_absolute()
        or ref_path.drive
        or any(part in {"", ".", ".."} for part in ref_path.parts)
    ):
        return None
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        return None
    if not root_resolved.is_dir() or _has_reparse_or_symlink(root_resolved):
        return None
    candidate = root_resolved.joinpath(*ref_path.parts)
    current = root_resolved
    try:
        for part in ref_path.parts:
            current = current / part
            if not current.exists() or _has_reparse_or_symlink(current):
                return None
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None
    return resolved if resolved.is_file() else None


def measure_package_tree(
    package_root: str | Path,
) -> tuple[dict[str, str], str, tuple[str, ...]]:
    """Measure the complete installed package tree, rejecting link indirection."""

    root = Path(package_root)
    errors: list[str] = []
    try:
        resolved_root = root.resolve(strict=True)
    except OSError:
        return {}, "", ("installed_package_root_missing",)
    if not resolved_root.is_dir() or _has_reparse_or_symlink(resolved_root):
        return {}, "", ("installed_package_root_unsafe",)
    inventory: dict[str, str] = {}
    try:
        candidates = sorted(
            (
                path
                for path in resolved_root.rglob("*")
                if path.is_file()
                and "__pycache__" not in path.parts
                and path.suffix.lower() not in {".pyc", ".pyo"}
            ),
            key=lambda item: item.as_posix(),
        )
    except OSError:
        return {}, "", ("installed_package_enumeration_failed",)
    for source in candidates:
        try:
            relative = source.relative_to(resolved_root).as_posix()
        except ValueError:
            errors.append("installed_package_path_escape")
            continue
        current = resolved_root
        unsafe = False
        for part in Path(relative).parts:
            current = current / part
            if _has_reparse_or_symlink(current):
                unsafe = True
                break
        if unsafe:
            errors.append(f"installed_package_link_forbidden:{relative}")
            continue
        try:
            inventory[relative] = file_sha256(source)
        except OSError:
            errors.append(f"installed_package_read_failed:{relative}")
    digest = mapping_sha256(inventory) if inventory and not errors else ""
    if not inventory:
        errors.append("installed_package_inventory_empty")
    return inventory, digest, tuple(dict.fromkeys(errors))


def canonical_artifact_projection(
    artifacts: dict[str, Any] | None,
    *,
    expected_pair: str = "",
) -> dict[str, dict[str, Any]]:
    raw_artifacts = dict(artifacts or {})
    aliases_to_skip = {
        "swing" if "swing_xgb" in raw_artifacts else "",
        "intraday" if "intraday_xgb" in raw_artifacts else "",
    }
    projection: dict[str, dict[str, Any]] = {}
    for raw_key, raw_value in sorted(raw_artifacts.items()):
        key = str(raw_key).strip()
        if not key or key in aliases_to_skip:
            continue
        ref = (
            dict(raw_value or {})
            if isinstance(raw_value, dict)
            else {"path": str(raw_value or "")}
        )
        component_key = str(ref.get("component_key") or key).strip()
        if (
            component_key in {"swing", "intraday"}
            and f"{component_key}_xgb" in raw_artifacts
        ):
            component_key = f"{component_key}_xgb"
        canonical = {
            "component_key": component_key,
            "pair": str(ref.get("pair") or expected_pair or "").strip().upper(),
            "timeframe": str(ref.get("timeframe") or "").strip().upper(),
            "model_family": str(ref.get("model_family") or "").strip(),
            "model_name": str(ref.get("model_name") or "").strip(),
            "model_version": str(ref.get("model_version") or "").strip(),
            "run_id": str(ref.get("run_id") or "").strip(),
            "bundle_run_id": str(ref.get("bundle_run_id") or "").strip(),
            "dataset_fingerprint": str(
                ref.get("dataset_fingerprint") or ""
            ).strip(),
            "artifact_digest": str(
                ref.get("artifact_hash")
                or ref.get("content_sha256")
                or ref.get("payload_sha256")
                or ref.get("sha256")
                or ""
            ).strip().lower(),
            "runtime_compatible": bool(ref.get("runtime_compatible", True)),
        }
        existing = projection.get(component_key)
        if existing is not None and existing != canonical:
            projection[f"__conflicting_alias__:{key}"] = canonical
        else:
            projection[component_key] = canonical
    return projection


def artifact_set_sha256(
    artifacts: dict[str, Any] | None,
    *,
    expected_pair: str = "",
) -> str:
    projection = canonical_artifact_projection(
        artifacts,
        expected_pair=expected_pair,
    )
    expected_pair_key = str(expected_pair or "").strip().upper()
    if any(
        str(key).startswith("__conflicting_alias__:")
        or not str(ref.get("component_key") or "").strip()
        or not str(ref.get("pair") or "").strip()
        or (
            expected_pair_key
            and str(ref.get("pair") or "").strip().upper()
            != expected_pair_key
        )
        or not is_sha256(ref.get("artifact_digest"))
        for key, ref in projection.items()
    ):
        return ""
    return mapping_sha256(projection) if projection else ""


def canonical_model_identity_sha256(
    *,
    pair: str,
    bundle_run_id: str,
    model_set_id: str,
    artifacts: dict[str, Any] | None,
) -> str:
    artifact_digest = artifact_set_sha256(artifacts, expected_pair=pair)
    if not artifact_digest:
        return ""
    return mapping_sha256(
        {
            "pair": str(pair).strip().upper(),
            "bundle_run_id": str(bundle_run_id).strip(),
            "model_set_id": str(model_set_id).strip(),
            "artifact_set_sha256": artifact_digest,
        }
    )


@dataclass(frozen=True, slots=True)
class ManifestIdentity:
    pair: str
    bundle_run_id: str
    model_set_id: str
    model_manifest_sha256: str
    artifact_set_sha256: str


def active_manifest_identity(
    *,
    manifest_path: str | Path,
    pair: str,
) -> ManifestIdentity:
    payload = read_json_object(manifest_path)
    pair_key = str(pair).strip().upper()
    row = dict(dict(payload.get("active_model_sets") or {}).get(pair_key) or {})
    model_set_id = str(row.get("model_set_id") or "").strip()
    metadata = dict(row.get("metadata") or {})
    bundle_run_id = str(
        metadata.get("bundle_run_id") or model_set_id
    ).strip()
    artifacts = dict(row.get("artifacts") or {})
    return ManifestIdentity(
        pair=pair_key,
        bundle_run_id=bundle_run_id,
        model_set_id=model_set_id,
        model_manifest_sha256=canonical_model_identity_sha256(
            pair=pair_key,
            bundle_run_id=bundle_run_id,
            model_set_id=model_set_id,
            artifacts=artifacts,
        ),
        artifact_set_sha256=artifact_set_sha256(
            artifacts,
            expected_pair=pair_key,
        ),
    )


@dataclass(frozen=True, slots=True)
class Phase5BundleValidation:
    valid: bool
    errors: tuple[str, ...]
    gate_errors: dict[str, tuple[str, ...]]
    gate_passes: dict[str, bool]
    model_manifest_sha256: str


def validate_phase5_gate_bundle(
    payload: dict[str, Any] | None,
    *,
    expected_pair: str,
    expected_bundle_run_id: str,
    evidence_root: str | Path | None = None,
) -> Phase5BundleValidation:
    bundle = dict(payload or {})
    errors: list[str] = []
    gate_errors: dict[str, tuple[str, ...]] = {}
    if str(bundle.get("bundle_version") or "") != PHASE5_BUNDLE_SCHEMA:
        errors.append("phase5_bundle_schema_invalid")
    identity = dict(bundle.get("evidence_identity") or {})
    if str(identity.get("schema_version") or "") != EVIDENCE_IDENTITY_SCHEMA:
        errors.append("phase5_bundle_identity_schema_invalid")
    pair = str(identity.get("pair") or "").strip().upper()
    bundle_run_id = str(identity.get("bundle_run_id") or "").strip()
    model_set_id = str(identity.get("model_set_id") or "").strip()
    manifest_identity_sha = str(
        identity.get("model_manifest_sha256") or ""
    ).strip().lower()
    artifact_sha = str(identity.get("artifact_set_sha256") or "").strip().lower()
    if pair != str(expected_pair).strip().upper():
        errors.append("phase5_bundle_pair_mismatch")
    if bundle_run_id != str(expected_bundle_run_id).strip():
        errors.append("phase5_bundle_run_id_mismatch")
    if not model_set_id:
        errors.append("phase5_bundle_model_set_id_missing")
    if not is_sha256(manifest_identity_sha):
        errors.append("phase5_bundle_manifest_sha256_invalid")
    if not is_sha256(artifact_sha):
        errors.append("phase5_bundle_artifact_set_sha256_invalid")

    refs = {
        str(key): str(value or "").strip()
        for key, value in dict(bundle.get("evidence_refs") or {}).items()
    }
    hashes = {
        str(key): str(value or "").strip().lower()
        for key, value in dict(bundle.get("evidence_hashes") or {}).items()
    }
    required = {
        str(item).strip()
        for item in list(bundle.get("binding_required_evidence") or [])
        if str(item).strip()
    }
    missing_finalization = sorted(_REQUIRED_FINALIZATION - required)
    if missing_finalization:
        errors.append(
            "release_finalization_contract_missing:"
            + ",".join(missing_finalization)
        )
    for key in sorted(required | {"model_manifest"}):
        ref_text = refs.get(key, "")
        expected_hash = hashes.get(key, "")
        source = (
            resolve_contained_evidence_ref(
                evidence_root=evidence_root,
                reference=ref_text,
            )
            if evidence_root is not None
            else (Path(ref_text) if ref_text else None)
        )
        if source is None or not source.is_file():
            errors.append(f"phase5_evidence_missing:{key}")
            continue
        if not is_sha256(expected_hash) or file_sha256(source) != expected_hash:
            errors.append(f"phase5_evidence_hash_mismatch:{key}")

    manifest_path = (
        resolve_contained_evidence_ref(
            evidence_root=evidence_root,
            reference=refs.get("model_manifest", ""),
        )
        if evidence_root is not None
        else Path(refs.get("model_manifest", ""))
    )
    if manifest_path is not None and manifest_path.is_file():
        active = active_manifest_identity(
            manifest_path=manifest_path,
            pair=expected_pair,
        )
        if active.bundle_run_id != bundle_run_id:
            errors.append("phase5_bundle_active_bundle_mismatch")
        if active.model_set_id != model_set_id:
            errors.append("phase5_bundle_active_model_set_mismatch")
        if active.model_manifest_sha256 != manifest_identity_sha:
            errors.append("phase5_bundle_model_identity_mismatch")
        if active.artifact_set_sha256 != artifact_sha:
            errors.append("phase5_bundle_active_artifact_set_mismatch")
        manifest_payload = read_json_object(manifest_path)
        active_row = dict(
            dict(manifest_payload.get("active_model_sets") or {}).get(pair) or {}
        )
        metadata = dict(active_row.get("metadata") or {})
        promotion_components = {
            str(key).strip(): str(value or "").strip().lower()
            for key, value in dict(metadata.get("promotion_components") or {}).items()
            if str(key).strip()
        }
        for component in sorted(_REQUIRED_PROMOTION_COMPONENTS):
            if promotion_components.get(component) != "eligible":
                errors.append(
                    f"phase5_bundle_promotion_component_not_eligible:{component}"
                )

    for gate_name in _AUTHORITY_GATES:
        gate = dict(bundle.get(gate_name) or {})
        gate_specific: list[str] = []
        if str(gate.get("gate") or "") != gate_name:
            gate_specific.append("gate_identity_invalid")
        if gate.get("passed") is not True:
            gate_specific.append("not_passed")
        if gate_specific:
            gate_errors[gate_name] = tuple(gate_specific)
    global_valid = not errors
    gate_passes = {
        gate_name: bool(global_valid and gate_name not in gate_errors)
        for gate_name in _AUTHORITY_GATES
    }
    gate_passes["canary_closeout"] = False
    return Phase5BundleValidation(
        valid=global_valid and not gate_errors,
        errors=tuple(dict.fromkeys(errors)),
        gate_errors=gate_errors,
        gate_passes=gate_passes,
        model_manifest_sha256=manifest_identity_sha,
    )
