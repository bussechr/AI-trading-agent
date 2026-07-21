from fastapi import FastAPI
from fastapi.testclient import TestClient

from fxstack.api.auth import add_api_key_middleware


def _client(*, command_token: str) -> TestClient:
    app = FastAPI()

    @app.get("/v2/state")
    def state() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v2/commands/poll")
    def poll() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v2/commands/ack")
    def ack() -> dict[str, bool]:
        return {"ok": True}

    add_api_key_middleware(
        app,
        "telemetry-key",
        required=True,
        command_token=command_token,
    )
    return TestClient(app)


def test_command_channel_rejects_general_bridge_key() -> None:
    client = _client(command_token="command-key")
    assert client.get("/v2/state", headers={"X-API-Key": "telemetry-key"}).status_code == 200
    assert client.get("/v2/commands/poll", headers={"X-API-Key": "telemetry-key"}).status_code == 401
    assert client.post("/v2/commands/ack", headers={"X-API-Key": "telemetry-key"}).status_code == 401
    assert client.get("/v2/commands/poll", headers={"X-API-Key": "command-key"}).status_code == 200
    assert client.post("/v2/commands/ack", headers={"X-API-Key": "command-key"}).status_code == 200


def test_staged_channel_retains_legacy_key_when_no_command_token_is_configured() -> None:
    client = _client(command_token="")
    assert client.get("/v2/commands/poll", headers={"X-API-Key": "telemetry-key"}).status_code == 200
