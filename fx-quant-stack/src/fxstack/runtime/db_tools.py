from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from fxstack.runtime.sqlite_url import ensure_sqlite_database_dir


class MigrationResourcesError(RuntimeError):
    """Raised when the deployed Alembic resource tree is unavailable."""


def resolve_migration_root(project_root: str | Path | None = None) -> Path:
    """Resolve Alembic resources from the explicit deployment root only."""

    configured = str(
        project_root
        if project_root is not None
        else os.environ.get("FXSTACK_PROJECT_ROOT", "") or ""
    ).strip()
    if not configured:
        raise MigrationResourcesError(
            "fxstack_project_root_required: set FXSTACK_PROJECT_ROOT to the deployment "
            "root containing fx-quant-stack/alembic.ini"
        )

    deployment_root = Path(configured).expanduser().resolve()
    candidates = (deployment_root / "fx-quant-stack", deployment_root)
    for candidate in candidates:
        migration_dir = candidate / "alembic"
        if (
            (candidate / "alembic.ini").is_file()
            and (migration_dir / "env.py").is_file()
            and (migration_dir / "versions").is_dir()
        ):
            return candidate

    expected = ", ".join(str(candidate) for candidate in candidates)
    raise MigrationResourcesError(
        "fxstack_alembic_root_missing: "
        f"FXSTACK_PROJECT_ROOT={deployment_root}; expected migration resources under {expected}"
    )


def repo_root() -> Path:
    """Compatibility wrapper for callers that use FXSTACK_PROJECT_ROOT."""

    return resolve_migration_root()


def load_migration_heads(*, root: str | Path | None = None) -> tuple[Path, list[str]]:
    base = resolve_migration_root(root)
    try:
        cfg = Config(str(base / "alembic.ini"))
        cfg.set_main_option("script_location", str(base / "alembic"))
        script = ScriptDirectory.from_config(cfg)
        heads = sorted(str(head) for head in script.get_heads())
    except Exception as exc:
        raise MigrationResourcesError(
            f"fxstack_alembic_load_failed:{base / 'alembic'}:{type(exc).__name__}: {exc}"
        ) from exc
    if not heads:
        raise MigrationResourcesError(f"fxstack_alembic_heads_missing:{base / 'alembic'}")
    return base, heads


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


def migrate_database(*, database_url: str, root: str | Path | None = None) -> dict[str, Any]:
    base, _ = load_migration_heads(root=root)
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


def verify_database(*, database_url: str, root: str | Path | None = None) -> dict[str, Any]:
    _, heads = load_migration_heads(root=root)
    effective_url = ensure_sqlite_database_dir(database_url, base_dir=Path.cwd())
    required = {
        "commands",
        "command_events",
        "runtime_state",
        "market_ticks",
        "reports",
        "decision_snapshots",
        "orchestration_runs",
        "agent_proposals",
        "governed_decisions",
        "agent_traces",
        "approval_events",
        "experiment_proposals",
        "experiment_promotions",
        "experiment_lineage",
        "governance_events",
        "feature_push_outbox",
        "feature_push_audit",
        "feature_parity_audit",
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


def main() -> None:
    from fxstack.settings import get_settings

    parser = argparse.ArgumentParser(description="Installed fxstack database tooling")
    parser.add_argument("command", choices=("ping", "migrate", "verify"))
    parser.add_argument("--database-url", default="")
    parser.add_argument("--project-root", default="")
    parser.add_argument("--allow-sqlite", action="store_true")
    args = parser.parse_args()

    settings = get_settings()
    database_url = str(args.database_url or settings.database_url)
    allow_sqlite = bool(args.allow_sqlite or settings.allow_sqlite)
    if database_url.lower().startswith("sqlite") and not allow_sqlite:
        print(json.dumps({"ok": False, "error": "sqlite_blocked"}, sort_keys=True))
        raise SystemExit(2)

    explicit_root = str(args.project_root or os.environ.get("FXSTACK_PROJECT_ROOT", "") or "").strip()
    try:
        if args.command == "ping":
            result = ping_database(database_url=database_url)
        elif args.command == "migrate":
            result = migrate_database(database_url=database_url, root=explicit_root or None)
        else:
            result = verify_database(database_url=database_url, root=explicit_root or None)
    except MigrationResourcesError as exc:
        result = {"ok": False, "error": str(exc)}
    print(json.dumps(result, sort_keys=True, default=str))
    raise SystemExit(0 if bool(result.get("ok")) else 1)


if __name__ == "__main__":
    main()
