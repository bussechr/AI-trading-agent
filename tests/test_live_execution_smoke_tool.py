from __future__ import annotations

import pytest

from tools import live_execution_smoke


def test_event_status_prefers_nested_event_json_status():
    event = {"event_json": {"status": "acked"}}

    assert live_execution_smoke._event_status(event) == "acked"


def test_wait_for_final_event_returns_first_terminal_event(monkeypatch):
    calls = iter(
        [
            [],
            [{"event_json": {"status": "queued"}}],
            [{"event_json": {"status": "acked"}, "reason": "ok"}],
        ]
    )

    monkeypatch.setattr(live_execution_smoke, "_command_events", lambda *a, **k: next(calls))
    monkeypatch.setattr(live_execution_smoke.time, "sleep", lambda *_a, **_k: None)

    final, events = live_execution_smoke._wait_for_final_event("http://bridge", "cmd-1", timeout_secs=0.25)

    assert final["event_json"]["status"] == "acked"
    assert len(events) == 1


def test_wait_for_final_event_times_out_when_no_terminal_event(monkeypatch):
    monkeypatch.setattr(live_execution_smoke, "_command_events", lambda *a, **k: [])
    monkeypatch.setattr(live_execution_smoke.time, "sleep", lambda *_a, **_k: None)

    with pytest.raises(TimeoutError):
        live_execution_smoke._wait_for_final_event("http://bridge", "cmd-1", timeout_secs=0.0)
