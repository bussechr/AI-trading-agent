from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from fxstack.runtime.sqlite_url import ensure_sqlite_database_dir


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def ping_database(*, database_url: str) -> dict[str, Any]:
    effective_url = ensure_sqlite_database_dir(database_url, base_dir=Path.cwd())
    engine = create_engine(str(effective_url), future=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "database_url": str(effective_url),
            "dialect": str(engine.dialect.name),
            "server_reachable": True,
        }
    except Exception as exc:
        return {
            "ok": False,
            "database_url": str(effective_url),
            "dialect": str(engine.dialect.name),
            "server_reachable": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    finally:
        engine.dispose()


def migrate_database(*, database_url: str, root: Path | None = None) -> dict[str, Any]:
    base = (root or repo_root()).resolve()
    ini = base / "alembic.ini"
    effective_url = ensure_sqlite_database_dir(database_url, base_dir=base.parent)
    cmd = [
        sys.executable,
        "-m",
        "alembic",
        "-c",
        str(ini),
        "upgrade",
        "head",
    ]
    env = dict(os.environ)
    env["FXSTACK_DATABASE_URL"] = str(effective_url)
    proc = subprocess.run(cmd, cwd=str(base), env=env, text=True, capture_output=True, check=False)
    return {
        "command": cmd,
        "database_url": str(effective_url),
        "return_code": int(proc.returncode),
        "stdout": str(proc.stdout or ""),
        "stderr": str(proc.stderr or ""),
        "ok": int(proc.returncode) == 0,
    }


def verify_database(*, database_url: str) -> dict[str, Any]:
    effective_url = ensure_sqlite_database_dir(database_url, base_dir=Path.cwd())
    required = {
        "commands",
        "command_events",
        "runtime_state",
        "market_ticks",
        "reports",
        "decision_snapshots",
        "governance_events",
        "model_runs",
        "model_artifacts",
        "active_model_sets",
    }
    engine = create_engine(str(effective_url), future=True)
    present: set[str] = set()
    try:
        with engine.connect() as conn:
            present = set(inspect(conn).get_table_names())
    finally:
        engine.dispose()
    missing = sorted(required - present)
    table_check = {
        "required": sorted(required),
        "present": sorted(present),
        "missing": missing,
        "missing_tables": missing,
        "ok": len(missing) == 0,
    }

    base = repo_root()
    ini = base / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(base / "alembic"))
    script = ScriptDirectory.from_config(cfg)
    heads = sorted(str(h) for h in script.get_heads())

    current: list[str] = []
    alembic_table_present = False
    engine = create_engine(str(effective_url), future=True)
    try:
        with engine.connect() as conn:
            alembic_table_present = "alembic_version" in set(inspect(conn).get_table_names())
            if alembic_table_present:
                rows = conn.execute(text("SELECT version_num FROM alembic_version")).fetchall()
                current = sorted({str(r[0]) for r in rows if r and r[0]})
    finally:
        engine.dispose()

    migration_ok = alembic_table_present and set(current) == set(heads)
    out = dict(table_check)
    out["migration"] = {
        "ok": bool(migration_ok),
        "expected_heads": heads,
        "current_revisions": current,
        "alembic_table_present": bool(alembic_table_present),
    }
    out["ok"] = bool(table_check.get("ok")) and bool(migration_ok)
    return out
