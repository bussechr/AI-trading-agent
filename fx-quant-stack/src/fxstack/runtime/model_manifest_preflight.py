"""Read-only active-model validation for guarded runtime startup.

This module deliberately has no runtime-service or settings dependency. It
validates the configured activation manifest, its local registry provenance,
and every configured local artifact before either the guarded launcher or the
Python runner is allowed to reach runtime state.
"""

# AGENT: ROLE: Read-only validation of the production model trust boundary before spawn or direct runtime access.
# AGENT: CALLED BY: `tools/preflight_active_models.py` and `fxstack.runtime.startup_preflight`.
# AGENT: SIDE EFFECTS: None; no locks, downloads, database access, deserialization, or writes.
# AGENT: SEE: `docs/agents/model-stack-and-feature-flow.md` and `docs/agents/ops-entrypoints.md`.

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Sequence

from fxstack.features.session_contract import (
    feature_contract_metadata,
    feature_contract_mismatches,
)
from fxstack.models.artifact_contract import validate_artifact_contract_read_only


ACTIVE_MODEL_MANIFEST_SCHEMA_VERSION = 1

_ARTIFACT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("regime", ("regime",)),
    ("meta", ("meta",)),
    ("swing_transformer", ("swing_transformer",)),
    ("swing_xgb", ("swing_xgb", "swing")),
    ("intraday_tcn", ("intraday_tcn",)),
    ("intraday_xgb", ("intraday_xgb", "intraday")),
    ("exit_policy", ("exit_policy", "exit", "exit_model")),
    ("directional_belief", ("directional_belief",)),
    ("reversal_failure", ("reversal_failure", "reversal_failure_xgb")),
    ("reversal_opportunity", ("reversal_opportunity", "reversal_opportunity_xgb")),
)


