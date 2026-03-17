from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools import dual_run_compare


def test_dual_run_compare_generates_report(tmp_path):
    base = tmp_path / "baseline.jsonl"
    cand = tmp_path / "candidate.jsonl"
    out = tmp_path / "out"

    base.write_text(
        "\n".join(
            [
                json.dumps({"command_id": "c1", "status": "acked", "symbol": "EURUSD", "cmd": "BUY"}),
                json.dumps({"command_id": "c2", "status": "failed", "symbol": "GBPUSD", "cmd": "SELL"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cand.write_text(
        "\n".join(
            [
                json.dumps({"command_id": "c1", "status": "acked", "symbol": "EURUSD", "cmd": "BUY"}),
                json.dumps({"command_id": "c2", "status": "acked", "symbol": "GBPUSD", "cmd": "SELL"}),
                json.dumps({"command_id": "c3", "status": "queued", "symbol": "USDJPY", "cmd": "BUY"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    code = dual_run_compare.run(
        argparse.Namespace(
            baseline=str(base),
            candidate=str(cand),
            out_dir=str(out),
            mismatch_limit=10,
        )
    )
    assert int(code) == 0

    json_files = sorted(Path(out).glob("dual_run_compare_*.json"))
    md_files = sorted(Path(out).glob("dual_run_compare_*.md"))
    assert json_files
    assert md_files

    payload = json.loads(json_files[-1].read_text(encoding="utf-8"))
    assert int(payload["overlap_commands"]) == 2
    assert int(payload["only_candidate"]) == 1
    assert float(payload["status_match_rate"]) < 1.0
