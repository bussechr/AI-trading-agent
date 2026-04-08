from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]
FXSTACK_SRC = REPO_ROOT / "fx-quant-stack" / "src"
if str(FXSTACK_SRC) not in sys.path:
    sys.path.insert(0, str(FXSTACK_SRC))

from fxstack.orchestration.replay import DEFAULT_PROFILE_PATH, run_experiment  # noqa: E402


def _default_experiment_id() -> str:
    return f"orchestration_replay_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Phase 3 orchestration twin replay and parity gates.")
    parser.add_argument("--config", default=str(REPO_ROOT / DEFAULT_PROFILE_PATH))
    parser.add_argument("--experiment-id", default=_default_experiment_id())
    parser.add_argument("--window", choices=["calm", "trend", "shock", "all"], default="all")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--out-dir", default=str(REPO_ROOT / "artifacts" / "orchestration"))
    return parser


def main() -> int:
    args = build_parser().parse_args()
    result = run_experiment(
        config_path=args.config,
        experiment_id=str(args.experiment_id),
        window=str(args.window),
        out_dir=args.out_dir,
        seed=args.seed,
    )
    summary = dict(result["summary"])
    print(f"experiment_id={summary['experiment_id']}")
    print(f"status={summary['status']}")
    print(f"experiment_summary_json={Path(args.out_dir) / str(args.experiment_id) / 'experiment_summary.json'}")
    print(f"promotion_pack_md={Path(args.out_dir) / str(args.experiment_id) / 'promotion_pack.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
