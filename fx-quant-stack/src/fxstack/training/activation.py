from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fxstack.runtime.service import RuntimeService


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid registry payload: {path}")
    return payload


def _artifact_path(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("path") or "")
    return ""


def _required_artifacts(policies: dict[str, str]) -> set[str]:
    out = {"regime", "meta"}
    swing_policy = str((policies or {}).get("swing", "transformer_primary_xgb_fallback")).strip().lower()
    intraday_policy = str((policies or {}).get("intraday", "tcn_primary_xgb_fallback")).strip().lower()

    if swing_policy == "transformer_primary_xgb_fallback":
        out.update({"swing_transformer", "swing_xgb"})
    else:
        out.add("swing_xgb")

    if intraday_policy == "tcn_primary_xgb_fallback":
        out.update({"intraday_tcn", "intraday_xgb"})
    else:
        out.add("intraday_xgb")
    return out


def parse_registry_entry(path: Path) -> dict[str, Any]:
    raw = _read_json(path)
    pair = str(raw.get("pair") or "").upper().strip()
    if not pair:
        raise ValueError(f"Registry file missing pair: {path}")

    model_set_id = str(raw.get("run_id") or path.stem)
    artifacts_raw = dict(raw.get("artifacts") or {})
    policies_raw = dict(raw.get("policies") or {})
    policies = {
        "swing": str(policies_raw.get("swing", "transformer_primary_xgb_fallback")),
        "intraday": str(policies_raw.get("intraday", "tcn_primary_xgb_fallback")),
    }
    artifacts = {
        "regime": _artifact_path(artifacts_raw.get("regime")),
        "meta": _artifact_path(artifacts_raw.get("meta")),
        "swing_transformer": _artifact_path(artifacts_raw.get("swing_transformer")),
        "swing_xgb": _artifact_path(artifacts_raw.get("swing_xgb")) or _artifact_path(artifacts_raw.get("swing")),
        "intraday_tcn": _artifact_path(artifacts_raw.get("intraday_tcn")),
        "intraday_xgb": _artifact_path(artifacts_raw.get("intraday_xgb")) or _artifact_path(artifacts_raw.get("intraday")),
    }
    # Compatibility aliases for loaders expecting generic keys.
    artifacts["swing"] = str(artifacts["swing_xgb"])
    artifacts["intraday"] = str(artifacts["intraday_xgb"])

    required = sorted(list(_required_artifacts(policies)))
    missing = [k for k in required if not str(artifacts.get(k, "")).strip()]
    if missing:
        raise ValueError(f"Registry file missing artifact paths ({','.join(missing)}): {path}")

    return {
        "pair": pair,
        "model_set_id": model_set_id,
        "registry_path": str(path),
        "artifacts": artifacts,
        "policies": policies,
        "metadata": raw,
    }


def latest_registry_for_pair(*, registry_root: Path, pair: str) -> Path | None:
    pair_u = str(pair).upper().strip()
    candidates: list[tuple[float, Path]] = []
    for p in sorted(registry_root.glob("*.json")):
        try:
            item = parse_registry_entry(p)
        except Exception:
            continue
        if str(item.get("pair", "")).upper() != pair_u:
            continue
        candidates.append((p.stat().st_mtime, p))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": 1, "active_model_sets": {}}
    try:
        payload = _read_json(path)
    except Exception:
        return {"schema_version": 1, "active_model_sets": {}}
    payload.setdefault("schema_version", 1)
    payload.setdefault("active_model_sets", {})
    if not isinstance(payload.get("active_model_sets"), dict):
        payload["active_model_sets"] = {}
    return payload


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def activate_registry_file(
    *,
    database_url: str,
    registry_file: Path,
    manifest_path: Path,
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
    enabled: bool = True,
) -> dict[str, Any]:
    item = parse_registry_entry(registry_file)
    svc = RuntimeService(
        database_url=database_url,
        default_session_id=default_session_id,
        command_ttl_secs=command_ttl_secs,
    )
    svc.upsert_active_model_set(
        pair=str(item["pair"]),
        model_set_id=str(item["model_set_id"]),
        registry_path=str(item["registry_path"]),
        artifacts=dict(item["artifacts"]),
        metadata=dict(item.get("metadata") or {}),
        enabled=bool(enabled),
    )

    manifest = load_manifest(manifest_path)
    active = dict(manifest.get("active_model_sets") or {})
    active[str(item["pair"])] = {
        "model_set_id": str(item["model_set_id"]),
        "registry_path": str(item["registry_path"]),
        "artifacts": dict(item["artifacts"]),
        "policies": dict(item.get("policies") or {}),
        "enabled": bool(enabled),
    }
    manifest["active_model_sets"] = active
    write_manifest(manifest_path, manifest)
    return item


def activate_pairs(
    *,
    database_url: str,
    registry_root: Path,
    manifest_path: Path,
    pairs: list[str],
    default_session_id: str = "default",
    command_ttl_secs: float = 120.0,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pair in pairs:
        path = latest_registry_for_pair(registry_root=registry_root, pair=pair)
        if path is None:
            continue
        item = activate_registry_file(
            database_url=database_url,
            registry_file=path,
            manifest_path=manifest_path,
            default_session_id=default_session_id,
            command_ttl_secs=command_ttl_secs,
            enabled=True,
        )
        out.append(item)
    return out
