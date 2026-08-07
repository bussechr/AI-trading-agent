"""Pytest setup for the root-level compatibility test suite.

This conftest runs before any test in this directory imports modules from
``fxstack``. It mirrors the explicit project-root and auth opt-out applied in
``fx-quant-stack/tests/conftest.py`` so collection never depends on ambient
machine configuration.
"""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
ROOT_TEST_DB = REPO_ROOT / ".pytest_cache" / "root-runtime.db"
ROOT_TEST_DB.parent.mkdir(exist_ok=True)
ROOT_TEST_DB.unlink(missing_ok=True)

# Bridge auth defaults to required in production; explicitly opt out for tests.
os.environ["FXSTACK_BRIDGE_AUTH_REQUIRED"] = "false"
os.environ["FXSTACK_PROJECT_ROOT"] = str(REPO_ROOT)
# Root compatibility tests must never depend on a locally running PostgreSQL
# service. Set this before collection imports the module-level bridge service.
os.environ["FXSTACK_DATABASE_URL"] = f"sqlite+pysqlite:///{ROOT_TEST_DB.as_posix()}"
os.environ["FXSTACK_ALLOW_SQLITE"] = "true"

sys.path.insert(0, str(FXSTACK_SRC))

migrate_database = importlib.import_module(
    "fxstack.runtime.db_tools"
).migrate_database

migration = migrate_database(
    database_url=os.environ["FXSTACK_DATABASE_URL"],
    root=REPO_ROOT,
)
if not migration["ok"]:
    raise RuntimeError(f"Unable to migrate isolated root test database: {migration}")
