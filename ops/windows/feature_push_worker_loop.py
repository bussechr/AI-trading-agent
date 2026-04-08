from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Feast feature-push worker in a simple restart loop.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--sleep-secs", type=float, default=5.0)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=0)
    args = parser.parse_args()

    os.chdir(os.getcwd())
    sleep_secs = max(1.0, float(args.sleep_secs))
    base_cmd = [
        sys.executable,
        "-u",
        "-m",
        "src.trader.cli",
        "features",
        "push-worker",
        "--repo-root",
        str(args.repo_root),
        "--limit",
        str(int(args.limit)),
    ]
    if str(args.database_url or "").strip():
        base_cmd.extend(["--database-url", str(args.database_url).strip()])
    if str(args.worker_id or "").strip():
        base_cmd.extend(["--worker-id", str(args.worker_id).strip()])
    if int(args.max_retries or 0) > 0:
        base_cmd.extend(["--max-retries", str(int(args.max_retries))])

    while True:
        rc = subprocess.call(base_cmd)
        if rc:
            print(f"[feature-push-worker] last_run_rc={rc}", flush=True)
        time.sleep(sleep_secs)


if __name__ == "__main__":
    raise SystemExit(main())
