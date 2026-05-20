"""Pin the contract that the runtime refuses to boot with a broken schema.

The schema-version protection lives in
``fxstack.runtime.postgres_store.PostgresRuntimeStore._bootstrap_schema``
and runs when ``RuntimeService(...)`` is constructed. It catches:

* **Missing tables** — any required table absent → RuntimeError at boot.
* **Stale migration** — all tables present but the alembic head doesn't
  match the script directory → RuntimeError at boot.

This is the classic "deploy that skipped migrations" guard. A bridge
serving traffic against a stale schema is worse than a bridge that
refuses to start; this test exists so a future refactor doesn't
silently drop the guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_runtime_service_refuses_unmigrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Constructing a RuntimeService against an empty DB must raise."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'unmigrated.db'}"
    monkeypatch.setenv("FXSTACK_DATABASE_URL", database_url)
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")

    # Create the file but do NOT run migrations.
    (tmp_path / "unmigrated.db").touch()

    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    with pytest.raises(RuntimeError) as exc_info:
        RuntimeService(database_url=database_url)
    msg = str(exc_info.value).lower()
    assert "schema" in msg or "missing_tables" in msg or "migration" in msg, exc_info.value


def test_runtime_service_starts_with_migrated_database(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A migrated DB lets RuntimeService construct cleanly."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("FXSTACK_DATABASE_URL", database_url)
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")

    from fxstack.runtime.db_tools import migrate_database
    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out

    # No exception — migrated DB satisfies _bootstrap_schema.
    svc = RuntimeService(database_url=database_url)
    health = svc.get_health()
    assert bool(health.get("tables_ok")) is True


def test_verify_required_tables_includes_migration_head_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The verification result must report migration head info, not just tables.

    Defense in depth: even if a future change drops a table from the
    required set, this test pins that the migration-version check is part
    of the contract.
    """
    database_url = f"sqlite+pysqlite:///{tmp_path / 'migrated.db'}"
    monkeypatch.setenv("FXSTACK_DATABASE_URL", database_url)
    monkeypatch.setenv("FXSTACK_ALLOW_SQLITE", "true")

    from fxstack.runtime.db_tools import migrate_database
    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    svc = RuntimeService(database_url=database_url)
    check = svc.store.verify_required_tables()
    migration = dict(check.get("migration") or {})
    assert "expected_heads" in migration
    assert "current_revisions" in migration
    assert migration["ok"] is True
    assert migration["expected_heads"] == migration["current_revisions"]
