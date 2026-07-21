from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine


def _write_migration_tree(deployment_root: Path) -> Path:
    stack_root = deployment_root / "fx-quant-stack"
    versions = stack_root / "alembic" / "versions"
    versions.mkdir(parents=True)
    (stack_root / "alembic.ini").write_text(
        "[alembic]\nscript_location = alembic\n",
        encoding="utf-8",
    )
    (stack_root / "alembic" / "env.py").write_text("", encoding="utf-8")
    (versions / "0001_installed_head.py").write_text(
        "revision = 'installed_head'\n"
        "down_revision = None\n"
        "branch_labels = None\n"
        "depends_on = None\n",
        encoding="utf-8",
    )
    return stack_root


def _preflight_check(result: dict[str, object], name: str) -> dict[str, object]:
    checks = list(result.get("checks") or [])
    return next(dict(item) for item in checks if dict(item).get("check") == name)


def test_package_preflight_accepts_live_mt4_bridge_data_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import package_preflight
    from fxstack.settings import get_settings

    monkeypatch.setenv("FXSTACK_DATA_PROVIDER", "mt4_bridge")
    get_settings.cache_clear()
    try:
        result = package_preflight.run_preflight(allow_sqlite=True)
    finally:
        get_settings.cache_clear()

    provider_check = _preflight_check(result, "data_provider_supported")
    assert provider_check == {
        "check": "data_provider_supported",
        "ok": True,
        "detail": "mt4_bridge",
    }


def test_installed_origin_uses_explicit_deployment_migration_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import db_tools, package_preflight, postgres_store
    from fxstack.settings import get_settings

    deployment_root = tmp_path / "deployment"
    stack_root = _write_migration_tree(deployment_root)
    installed_origin = tmp_path / "venv" / "Lib" / "site-packages" / "fxstack" / "runtime"
    installed_origin.mkdir(parents=True)
    monkeypatch.setattr(db_tools, "__file__", str(installed_origin / "db_tools.py"))
    monkeypatch.setattr(postgres_store, "__file__", str(installed_origin / "postgres_store.py"))
    monkeypatch.setenv("FXSTACK_PROJECT_ROOT", str(deployment_root))

    assert db_tools.resolve_migration_root() == stack_root.resolve()
    resolved_root, heads = db_tools.load_migration_heads()
    assert resolved_root == stack_root.resolve()
    assert heads == ["installed_head"]

    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    try:
        store = object.__new__(postgres_store.PostgresRuntimeStore)
        store.engine = engine
        verification = store.verify_required_tables()
    finally:
        engine.dispose()
    migration = dict(verification.get("migration") or {})
    assert migration["expected_heads"] == ["installed_head"]
    assert migration["error"] == ""
    assert verification["ok"] is False

    get_settings.cache_clear()
    try:
        result = package_preflight.run_preflight(allow_sqlite=True)
    finally:
        get_settings.cache_clear()
    migration_check = _preflight_check(result, "migration_resources")
    assert migration_check["ok"] is True
    assert migration_check["detail"] == str(stack_root.resolve())


def test_missing_deployment_migration_root_fails_before_database_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import db_tools, package_preflight, postgres_store
    from fxstack.settings import get_settings

    missing_root = tmp_path / "deployment-without-migrations"
    missing_root.mkdir()
    installed_origin = tmp_path / "venv" / "Lib" / "site-packages" / "fxstack" / "runtime"
    installed_origin.mkdir(parents=True)
    monkeypatch.setattr(db_tools, "__file__", str(installed_origin / "db_tools.py"))
    monkeypatch.setattr(postgres_store, "__file__", str(installed_origin / "postgres_store.py"))
    monkeypatch.setenv("FXSTACK_PROJECT_ROOT", str(missing_root))

    def _unexpected_access(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("database or migration subprocess must not be touched")

    monkeypatch.setattr(db_tools, "create_engine", _unexpected_access)
    monkeypatch.setattr(db_tools.subprocess, "run", _unexpected_access)
    monkeypatch.setattr(postgres_store, "create_engine", _unexpected_access)

    with pytest.raises(db_tools.MigrationResourcesError, match="fxstack_alembic_root_missing"):
        db_tools.resolve_migration_root()
    with pytest.raises(db_tools.MigrationResourcesError, match="fxstack_alembic_root_missing"):
        db_tools.migrate_database(database_url="postgresql+psycopg://invalid")
    with pytest.raises(db_tools.MigrationResourcesError, match="fxstack_alembic_root_missing"):
        db_tools.verify_database(database_url="postgresql+psycopg://invalid")
    with pytest.raises(db_tools.MigrationResourcesError, match="fxstack_alembic_root_missing"):
        postgres_store.PostgresRuntimeStore("postgresql+psycopg://invalid")

    store = object.__new__(postgres_store.PostgresRuntimeStore)
    store.engine = object()
    verification = store.verify_required_tables()
    migration = dict(verification.get("migration") or {})
    assert verification["ok"] is False
    assert migration["ok"] is False
    assert "fxstack_alembic_root_missing" in str(migration["error"])

    get_settings.cache_clear()
    try:
        result = package_preflight.run_preflight(allow_sqlite=True)
    finally:
        get_settings.cache_clear()
    migration_check = _preflight_check(result, "migration_resources")
    assert migration_check["ok"] is False
    assert "fxstack_alembic_root_missing" in str(migration_check["detail"])
