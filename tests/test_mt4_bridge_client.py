from __future__ import annotations


def test_post_visuals_attempts_once_when_max_retries_zero(monkeypatch):
    from src.execution import mt4_bridge_client as client

    calls: list[tuple[str, dict, float]] = []

    class _Resp:
        def raise_for_status(self) -> None:
            return None

    def _fake_post(url: str, json: dict, timeout: float):
        calls.append((url, dict(json or {}), float(timeout)))
        return _Resp()

    monkeypatch.setattr(client.requests, "post", _fake_post)
    client.post_visuals({"symbol": "EURUSD", "type": "hud"}, max_retries=0)

    assert len(calls) == 1
    assert calls[0][0].endswith("/v2/visuals")

