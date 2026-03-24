from __future__ import annotations

import argparse
import json
import os
import time
import uuid
from typing import Any

import requests


def _headers() -> dict[str, str]:
    api_key = str(os.environ.get("FXSTACK_BRIDGE_API_KEY", "")).strip()
    return {"X-API-Key": api_key} if api_key else {}


def _request(method: str, url: str, **kwargs: Any) -> requests.Response:
    response = requests.request(method=method, url=url, headers=_headers(), timeout=5, **kwargs)
    response.raise_for_status()
    return response


def _submit_command(base_url: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", f"{base_url.rstrip('/')}/v2/commands", json=payload).json()


def _command_events(base_url: str, command_id: str) -> list[dict[str, Any]]:
    out = _request(
        "GET",
        f"{base_url.rstrip('/')}/v2/commands/events",
        params={"command_id": command_id, "limit": 100},
    ).json()
    return list(out.get("events", []) or [])


def _event_status(event: dict[str, Any]) -> str:
    return str(
        event.get("event_status")
        or event.get("status")
        or (event.get("event_json") or {}).get("status")
        or ""
    ).strip().lower()


def _wait_for_final_event(base_url: str, command_id: str, timeout_secs: float) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.time() + float(timeout_secs)
    last_events: list[dict[str, Any]] = []
    while time.time() < deadline:
        last_events = _command_events(base_url, command_id)
        if last_events:
            for event in last_events:
                final = dict(event or {})
                status = _event_status(final)
                if status in {"acked", "failed", "expired", "already_finalized"}:
                    return final, last_events
        time.sleep(1.0)
    raise TimeoutError(f"timed out waiting for command final event: {command_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Submit a live execution smoke command and optionally close it.")
    ap.add_argument("--bridge-url", default="http://127.0.0.1:58710")
    ap.add_argument("--pair", required=True)
    ap.add_argument("--side", default="BUY", choices=["BUY", "SELL"])
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--timeout-secs", type=float, default=45.0)
    ap.add_argument("--hold-secs", type=float, default=3.0)
    ap.add_argument("--skip-close", action="store_true")
    args = ap.parse_args()

    pair = str(args.pair).upper().strip()
    side = str(args.side).upper().strip()
    trace = uuid.uuid4().hex[:8]
    open_id = f"smoke-open-{pair.lower()}-{int(time.time())}-{trace}"
    open_payload = {
        "command_id": open_id,
        "cmd": side,
        "symbol": pair,
        "lots": float(args.lots),
        "intent": "SMOKE_TEST",
        "trace_id": open_id,
    }
    open_queued = _submit_command(args.bridge_url, open_payload)
    open_final, open_events = _wait_for_final_event(args.bridge_url, open_id, float(args.timeout_secs))

    result: dict[str, Any] = {
        "pair": pair,
        "side": side,
        "open": {
            "queued": open_queued,
            "final": open_final,
            "events": open_events,
        },
    }
    if _event_status(open_final) != "acked":
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(2)

    if bool(args.skip_close):
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    time.sleep(max(0.0, float(args.hold_secs)))
    close_id = f"smoke-close-{pair.lower()}-{int(time.time())}-{trace}"
    close_payload = {
        "command_id": close_id,
        "cmd": "CLOSE",
        "symbol": pair,
        "lots": 0.0,
        "intent": "SMOKE_TEST",
        "trace_id": close_id,
    }
    close_queued = _submit_command(args.bridge_url, close_payload)
    close_final, close_events = _wait_for_final_event(args.bridge_url, close_id, float(args.timeout_secs))
    result["close"] = {
        "queued": close_queued,
        "final": close_final,
        "events": close_events,
    }
    print(json.dumps(result, indent=2, sort_keys=True))

    if _event_status(close_final) != "acked":
        raise SystemExit(3)


if __name__ == "__main__":
    main()
