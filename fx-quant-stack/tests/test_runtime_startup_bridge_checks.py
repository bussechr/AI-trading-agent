from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.runtime import startup


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = dict(payload)

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        mt4_bridge_url="http://127.0.0.1:58710",
        bridge_api_key="test-key",
    )


def _install_bridge_responses(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handshake: dict[str, Any],
) -> list[str]:
    calls: list[str] = []

    def _urlopen(request: Any, *, timeout: float) -> _Response:
        assert timeout == 5.0
        url = str(request.full_url)
        calls.append(url)
        if url.endswith("/v2/handshake"):
            return _Response(handshake)
        if url.endswith("/v2/positions/reconcile"):
            assert request.get_header("X-api-key") == "test-key"
            return _Response(
                {
                    "only_in_db": [],
                    "only_in_ea": [],
                    "lot_mismatches": [],
                    "ea_snapshot_available": True,
                }
            )
        raise AssertionError(f"unexpected URL: {url}")

    monkeypatch.setattr(startup.urllib.request, "urlopen", _urlopen)
    return calls


def test_startup_bridge_check_fails_on_protocol_major_mismatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_bridge_responses(
        monkeypatch,
        handshake={"protocol_version": "v3.0.0", "min_compatible": "v3.0.0"},
    )

    with pytest.raises(startup.BridgeProtocolMismatchError, match="major mismatch"):
        startup.perform_startup_bridge_checks(_settings())

    assert calls == ["http://127.0.0.1:58710/v2/handshake"]
    assert "FATAL bridge protocol major mismatch" in capsys.readouterr().out


@pytest.mark.parametrize(
    "handshake",
    [
        {},
        {"protocol_version": ""},
        {"protocol_version": "v2"},
        {"protocol_version": "not-a-version"},
    ],
    ids=["missing", "empty", "incomplete", "malformed"],
)
def test_startup_bridge_check_fails_closed_on_missing_or_malformed_protocol(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handshake: dict[str, Any],
) -> None:
    calls = _install_bridge_responses(monkeypatch, handshake=handshake)

    with pytest.raises(startup.BridgeProtocolMismatchError, match="protocol missing or malformed"):
        startup.perform_startup_bridge_checks(_settings())

    assert calls == ["http://127.0.0.1:58710/v2/handshake"]
    assert "FATAL bridge handshake protocol missing or malformed" in capsys.readouterr().out


def test_startup_bridge_check_fails_closed_on_malformed_minimum_compatible(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_bridge_responses(
        monkeypatch,
        handshake={
            "protocol_version": startup.BRIDGE_PROTOCOL_VERSION,
            "min_compatible": "not-a-version",
        },
    )

    with pytest.raises(startup.BridgeProtocolMismatchError, match="minimum compatible version"):
        startup.perform_startup_bridge_checks(_settings())

    assert calls == ["http://127.0.0.1:58710/v2/handshake"]
    assert "FATAL bridge minimum compatible version" in capsys.readouterr().out


def test_startup_bridge_check_warns_on_compatible_minor_or_patch_drift(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_bridge_responses(
        monkeypatch,
        handshake={"protocol_version": "v2.9.7", "min_compatible": "v2.0.0"},
    )

    startup.perform_startup_bridge_checks(_settings())

    assert calls == [
        "http://127.0.0.1:58710/v2/handshake",
        "http://127.0.0.1:58710/v2/positions/reconcile",
    ]
    output = capsys.readouterr().out
    assert "WARN compatible bridge handshake drift" in output
    assert "position reconcile OK" in output


def test_startup_bridge_check_honors_server_minimum_compatible_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls = _install_bridge_responses(
        monkeypatch,
        handshake={"protocol_version": "v2.9.0", "min_compatible": "v2.2.0"},
    )

    with pytest.raises(startup.BridgeProtocolMismatchError, match="older than bridge minimum"):
        startup.perform_startup_bridge_checks(_settings())

    assert calls == ["http://127.0.0.1:58710/v2/handshake"]
    assert "FATAL runtime protocol" in capsys.readouterr().out
