from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from fxstack.runtime.service import RuntimeService
from fxstack.runtime.sqlite_url import ensure_sqlite_database_dir
from fxstack.settings import get_settings


def repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


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
    s = get_settings()
    svc = RuntimeService(
        database_url=effective_url,
        default_session_id=s.default_session_id,
        command_ttl_secs=s.command_ttl_secs,
        requeue_age_secs=s.startup_requeue_age_secs,
        db_connect_retries=s.db_connect_retries,
    )
    return svc.verify_tables()
