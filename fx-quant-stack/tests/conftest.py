from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def _env_setup(tmp_path_factory: pytest.TempPathFactory):
    db_dir = tmp_path_factory.mktemp("db")
    db_url = f"sqlite+pysqlite:///{Path(db_dir) / 'runtime.db'}"
    data_dir = tmp_path_factory.mktemp("dukascopy")
    os.environ.setdefault("FXSTACK_DATABASE_URL", db_url)
    os.environ.setdefault("FXSTACK_DATA_PROVIDER", "dukascopy")
    os.environ.setdefault("FXSTACK_DUKASCOPY_SOURCE_ROOT", str(data_dir))
    os.environ.setdefault("FXSTACK_DUKASCOPY_FILE_PATTERN", "{pair}_{granularity}.csv")
    os.environ.setdefault("MT4_BRIDGE_URL", "http://127.0.0.1:58710")
    # Bridge auth defaults to required in production; explicitly opt out for tests so
    # importing fxstack.api.app doesn't register the fail-secure 503 middleware.
    os.environ.setdefault("FXSTACK_BRIDGE_AUTH_REQUIRED", "false")
