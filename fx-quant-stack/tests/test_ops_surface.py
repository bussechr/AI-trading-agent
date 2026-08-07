"""Tests for the production ops surface added on top of the bridge.

Pins three contracts:
* JSON log formatter emits one parseable JSON object per line, with stable
  field names and the request-id correlation tag.
* ``/v2/livez`` returns 200 unconditionally; ``/v2/readyz`` returns 200 when
  all checks pass and 503 with the same payload shape when any fail.
* ``service.drain()`` flips a fence; subsequent ``submit_command`` calls
  return 503 with a clear reason rather than enqueuing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# JSON log formatter
# ---------------------------------------------------------------------------


def _capture_formatter_output(*, formatter: logging.Formatter, level: int, msg: str, **extra: object) -> str:
    """Render a single log record through ``formatter`` and return the string."""
    record = logging.LogRecord(
        name="fxstack.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
        func="test",
    )
    record.request_id = "abc123"
    for k, v in extra.items():
        setattr(record, k, v)
    return formatter.format(record)


def test_json_formatter_emits_parseable_json_with_required_fields() -> None:
    from fxstack.api.middleware import _JsonLogFormatter

    line = _capture_formatter_output(
        formatter=_JsonLogFormatter(),
        level=logging.INFO,
        msg="hello",
    )
    payload = json.loads(line)
    assert payload["level"] == "INFO"
    assert payload["msg"] == "hello"
    assert payload["logger"] == "fxstack.test"
    assert payload["rid"] == "abc123"
    assert payload["ts"]  # ISO-8601 timestamp
    # Stable enough to be greppable: starts with year + 'T'
    assert "T" in payload["ts"]


def test_json_formatter_includes_extra_fields() -> None:
    """`logger.info("...", extra={"pair": "EURUSD"})` flows through as a top-level key."""
    from fxstack.api.middleware import _JsonLogFormatter

    line = _capture_formatter_output(
        formatter=_JsonLogFormatter(),
        level=logging.INFO,
        msg="signal",
        pair="EURUSD",
        score=0.62,
    )
    payload = json.loads(line)
    assert payload["pair"] == "EURUSD"
    assert payload["score"] == 0.62


def test_json_formatter_skips_internal_logrecord_fields() -> None:
    """Default LogRecord noise (pathname, process, etc.) is not in the JSON."""
    from fxstack.api.middleware import _JsonLogFormatter

    line = _capture_formatter_output(
        formatter=_JsonLogFormatter(),
        level=logging.INFO,
        msg="x",
    )
    payload = json.loads(line)
    for noisy in ("pathname", "filename", "lineno", "process", "threadName", "args"):
        assert noisy not in payload


def test_json_formatter_handles_exceptions() -> None:
    from fxstack.api.middleware import _JsonLogFormatter

    try:
        raise RuntimeError("boom")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="fxstack.test",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="failed",
        args=None,
        exc_info=exc_info,
        func="test",
    )
    record.request_id = "-"
    line = _JsonLogFormatter().format(record)
    payload = json.loads(line)
    assert payload["level"] == "ERROR"
    assert "RuntimeError: boom" in payload["exc"]


def test_configure_structured_logging_json_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without `FXSTACK_LOG_FORMAT`, the handler uses JSON output."""
    from fxstack.api.middleware import _JsonLogFormatter, configure_structured_logging

    monkeypatch.delenv("FXSTACK_LOG_FORMAT", raising=False)
    # Strip any prior handler so we observe a fresh install
    pkg = logging.getLogger("fxstack")
    pkg.handlers = [h for h in pkg.handlers if not getattr(h, "_fxstack_structured", False)]

    configure_structured_logging()
    matches = [h for h in pkg.handlers if getattr(h, "_fxstack_structured", False)]
    assert matches, "structured handler should be installed"
    assert isinstance(matches[0].formatter, _JsonLogFormatter)


