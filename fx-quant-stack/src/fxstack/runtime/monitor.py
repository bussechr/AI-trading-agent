"""Small installed-package bridge confidence monitor."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import time
from typing import Any

import requests


class _Mt4Supervisor:
    """Restart an exited Windows terminal without masking bridge failures."""

    def __init__(
        self,
        *,
        start_script: str,
        stale_polls: int = 3,
        cooldown_seconds: float = 60.0,
    ) -> None:
        raw_path = str(start_script or "").strip()
        self.start_script = (
            Path(raw_path).resolve(strict=True) if raw_path else None
        )
        if self.start_script is not None and self.start_script.name.lower() != (
            "19_start_mt4.ps1"
        ):
            raise ValueError("mt4_start_script_must_be_19_start_mt4_ps1")
        self.stale_polls_required = max(1, int(stale_polls))
        self.cooldown_seconds = max(10.0, float(cooldown_seconds))
        self.stale_polls = 0
        self.last_attempt_at = 0.0

    def observe(self, ready: dict[str, Any], *, now: float) -> str:
        if self.start_script is None:
            return ""
        bridge_healthy = ready.get("bridge_up") is True
        runtime_healthy = ready.get("runtime_ready") is True
        if not bridge_healthy or not runtime_healthy:
            self.stale_polls = 0
            return ""
        if ready.get("mt4_fresh") is True:
            self.stale_polls = 0
            return ""
        self.stale_polls += 1
        if self.stale_polls < self.stale_polls_required:
            return ""
        if now - self.last_attempt_at < self.cooldown_seconds:
            return ""
        self.last_attempt_at = float(now)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.start_script),
                "-WaitSeconds",
                "30",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=45.0,
        )
        if completed.returncode == 0:
            return "mt4_restart_requested"
        detail = str(completed.stderr or completed.stdout or "").strip()
        return f"mt4_restart_failed:{completed.returncode}:{detail[:160]}"


def main() -> None:
    parser = argparse.ArgumentParser(description="Monitor the isolated fxstack bridge.")
    parser.add_argument("--bridge-url", required=True)
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    args = parser.parse_args()

    base = str(args.bridge_url).rstrip("/")
    poll = float(max(0.2, args.poll_seconds))
    supervisor = _Mt4Supervisor(
        start_script=os.environ.get("FXSTACK_MT4_START_SCRIPT", ""),
    )
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
            ready = requests.get(
                f"{base}/v2/ready",
                headers=headers,
                timeout=2,
            ).json()
            supervision_event = supervisor.observe(ready, now=time.time())
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
            if supervision_event:
                print(
                    f"[{time.strftime('%H:%M:%S')}] {supervision_event}",
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
