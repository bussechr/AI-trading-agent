from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
for _path in (REPO_ROOT, FXSTACK_SRC):
    _text = str(_path)
    if _text not in sys.path:
        sys.path.insert(0, _text)

from fxstack.runtime.db_tools import migrate_database


def _bootstrap_workspace() -> Path:
    os.chdir(str(REPO_ROOT))
    return REPO_ROOT


def _prepare_worker_database(*, repo_root: Path, database_url: str) -> None:
    if not str(database_url or "").strip():
        return
    out = migrate_database(database_url=str(database_url).strip(), root=repo_root / "fx-quant-stack")
    if not bool(out.get("ok")) or int(out.get("return_code", 1)) != 0:
        raise RuntimeError(
            "feature-push worker database migration failed: "
            + str(out.get("stderr") or out.get("stdout") or out.get("return_code") or "unknown")
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Feast feature-push worker in a simple restart loop.")
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--sleep-secs", type=float, default=5.0)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=0)
    args = parser.parse_args()

    repo_root = _bootstrap_workspace()
    _prepare_worker_database(repo_root=repo_root, database_url=str(args.database_url or ""))

    from fxstack.feast.push import drain_feature_push_outbox
    from fxstack.runtime.service import RuntimeService

    sleep_secs = max(1.0, float(args.sleep_secs))
    service = RuntimeService(database_url=str(args.database_url or ""))
    # Emit readiness once bootstrap and DB/service construction succeed so the
    # background launcher does not mistake a long first drain for a dead worker.
    print("[feature-push-worker] ready", flush=True)

    while True:
        try:
            out = drain_feature_push_outbox(
                service,
                worker_id=str(args.worker_id or ""),
                limit=int(args.limit),
                repo_root=str(args.repo_root),
                max_retries=int(args.max_retries or 0),
            )
            print(out, flush=True)
        except Exception as exc:
            print(f"[feature-push-worker] error={type(exc).__name__}: {exc}", flush=True)
            traceback.print_exc()
        time.sleep(sleep_secs)


if __name__ == "__main__":
    raise SystemExit(main())
