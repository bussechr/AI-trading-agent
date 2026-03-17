from __future__ import annotations

import argparse
from pathlib import Path

from tools import baseline_freeze


def test_baseline_freeze_generates_artifacts(tmp_path, monkeypatch):
    db_path = tmp_path / "runtime.db"
    audit_dir = tmp_path / "audit"
    out_dir = tmp_path / "out"

    (audit_dir / "interop").mkdir(parents=True, exist_ok=True)
    (audit_dir / "interop" / "trace.jsonl").write_text('{"x":1}\n{"x":2}\n', encoding="utf-8")

    monkeypatch.setenv("TRADER_RUNTIME_DB_PATH", str(db_path))

    code = baseline_freeze.run(
        argparse.Namespace(
            db_path=str(db_path),
            audit_dir=str(audit_dir),
            out_dir=str(out_dir),
        )
    )
    assert int(code) == 0

    json_files = sorted(Path(out_dir).glob("baseline_freeze_*.json"))
    md_files = sorted(Path(out_dir).glob("baseline_freeze_*.md"))
    assert json_files
    assert md_files

    payload = json_files[-1].read_text(encoding="utf-8")
    assert "contract_matrix" in payload
    assert "kpis" in payload
