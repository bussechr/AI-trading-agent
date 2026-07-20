"""Installed-package Feast outbox worker for the isolated production runtime."""

from __future__ import annotations

import argparse
from pathlib import Path
import time
import traceback

from fxstack.runtime.db_tools import migrate_database
from fxstack.settings import get_settings


def _stack_root(project_root: Path) -> Path:
    root = Path(project_root).expanduser().resolve()
    nested = root / "fx-quant-stack"
    if (nested / "alembic.ini").is_file():
        return nested
    if (root / "alembic.ini").is_file():
        return root
    raise RuntimeError(f"fxstack_alembic_root_missing:{root}")


def _prepare_worker_database(*, project_root: Path, database_url: str) -> None:
    if not str(database_url or "").strip():
        return
    out = migrate_database(
        database_url=str(database_url).strip(),
        root=_stack_root(project_root),
    )
    if not bool(out.get("ok")) or int(out.get("return_code", 1)) != 0:
        raise RuntimeError(
            "feature-push worker database migration failed: "
            + str(
                out.get("stderr")
                or out.get("stdout")
                or out.get("return_code")
                or "unknown"
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Feast feature-push worker in a simple restart loop."
    )
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--repo-root", required=True)
    parser.add_argument("--sleep-secs", type=float, default=5.0)
    parser.add_argument("--database-url", default="")
    parser.add_argument("--worker-id", default="")
    parser.add_argument("--instance-id", default="baseline", help=argparse.SUPPRESS)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--max-retries", type=int, default=0)
    args = parser.parse_args()

    settings = get_settings()
    project_root = Path(str(args.project_root or settings.project_root)).resolve()
    database_url = str(args.database_url or settings.database_url or "").strip()
    _prepare_worker_database(
        project_root=project_root,
        database_url=database_url,
    )

    from fxstack.feast.push import drain_feature_push_outbox
    from fxstack.runtime.service import RuntimeService

    sleep_secs = max(1.0, float(args.sleep_secs))
    worker_id = str(args.worker_id or "").strip() or str(
        settings.feature_push_worker_id or "feature-push-worker"
    ).strip()
    service = RuntimeService(database_url=database_url)
    print(
        f"[feature-push-worker] ready instance_id={args.instance_id} worker_id={worker_id}",
        flush=True,
    )

    while True:
        try:
            out = drain_feature_push_outbox(
                service,
                worker_id=worker_id,
                limit=int(args.limit),
                repo_root=str(args.repo_root),
                max_retries=int(args.max_retries or 0),
            )
            print(out, flush=True)
        except Exception as exc:
            print(
                f"[feature-push-worker] error={type(exc).__name__}: {exc}",
                flush=True,
            )
            traceback.print_exc()
        time.sleep(sleep_secs)


if __name__ == "__main__":
    main()
