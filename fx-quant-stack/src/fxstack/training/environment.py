"""External training-host environment diagnostics."""

from __future__ import annotations

from typing import Any


def stack_preflight(*, allow_sqlite: bool = False) -> dict[str, Any]:
    """Validate an external build/training host without starting services."""
    import importlib.util
    import os
    import shutil
    import sys
    from pathlib import Path

    from fxstack.settings import get_settings

    settings = get_settings()
    sqlite_allowed = bool(allow_sqlite or settings.allow_sqlite)
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
            bridge_port = int(raw_bridge_port)
            if not 1 <= bridge_port <= 65535:
                raise ValueError
        except ValueError:
            config_errors.append("TRADER_BRIDGE_PORT must be an integer from 1 to 65535")
    _push("settings_validate_for_startup", not config_errors, "; ".join(config_errors))
    _push("python_executable", bool(sys.executable), sys.executable)
    uv_path = shutil.which("uv")
    _push("uv_available_optional", True, str(uv_path) if uv_path else "missing; using pip/venv fallback")
    node_path = str(os.environ.get("NODE_EXE") or shutil.which("node") or "").strip()
    _push("node_available", bool(node_path), node_path)
    _push(
        "pnpm_available",
        package_mode or shutil.which("pnpm") is not None,
        "not-required-in-package-mode" if package_mode else str(shutil.which("pnpm") or ""),
    )
    provider = str(settings.normalized_data_provider)
    _push("data_provider_supported", provider in {"dukascopy", "mt4_bridge"}, provider)
    source_root = Path(str(settings.dukascopy_source_root).strip()).expanduser()
    _push("dukascopy_source_root_exists", source_root.exists(), str(source_root))
    _push(
        "dukascopy_file_pattern_set",
        bool(str(settings.dukascopy_file_pattern).strip()),
        str(settings.dukascopy_file_pattern),
    )
    _push("database_url_set", bool(str(settings.database_url).strip()), str(settings.database_url))
    sqlite_selected = str(settings.database_url).lower().startswith("sqlite")
    _push(
        "sqlite_block",
        not sqlite_selected or sqlite_allowed,
        "FXSTACK_DATABASE_URL points to sqlite and allow_sqlite is false"
        if sqlite_selected and not sqlite_allowed
        else "",
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
    swing_policy = str(getattr(settings, "swing_model_policy", "") or "").strip().lower()
    intraday_policy = str(getattr(settings, "intraday_model_policy", "") or "").strip().lower()
    require_deep_stack = (
        bool(settings.require_cuda)
        or swing_policy == "transformer_primary_xgb_fallback"
        or intraday_policy == "tcn_primary_xgb_fallback"
        or bool(getattr(settings, "sequence_shadow_enabled", False))
    )
    if require_deep_stack:
        required_modules.extend(["torch", "transformers", "pytorch_tcn"])
    for module_name in required_modules:
        _push(f"module:{module_name}", importlib.util.find_spec(module_name) is not None)

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

    return {
        "ok": all(bool(item.get("ok")) for item in checks),
        "checks": checks,
        "settings": settings.to_public_dict(),
    }


def gpu_diagnostics() -> dict[str, Any]:
    """Return CUDA capability without importing torch until explicitly called."""
    from fxstack.settings import get_settings

    settings = get_settings()
    try:
        import torch
    except Exception as exc:
        return {"ok": False, "error": f"torch_import_error:{type(exc).__name__}: {exc}"}

    available = bool(torch.cuda.is_available())
    device_count = int(torch.cuda.device_count() if available else 0)
    return {
        "ok": available or not bool(settings.require_cuda),
        "require_cuda": bool(settings.require_cuda),
        "cuda_available": available,
        "cuda_device_count": device_count,
        "cuda_devices": [str(torch.cuda.get_device_name(index)) for index in range(device_count)],
    }
