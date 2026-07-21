"""Installed-package preflight for the production Windows payload."""

from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path
import shutil
import sys

from fxstack.runtime.db_tools import load_migration_heads
from fxstack.runtime.startup_preflight import runtime_physical_isolation_errors
from fxstack.settings import get_settings


def run_preflight(*, allow_sqlite: bool = False) -> dict[str, object]:
    settings = get_settings()
    package_mode = str(os.environ.get("FXSTACK_PACKAGE_MODE", "")).strip().lower() not in {
        "",
        "0",
        "false",
        "no",
    }
    checks: list[dict[str, object]] = []

    def _push(name: str, ok: bool, detail: str = "") -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    config_errors = list(settings.validate_for_startup())
    raw_bridge_port = str(os.environ.get("TRADER_BRIDGE_PORT", "") or "").strip()
    if raw_bridge_port:
        try:
            parsed_bridge_port = int(raw_bridge_port)
            if not 1 <= parsed_bridge_port <= 65535:
                raise ValueError
        except ValueError:
            config_errors.append("TRADER_BRIDGE_PORT must be an integer from 1 to 65535")
    _push("settings_validate_for_startup", not config_errors, "; ".join(config_errors))
    _push("runtime_physical_isolation", not runtime_physical_isolation_errors())
    _push("python_executable", bool(sys.executable), sys.executable)
    _push("uv_available_optional", True, str(shutil.which("uv") or "not-required"))
    node_path = str(os.environ.get("NODE_EXE") or shutil.which("node") or "").strip()
    _push("node_available", bool(node_path), node_path)
    _push(
        "pnpm_available",
        package_mode or shutil.which("pnpm") is not None,
        "not-required-in-package-mode" if package_mode else str(shutil.which("pnpm") or ""),
    )
    provider = str(settings.normalized_data_provider)
    _push(
        "data_provider_supported",
        provider in {"dukascopy", "mt4_bridge"},
        provider,
    )
    source_root = Path(str(settings.dukascopy_source_root).strip()).expanduser()
    _push("dukascopy_source_root_exists", source_root.exists(), str(source_root))
    _push(
        "dukascopy_file_pattern_set",
        bool(str(settings.dukascopy_file_pattern).strip()),
        str(settings.dukascopy_file_pattern),
    )
    try:
        migration_root, _ = load_migration_heads()
    except Exception as exc:
        _push("migration_resources", False, f"{type(exc).__name__}: {exc}")
    else:
        _push("migration_resources", True, str(migration_root))
    _push("database_url_set", bool(str(settings.database_url).strip()), str(settings.database_url))
    sqlite_allowed = bool(allow_sqlite or settings.allow_sqlite)
    _push(
        "sqlite_block",
        not str(settings.database_url).lower().startswith("sqlite") or sqlite_allowed,
        "" if sqlite_allowed else "sqlite requires explicit allow_sqlite",
    )

    required_modules = [
        "fastapi",
        "uvicorn",
        "sqlalchemy",
        "pydantic",
        "pydantic_settings",
        "requests",
        "xgboost",
        "hmmlearn",
        "dukascopy_python",
    ]
    require_deep_stack = (
        bool(settings.require_cuda)
        or str(settings.swing_model_policy).strip().lower()
        == "transformer_primary_xgb_fallback"
        or str(settings.intraday_model_policy).strip().lower()
        == "tcn_primary_xgb_fallback"
    )
    if require_deep_stack:
        required_modules.extend(["torch", "transformers", "pytorch_tcn"])
    for module_name in required_modules:
        _push(
            f"module:{module_name}",
            importlib.util.find_spec(module_name) is not None,
        )

    cuda_ok = True
    cuda_detail = "not-required"
    if bool(settings.require_cuda):
        try:
            import torch

            cuda_ok = bool(torch.cuda.is_available())
            cuda_detail = f"required=1,available={int(cuda_ok)}"
        except Exception as exc:
            cuda_ok = False
            cuda_detail = f"torch_import_error:{type(exc).__name__}: {exc}"
    _push("cuda_available", cuda_ok, cuda_detail)

    ok = all(bool(item.get("ok")) for item in checks)
    return {
        "ok": ok,
        "checks": checks,
        "settings": settings.to_public_dict(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate the installed production payload.")
    parser.add_argument("--allow-sqlite", action="store_true")
    args = parser.parse_args()
    result = run_preflight(allow_sqlite=bool(args.allow_sqlite))
    print(result)
    raise SystemExit(0 if bool(result.get("ok")) else 2)


if __name__ == "__main__":
    main()
