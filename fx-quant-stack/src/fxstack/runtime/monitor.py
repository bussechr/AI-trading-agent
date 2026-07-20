"""Small installed-package bridge confidence monitor."""

from __future__ import annotations

import argparse
import os
import time

import requests


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor the isolated fxstack bridge.")
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()

    base = str(args.bridge_url).rstrip("/")
    poll = float(max(0.2, args.poll_seconds))
    api_key = os.environ.get("FXSTACK_BRIDGE_API_KEY", "")
    headers = {"X-API-Key": api_key} if api_key else None
    print(f"Monitoring: {base} every {poll:.1f}s (Ctrl+C to stop)", flush=True)
    while True:
        started = time.time()
        try:
            monitor = requests.get(
                f"{base}/v2/monitor",
                headers=headers,
                timeout=2,
            ).json()
            metrics = requests.get(
                f"{base}/v2/metrics",
                headers=headers,
                timeout=2,
            ).json()
            entry = dict((monitor.get("monitor", {}) or {}).get("entry", {}) or {})
            close = dict((monitor.get("monitor", {}) or {}).get("close", {}) or {})
            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"status={monitor.get('bridge', {}).get('system_status', 'unknown')} "
                f"eq={float(monitor.get('account', {}).get('equity', 0.0)):.2f} "
                f"pending={int((metrics.get('pending', {}) or {}).get('count', 0))} "
                f"entry={entry.get('symbol', 'N/A')}:{entry.get('side', 'N/A')} "
                f"close_reason={close.get('dominant_close_reason', 'none')}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"[{time.strftime('%H:%M:%S')}] monitor error: {exc}",
                flush=True,
            )
        time.sleep(max(0.0, poll - (time.time() - started)))


if __name__ == "__main__":
    main()
