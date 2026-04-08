from __future__ import annotations

import subprocess
from pathlib import Path


def test_orchestration_canary_control_help_runs_from_repo_root():
    repo_root = Path(__file__).resolve().parents[1]
    proc = subprocess.run(
        ["python3", "tools/orchestration_canary_control.py", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "status" in proc.stdout
    assert "advance-stage" in proc.stdout
