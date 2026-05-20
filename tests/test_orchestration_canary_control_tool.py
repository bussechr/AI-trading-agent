from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_orchestration_canary_control_help_runs_from_repo_root():
    repo_root = Path(__file__).resolve().parents[1]
    # Use sys.executable so the subprocess runs under the same Python (and
    # therefore the same venv with the project's dependencies) as the test.
    # Hardcoding "python3" picks up whatever shell resolution chooses, which
    # on Windows + non-PATH'd dev environments may be a bare system Python
    # without fxstack deps installed.
    proc = subprocess.run(
        [sys.executable, "tools/orchestration_canary_control.py", "--help"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "status" in proc.stdout
    assert "advance-stage" in proc.stdout
