from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class TraderConfig:
    runtime_db_path: str = "data/state/runtime.db"
    audit_dir: str = "data/state/audit"
    default_session_id: str = "default"
    command_ttl_secs: float = 120.0
    soft_dd_min: float = 0.06
    soft_dd_max: float = 0.09
    hard_dd_min: float = 0.10
    hard_dd_max: float = 0.12
    daily_breaker_min: float = 0.02
    daily_breaker_max: float = 0.03
    base_lot: float = 0.03
    min_lot: float = 0.01
    max_lot: float = 2.00

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "TraderConfig":
        d = dict(data or {})
        defaults = cls()
        return cls(
            runtime_db_path=str(
                d.get("runtime_db_path") or d.get("trader_runtime_db_path") or defaults.runtime_db_path
            ),
            audit_dir=str(d.get("audit_dir") or d.get("trader_audit_dir") or defaults.audit_dir),
            default_session_id=str(
                d.get("default_session_id") or d.get("trader_default_session_id") or defaults.default_session_id
            ),
            command_ttl_secs=float(
                d.get("command_ttl_secs") or d.get("trader_command_ttl_secs") or defaults.command_ttl_secs
            ),
            soft_dd_min=float(d.get("soft_dd_min") or d.get("trader_soft_dd_min") or defaults.soft_dd_min),
            soft_dd_max=float(d.get("soft_dd_max") or d.get("trader_soft_dd_max") or defaults.soft_dd_max),
            hard_dd_min=float(d.get("hard_dd_min") or d.get("trader_hard_dd_min") or defaults.hard_dd_min),
            hard_dd_max=float(d.get("hard_dd_max") or d.get("trader_hard_dd_max") or defaults.hard_dd_max),
            daily_breaker_min=float(
                d.get("daily_breaker_min") or d.get("trader_daily_breaker_min") or defaults.daily_breaker_min
            ),
            daily_breaker_max=float(
                d.get("daily_breaker_max") or d.get("trader_daily_breaker_max") or defaults.daily_breaker_max
            ),
            base_lot=float(d.get("base_lot") or d.get("trader_base_lot") or defaults.base_lot),
            min_lot=float(d.get("min_lot") or d.get("trader_min_lot") or defaults.min_lot),
            max_lot=float(d.get("max_lot") or d.get("trader_max_lot") or defaults.max_lot),
        )


def load_trader_config(config_path: str | None = None) -> TraderConfig:
    path = str(config_path or "src/config/fx_el_minis.yaml").strip()
    payload: dict[str, Any] = {}
    p = Path(path)
    if p.exists() and p.is_file():
        with p.open("r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            if isinstance(loaded, dict):
                payload = dict(loaded)

    cfg = TraderConfig.from_mapping(payload)

    db_override = str(os.environ.get("TRADER_RUNTIME_DB_PATH", "")).strip()
    if db_override:
        cfg.runtime_db_path = db_override

    session_override = str(os.environ.get("TRADER_SESSION_ID", "")).strip()
    if session_override:
        cfg.default_session_id = session_override

    ttl_override = str(os.environ.get("TRADER_COMMAND_TTL_SECS", "")).strip()
    if ttl_override:
        try:
            cfg.command_ttl_secs = float(ttl_override)
        except Exception:
            pass

    return cfg