class ModelManifestPreflightError(RuntimeError):
    """The active manifest is not safe to hand to a runtime process."""


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ModelManifestPreflightError(
            f"{label}_invalid_json:{path}:{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ModelManifestPreflightError(f"{label}_not_object:{path}")
    return dict(payload)


def _read_object_with_sha256(path: Path, *, label: str) -> tuple[dict[str, Any], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ModelManifestPreflightError(
            f"{label}_invalid_json:{path}:{type(exc).__name__}"
        ) from exc
    if not isinstance(payload, dict):
        raise ModelManifestPreflightError(f"{label}_not_object:{path}")
    return dict(payload), hashlib.sha256(raw).hexdigest()


def _normalize_pairs(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        pair = str(raw or "").strip().upper()
        if not pair or pair in seen:
            continue
        if re.fullmatch(r"[A-Z]{6}", pair) is None:
            raise ModelManifestPreflightError(f"configured_pair_invalid:{pair or '<empty>'}")
        seen.add(pair)
        out.append(pair)
    return out


def _ref_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or value.get("model_uri") or "").strip()
    return str(value or "").strip()


def _local_ref_path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or "").strip()
    return str(value or "").strip()


def _expected_artifact_digest(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    return str(value.get("artifact_hash") or "").strip().lower()


def _resolve_local_path(raw: str, *, project_root: Path, label: str) -> Path:
    text = str(raw or "").strip()
    if not text:
        raise ModelManifestPreflightError(f"{label}_path_missing")
    candidate = Path(text).expanduser()
    candidates = [candidate] if candidate.is_absolute() else [project_root / candidate]
    if (
        not candidate.is_absolute()
        and candidate.parts
        and candidate.parts[0].lower() == project_root.name.lower()
    ):
        candidates.append(project_root.parent / candidate)
    for item in candidates:
        if item.exists():
            absolute = item.absolute()
            if absolute.is_symlink():
                raise ModelManifestPreflightError(
                    f"{label}_symlink_rejected:{absolute}"
                )
            return absolute
    raise ModelManifestPreflightError(f"{label}_unresolved:{text}")


def _first_ref(artifacts: dict[str, Any], keys: Iterable[str]) -> Any | None:
    for key in keys:
        value = artifacts.get(str(key))
        if _ref_path(value):
            return value
    return None


def _contract_error(payload: dict[str, Any], *, label: str) -> str:
    mismatches = feature_contract_mismatches(payload)
    if not mismatches:
        return ""
    detail = ",".join(
        f"{key}=expected:{expected}|actual:{actual or '<missing>'}"
        for key, (expected, actual) in sorted(mismatches.items())
    )
    return f"feature_contract_mismatch:{label}:{detail}; retraining is required"


def _validate_registry_provenance(
    *,
    pair: str,
    model_set_id: str,
    registry_path: str,
    project_root: Path,
) -> None:
    if re.match(r"^[a-z][a-z0-9+.-]*://", registry_path, flags=re.IGNORECASE):
        return
    path = _resolve_local_path(
        registry_path,
        project_root=project_root,
        label=f"registry:{pair}",
    )
    payload = _read_object(path, label=f"registry:{pair}")
    registry_pair = str(payload.get("pair") or "").strip().upper()
    if registry_pair != pair:
        raise ModelManifestPreflightError(
            f"registry_pair_mismatch:{pair}:actual:{registry_pair or '<missing>'}"
        )
    registry_id = str(payload.get("model_set_id") or payload.get("run_id") or "").strip()
    if registry_id != model_set_id:
        raise ModelManifestPreflightError(
            f"registry_model_set_mismatch:{pair}:"
            f"expected:{model_set_id}|actual:{registry_id or '<missing>'}"
        )
    contract_error = _contract_error(
        dict(payload.get("feature_schema") or {}),
        label=f"registry:{pair}",
    )
    if contract_error:
        raise ModelManifestPreflightError(contract_error)


def _validate_portfolio_rl_ref(
    value: Any,
    *,
    pair: str,
    project_root: Path,
) -> None:
    if value is None or not _ref_path(value):
        return
    local_path = _local_ref_path(value)
    if not local_path:
        raise ModelManifestPreflightError(
            f"portfolio_rl_local_path_required:{pair}:{_ref_path(value)}"
        )
    expected = str(value.get("content_sha256") or "").strip().lower() if isinstance(value, dict) else ""
    if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
        raise ModelManifestPreflightError(f"portfolio_rl_content_sha256_invalid:{pair}")
    path = _resolve_local_path(
        local_path,
        project_root=project_root,
        label=f"portfolio_rl:{pair}",
    )
    if not path.is_file() or path.is_symlink():
        raise ModelManifestPreflightError(f"portfolio_rl_file_invalid:{pair}:{path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != expected:
        raise ModelManifestPreflightError(
            f"portfolio_rl_content_sha256_mismatch:{pair}:"
            f"expected:{expected}|actual:{actual}"
        )


def _portfolio_rl_ref(
    artifacts: dict[str, Any],
    metadata: dict[str, Any],
) -> Any | None:
    for key in ("portfolio_rl", "rl_policy", "rl_checkpoint", "offline_rl"):
        value = artifacts.get(key)
        if _ref_path(value):
            if isinstance(value, dict) and not str(value.get("content_sha256") or "").strip():
                return {
                    **value,
                    "content_sha256": str(value.get("checkpoint_content_sha256") or ""),
                }
            return value
    value = metadata.get("rl_checkpoint")
    if _ref_path(value):
        if isinstance(value, dict) and not str(value.get("content_sha256") or "").strip():
            return {
                **value,
                "content_sha256": str(value.get("checkpoint_content_sha256") or ""),
            }
        return value
    legacy_path = str(metadata.get("rl_checkpoint_path") or "").strip()
    if not legacy_path:
        return None
    return {
        "path": legacy_path,
        "content_sha256": str(metadata.get("rl_checkpoint_content_sha256") or ""),
    }


def _validate_pair(
    *,
    pair: str,
    row: dict[str, Any],
    project_root: Path,
) -> int:
    if not bool(row.get("enabled", True)):
        raise ModelManifestPreflightError(f"active_model_set_disabled:{pair}")
    model_set_id = str(row.get("model_set_id") or "").strip()
    if not model_set_id:
        raise ModelManifestPreflightError(f"model_set_id_missing:{pair}")
    registry_path = str(row.get("registry_path") or "").strip()
    if not registry_path:
        raise ModelManifestPreflightError(f"registry_path_missing:{pair}")

    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        raise ModelManifestPreflightError(f"model_metadata_missing:{pair}")
    metadata_pair = str(metadata.get("pair") or "").strip().upper()
    if metadata_pair and metadata_pair != pair:
        raise ModelManifestPreflightError(
            f"model_metadata_pair_mismatch:{pair}:actual:{metadata_pair}"
        )
    feature_schema = metadata.get("feature_schema")
    if not isinstance(feature_schema, dict):
        raise ModelManifestPreflightError(f"feature_schema_missing:manifest:{pair}")
    contract_error = _contract_error(feature_schema, label=f"manifest:{pair}")
    if contract_error:
        raise ModelManifestPreflightError(contract_error)
    promotion_status = str(metadata.get("promotion_status") or "").strip().lower()
    if promotion_status != "eligible":
        raise ModelManifestPreflightError(
            f"promotion_status_not_eligible:{pair}:actual:{promotion_status or '<missing>'}"
        )

    artifacts = row.get("artifacts")
    if not isinstance(artifacts, dict):
        raise ModelManifestPreflightError(f"artifacts_missing:{pair}")
    for component, keys in (
        ("regime", ("regime",)),
        ("meta", ("meta",)),
        ("swing", ("swing_transformer", "swing_xgb", "swing")),
        ("intraday", ("intraday_tcn", "intraday_xgb", "intraday")),
    ):
        if _first_ref(artifacts, keys) is None:
            raise ModelManifestPreflightError(f"required_artifact_missing:{pair}:{component}")

    _validate_registry_provenance(
        pair=pair,
        model_set_id=model_set_id,
        registry_path=registry_path,
        project_root=project_root,
    )

    validated_paths: set[str] = set()
    validated_count = 0
    for component, keys in _ARTIFACT_GROUPS:
        value = _first_ref(artifacts, keys)
        if value is None:
            continue
        if isinstance(value, dict) and value.get("runtime_compatible") is False:
            raise ModelManifestPreflightError(
                f"artifact_runtime_incompatible:{pair}:{component}"
            )
        local_path = _local_ref_path(value)
        if not local_path:
            raise ModelManifestPreflightError(
                f"artifact_local_path_required:{pair}:{component}:{_ref_path(value)}"
            )
        resolved = _resolve_local_path(
            local_path,
            project_root=project_root,
            label=f"artifact:{pair}:{component}",
        )
        identity = str(resolved).casefold()
        if identity in validated_paths:
            continue
        try:
            if component == "directional_belief":
                from fxstack.belief.engine import (
                    validate_directional_belief_artifact_contract_read_only,
                )

                meta = validate_directional_belief_artifact_contract_read_only(
                    resolved,
                    expected_contract=str(feature_schema.get("belief_contract") or "").strip() or None,
                    expected_digest=_expected_artifact_digest(value),
                )
            else:
                meta = validate_artifact_contract_read_only(
                    resolved,
                    label=f"preflight:{pair}:{component}",
                    expected_digest=_expected_artifact_digest(value),
                )
        except Exception as exc:
            raise ModelManifestPreflightError(
                f"artifact_invalid:{pair}:{component}:{type(exc).__name__}:{exc}"
            ) from exc
        artifact_pair = str(meta.get("pair") or "").strip().upper()
        pair_matches = artifact_pair == pair or (
            component == "directional_belief" and artifact_pair == "GLOBAL"
        )
        if artifact_pair and not pair_matches:
            raise ModelManifestPreflightError(
                f"artifact_pair_mismatch:{pair}:{component}:actual:{artifact_pair}"
            )
        validated_paths.add(identity)
        validated_count += 1

    _validate_portfolio_rl_ref(
        _portfolio_rl_ref(artifacts, metadata),
        pair=pair,
        project_root=project_root,
    )
    return validated_count


def preflight_active_model_manifest(
    *,
    manifest_path: Path,
    project_root: Path,
    required_pairs: Iterable[str] = (),
) -> dict[str, Any]:
    """Fail closed unless the active manifest is locally runtime-compatible."""

    root = Path(project_root).expanduser().resolve()
    manifest = Path(manifest_path).expanduser()
    if not manifest.is_absolute():
        manifest = root / manifest
    if manifest.is_symlink():
        raise ModelManifestPreflightError(
            f"active_manifest_symlink_rejected:{manifest.absolute()}"
        )
    if not manifest.is_file():
        raise ModelManifestPreflightError(f"active_manifest_missing:{manifest}")
    payload, manifest_content_sha256 = _read_object_with_sha256(
        manifest,
        label="active_manifest",
    )
    version = payload.get("schema_version")
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != ACTIVE_MODEL_MANIFEST_SCHEMA_VERSION
    ):
        raise ModelManifestPreflightError(
            "active_manifest_schema_mismatch:"
            f"expected:{ACTIVE_MODEL_MANIFEST_SCHEMA_VERSION}|actual:{version!r}"
        )
    if bool(payload.get("research_only", False)):
        raise ModelManifestPreflightError("research_manifest_rejected_for_runtime")
    active = payload.get("active_model_sets")
    if not isinstance(active, dict) or not active:
        raise ModelManifestPreflightError("active_manifest_empty")

    normalized_active: dict[str, dict[str, Any]] = {}
    for raw_pair, raw_row in active.items():
        pair = str(raw_pair or "").strip().upper()
        if re.fullmatch(r"[A-Z]{6}", pair) is None:
            raise ModelManifestPreflightError(
                f"active_manifest_pair_invalid:{pair or '<empty>'}"
            )
        if pair in normalized_active:
            raise ModelManifestPreflightError(f"active_manifest_pair_duplicate:{pair}")
        if not isinstance(raw_row, dict):
            raise ModelManifestPreflightError(f"active_model_set_not_object:{pair}")
        normalized_active[pair] = dict(raw_row)

    requested = _normalize_pairs(required_pairs)
    target_pairs = requested or sorted(
        pair
        for pair, row in normalized_active.items()
        if bool(row.get("enabled", True))
    )
    if not target_pairs:
        raise ModelManifestPreflightError("active_manifest_has_no_enabled_pairs")
    missing = [pair for pair in target_pairs if pair not in normalized_active]
    if missing:
        raise ModelManifestPreflightError(
            f"active_manifest_missing_pairs:{','.join(sorted(missing))}"
        )

    errors: list[str] = []
    artifact_count = 0
    validated_pairs: list[str] = []
    for pair in target_pairs:
        try:
            artifact_count += _validate_pair(
                pair=pair,
                row=normalized_active[pair],
                project_root=root,
            )
            validated_pairs.append(pair)
        except ModelManifestPreflightError as exc:
            errors.append(str(exc))
        except Exception as exc:
            errors.append(
                f"preflight_internal_error:{pair}:{type(exc).__name__}:{exc}"
            )
    if errors:
        raise ModelManifestPreflightError(
            f"active_model_preflight_failed:{' ; '.join(errors)}"
        )

    return {
        "ok": True,
        "read_only": True,
        "manifest_path": str(manifest.resolve()),
        "manifest_content_sha256": str(manifest_content_sha256),
        "required_pairs": list(target_pairs),
        "validated_pairs": validated_pairs,
        "validated_artifacts": int(artifact_count),
        "feature_contract": feature_contract_metadata(),
    }


def _cli_pairs(raw: str) -> list[str]:
    return [
        value.strip()
        for value in str(raw or "").replace(";", ",").split(",")
        if value.strip()
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate the active manifest, current feature contract, registry provenance, "
            "and local artifact payloads without loading or activating models."
        )
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        required=True,
        help="Repository root used to resolve local artifact references.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Activation manifest to inspect; the file is never modified.",
    )
    parser.add_argument(
        "--pairs",
        default=os.environ.get("FXSTACK_PAIRS", ""),
        help="Comma-separated runtime pair scope. All enabled rows are checked when omitted.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = preflight_active_model_manifest(
            manifest_path=args.manifest,
            project_root=args.project_root,
            required_pairs=_cli_pairs(args.pairs),
        )
    except ModelManifestPreflightError as exc:
        print(f"[model-preflight] ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


__all__ = [
    "ACTIVE_MODEL_MANIFEST_SCHEMA_VERSION",
    "ModelManifestPreflightError",
    "build_parser",
    "main",
    "preflight_active_model_manifest",
]


if __name__ == "__main__":
    raise SystemExit(main())
