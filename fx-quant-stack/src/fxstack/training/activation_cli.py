"""Shared external activation entrypoint used by focused and compatibility CLIs."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence


def activate_models(
    *,
    database_url: str = "",
    registry_root: str = "",
    manifest: str = "",
    registry_file: str = "",
    pairs: Sequence[str] = (),
    source: str = "compat",
    alias: str = "champion",
    require_all: bool = False,
) -> tuple[dict[str, Any], int]:
    """Activate validated model bundles and return the report plus process code."""
    from fxstack.settings import get_settings
    from fxstack.training.activation import activate_mlflow_alias, activate_pairs, activate_registry_file

    settings = get_settings()
    resolved_database_url = str(database_url or settings.database_url)
    resolved_registry_root = Path(str(registry_root or settings.registry_root))
    manifest_path = Path(str(manifest or settings.model_activation_manifest))
    resolved_pairs = [str(pair).upper() for pair in pairs] or list(settings.pairs)
    resolved_source = str(source or "compat").strip().lower()
    resolved_alias = str(alias or "champion").strip().lower()
    if resolved_source not in {"compat", "mlflow"}:
        raise ValueError(f"unsupported activation source: {resolved_source}")
    if resolved_alias not in {"champion", "shadow"}:
        raise ValueError(f"unsupported MLflow alias: {resolved_alias}")

    activated: list[dict[str, Any]] = []
    if resolved_source == "mlflow":
        activated = activate_mlflow_alias(
            database_url=resolved_database_url,
            manifest_path=manifest_path,
            pairs=resolved_pairs,
            alias=resolved_alias,
            default_session_id=settings.default_session_id,
            command_ttl_secs=settings.command_ttl_secs,
        )
    elif registry_file:
        activated.append(
            activate_registry_file(
                database_url=resolved_database_url,
                registry_file=Path(str(registry_file)),
                manifest_path=manifest_path,
                default_session_id=settings.default_session_id,
                command_ttl_secs=settings.command_ttl_secs,
                enabled=True,
            )
        )
    else:
        activated = activate_pairs(
            database_url=resolved_database_url,
            registry_root=resolved_registry_root,
            manifest_path=manifest_path,
            pairs=resolved_pairs,
            default_session_id=settings.default_session_id,
            command_ttl_secs=settings.command_ttl_secs,
        )

    activated_pairs = {str(item.get("pair", "")).upper() for item in activated}
    missing_pairs = [pair for pair in resolved_pairs if pair not in activated_pairs]
    report = {
        "database_url": resolved_database_url,
        "registry_root": str(resolved_registry_root),
        "manifest": str(manifest_path),
        "source": resolved_source,
        "alias": resolved_alias if resolved_source == "mlflow" else "",
        "activated_count": len(activated),
        "activated_pairs": sorted(activated_pairs),
        "missing_pairs": missing_pairs,
    }
    return report, 1 if require_all and missing_pairs else 0
