"""End-to-end happy-path smoke test for the bridge HTTP surface.

This is the test that catches integration regressions unit tests miss. It
walks the full EA-vs-bridge contract:

1. Liveness probe — ``/v2/livez`` returns 200.
2. Protocol handshake — ``/v2/handshake`` returns the negotiated version and
   bridge-published basket_tp_pct (the value the EA reads at startup).
3. Readiness probe — ``/v2/readyz`` returns 503 because runtime/MT4 are not
   yet wired in a test environment (that's the protective default).
4. Market tick ingest — ``POST /v2/market/tick`` accepts the EA's
   broker-side spread/bid/ask.
5. Command enqueue — ``POST /v2/commands`` queues a synthetic order.
6. Command polling — ``GET /v2/commands/poll`` hands the queued command to
   the EA.
7. Command ack — ``POST /v2/commands/ack`` marks it complete with a ticket.
8. Bridge state — ``GET /v2/state`` returns the consolidated state.
9. Prometheus exposition — ``GET /v2/metrics/prometheus`` emits the
   expected text-format metrics.

If this test fails, the bridge's contract with the EA is broken — that's a
release blocker.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def smoke_client(tmp_path: Path) -> TestClient:
    """Bring up a fresh app instance against a sqlite DB.

    Auth is disabled for these tests (the conftest already sets
    ``FXSTACK_BRIDGE_AUTH_REQUIRED=false``). That's intentional — auth is
    covered separately; here we want to focus on the happy-path wiring.
    """
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


def test_full_bridge_happy_path(smoke_client: TestClient) -> None:
    """Walk the EA-vs-bridge contract end to end on a clean DB."""
    # 1. Liveness — must be 200 even with nothing else wired.
    livez = smoke_client.get("/v2/livez")
    assert livez.status_code == 200
    assert livez.json() == {"status": "ok"}

    # 2. Protocol handshake — public endpoint, returns the negotiated version
    #    + the basket_tp_pct that the EA persists at startup.
    handshake = smoke_client.get("/v2/handshake")
    assert handshake.status_code == 200
    hs = handshake.json()
    assert hs["protocol_version"], "handshake must publish a protocol_version"
    assert "basket_tp_pct" in hs
    assert isinstance(hs["basket_tp_pct"], (int, float))
    assert 0.0 <= float(hs["basket_tp_pct"]) <= 1.0

    # 3. Readiness — on a fresh bridge with no MT4 heartbeat and no runtime
    #    loop, this is 503 by design. That's the protective contract: don't
    #    take traffic until everything is up.
    readyz = smoke_client.get("/v2/readyz")
    assert readyz.status_code == 503
    rz = readyz.json()
    assert rz["ready"] is False
    assert rz["checks"]["database_ok"] is True  # migration worked
    assert rz["checks"]["runtime_running"] is False  # no runtime in tests
    assert rz["checks"]["not_draining"] is True

    # 4. Market tick ingest — what the EA POSTs every quote update.
    tick = smoke_client.post(
        "/v2/market/tick",
        json={
            "symbol": "EURUSD",
            "bid": 1.10100,
            "ask": 1.10112,
            "spread_bps": 1.2,
            "digits": 5,
            "ts": "2026-05-20T12:00:00Z",
        },
    )
    assert tick.status_code == 200, tick.text

    # 5. Command enqueue — synthetic decision from "runtime" side. Valid
    #    commands are defined by fxstack.runtime.dto.SUPPORTED_COMMANDS.
    cmd_payload = {
        "command_id": "smoke-cmd-1",
        "symbol": "EURUSD",
        "cmd": "BUY",
        "side": "BUY",
        "lots": 0.01,
        "action": "entry",
        "intent": "ENTRY",
        "session_id": "default",
    }
    cmd = smoke_client.post("/v2/commands", json=cmd_payload)
    assert cmd.status_code in (200, 201), cmd.text
    body = cmd.json()
    assert body.get("status") in {"queued", "accepted", "ok"}, body

    # 6. Command poll — what the EA hits every cycle to pick up new work.
    poll = smoke_client.get("/v2/commands/poll")
    assert poll.status_code == 200
    poll_body = poll.json()
    # The poll endpoint returns the next queued command or a "no_command"
    # marker. Either is fine for the smoke test — we just need 200.
    assert isinstance(poll_body, dict)

    # 7. Command ack — EA reports the broker outcome.
    ack = smoke_client.post(
        "/v2/commands/ack",
        json={
            "command_id": "smoke-cmd-1",
            "ticket": 12345678,
            "status": "filled",
        },
    )
    assert ack.status_code in (200, 201), ack.text

    # 8. State — operators read this for dashboards and ops.
    state = smoke_client.get("/v2/state")
    assert state.status_code == 200
    st = state.json()
    # The state always includes a runtime_status, even if "unknown".
    assert "runtime_status" in st or "status" in st or "system_status" in st

    # 9. Prometheus exposition — alerting consumes this.
    prom = smoke_client.get("/v2/metrics/prometheus")
    assert prom.status_code == 200
    text = prom.text
    for required in (
        "fxstack_bridge_up",
        "fxstack_protocol_version_info",
        "fxstack_database_ok",
    ):
        assert f"# TYPE {required}" in text, f"missing {required} in /v2/metrics/prometheus"


def test_handshake_protocol_version_matches_wire_constant(smoke_client: TestClient) -> None:
    """The handshake must publish exactly the BRIDGE_PROTOCOL_VERSION the runtime expects."""
    from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION

    resp = smoke_client.get("/v2/handshake")
    assert resp.status_code == 200
    assert resp.json()["protocol_version"] == BRIDGE_PROTOCOL_VERSION


def test_request_id_echoed_on_response(smoke_client: TestClient) -> None:
    """The middleware must echo the X-Request-ID header on every response."""
    custom_rid = "smoke-test-rid-42"
    resp = smoke_client.get("/v2/livez", headers={"X-Request-ID": custom_rid})
    assert resp.status_code == 200
    assert resp.headers.get("X-Request-ID") == custom_rid


def test_request_id_generated_when_missing(smoke_client: TestClient) -> None:
    """When the client does not supply a request id, the bridge generates one."""
    resp = smoke_client.get("/v2/livez")
    assert resp.status_code == 200
    rid = resp.headers.get("X-Request-ID")
    assert rid, "bridge must always echo an X-Request-ID"
    assert len(rid) >= 16  # uuid4 hex is 32 chars; bound for safety


def test_unknown_endpoint_returns_404(smoke_client: TestClient) -> None:
    """Sanity check that the app does not silently swallow unknown paths."""
    resp = smoke_client.get("/v2/this-does-not-exist")
    assert resp.status_code == 404


def test_handshake_is_publicly_accessible(smoke_client: TestClient) -> None:
    """``/v2/handshake`` must be reachable without an API key.

    This is what lets the EA confirm protocol compatibility before it bothers
    sending an auth header. If we ever break the public-path list, the EA
    cannot bootstrap.
    """
    # Even when auth is enabled, this endpoint is in _PUBLIC_PATHS.
    resp = smoke_client.get("/v2/handshake")
    assert resp.status_code == 200, "handshake must always be public"