def test_configure_structured_logging_honors_plain_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FXSTACK_LOG_FORMAT=plain` keeps the legacy human-readable format."""
    from fxstack.api.middleware import _JsonLogFormatter, configure_structured_logging

    monkeypatch.setenv("FXSTACK_LOG_FORMAT", "plain")
    pkg = logging.getLogger("fxstack")
    pkg.handlers = [h for h in pkg.handlers if not getattr(h, "_fxstack_structured", False)]

    configure_structured_logging()
    matches = [h for h in pkg.handlers if getattr(h, "_fxstack_structured", False)]
    assert matches
    assert not isinstance(matches[0].formatter, _JsonLogFormatter)


# ---------------------------------------------------------------------------
# Drain fence on RuntimeService
# ---------------------------------------------------------------------------


def test_drain_flips_fence_and_submit_command_returns_503(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After drain(), submit_command must reject with 503."""
    monkeypatch.setenv("FXSTACK_DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}")
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.runtime.service import RuntimeService
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    migrate_database(database_url=os.environ["FXSTACK_DATABASE_URL"], root=Path(__file__).resolve().parents[1])

    svc = RuntimeService(database_url=os.environ["FXSTACK_DATABASE_URL"])
    assert svc.draining is False

    svc.drain()
    assert svc.draining is True

    body, status = svc.submit_command(
        {"cmd": "OPEN", "symbol": "EURUSD", "lots": 0.01, "intent": "ENTRY", "action": "entry"}
    )
    assert status == 503
    assert body["status"] == "draining"


# ---------------------------------------------------------------------------
# /v2/livez and /v2/readyz endpoints
# ---------------------------------------------------------------------------


def _fresh_app_client(tmp_path: Path) -> TestClient:
    """Bring up a fresh app instance against an empty sqlite DB."""
    database_url = f"sqlite+pysqlite:///{tmp_path / 'runtime.db'}"
    os.environ["FXSTACK_DATABASE_URL"] = database_url
    os.environ["FXSTACK_ALLOW_SQLITE"] = "true"
    from fxstack.runtime.db_tools import migrate_database
    from fxstack.settings import get_settings

    get_settings.cache_clear()
    out = migrate_database(database_url=database_url, root=Path(__file__).resolve().parents[1])
    assert bool(out.get("ok")), out
    get_settings.cache_clear()
    if "fxstack.api.app" in sys.modules:
        del sys.modules["fxstack.api.app"]
    from fxstack.api.app import app

    return TestClient(app)


def test_livez_returns_200_unconditionally(tmp_path: Path) -> None:
    """Liveness probe must never fail — process up is the only check."""
    client = _fresh_app_client(tmp_path)
    resp = client.get("/v2/livez")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_returns_503_when_runtime_not_running(tmp_path: Path) -> None:
    """Fresh bridge with no runtime loop yet → readyz must report 503.

    This is the protective contract: a freshly-booted bridge with no MT4
    connection should NOT receive traffic until the runtime is up.
    """
    client = _fresh_app_client(tmp_path)
    resp = client.get("/v2/readyz")
    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    # Specific check names so dashboards can target them
    checks = payload["checks"]
    for key in ("database_ok", "runtime_running", "mt4_fresh", "not_draining"):
        assert key in checks
    # DB migrated cleanly, so that one is true
    assert checks["database_ok"] is True
    # Runtime not started in test, so this is false
    assert checks["runtime_running"] is False


def test_readyz_payload_shape_is_stable(tmp_path: Path) -> None:
    """The readyz response must always have ``ready`` (bool) + ``checks`` (dict)."""
    client = _fresh_app_client(tmp_path)
    resp = client.get("/v2/readyz")
    payload = resp.json()
    assert isinstance(payload["ready"], bool)
    assert isinstance(payload["checks"], dict)
    # All checks are boolean — no Nones, no strings
    for k, v in payload["checks"].items():
        assert isinstance(v, bool), f"check {k} is not a bool: {v!r}"


def test_readyz_fails_closed_for_malformed_liveness_timestamps(tmp_path: Path) -> None:
    client = _fresh_app_client(tmp_path)
    from fxstack.api.app import service

    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": "not-a-timestamp",
            "system_status": "connected",
            "last_heartbeat": "also-not-a-timestamp",
        }
    )

    resp = client.get("/v2/readyz")

    assert resp.status_code == 503
    assert resp.json()["checks"]["runtime_running"] is False
    assert resp.json()["checks"]["mt4_fresh"] is False


def test_readyz_rejects_liveness_timestamps_far_in_the_future(tmp_path: Path) -> None:
    client = _fresh_app_client(tmp_path)
    from fxstack.api.app import service

    future_ts = time.time() + 3600.0
    service.patch_state(
        {
            "runtime_status": "running",
            "runtime_last_cycle_ts": future_ts,
            "system_status": "connected",
            "last_heartbeat": future_ts,
        }
    )

    resp = client.get("/v2/readyz")

    assert resp.status_code == 503
    assert resp.json()["checks"]["runtime_running"] is False
    assert resp.json()["checks"]["mt4_fresh"] is False
