from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(description="Capture legacy baseline freeze artifacts before candidate cutover")
    ap.add_argument("--runtime-db", default="data/state/runtime_v2.db")
    ap.add_argument("--audit-dir", default="data/state/audit")
    ap.add_argument("--out-dir", default="docs")
    args = ap.parse_args()

    root = Path(__file__).resolve().parents[2]
    cmd = [
        sys.executable,
        "-m",
        "src.trader.cli",
        "audit",
        "baseline-freeze",
        "--",
        "--db-path",
        str(Path(args.runtime_db)),
        "--audit-dir",
        str(Path(args.audit_dir)),
        "--out-dir",
        str(Path(args.out_dir)),
    ]
    print("running:", " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(root))


if __name__ == "__main__":
    main()
