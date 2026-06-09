"""Run the optimize-only self-correction loop continuously.

The supervisor does not submit broker commands. It repeatedly runs the existing
`trader agent improve` pipeline, writes auditable artifacts, and optionally
registers draft experiment proposals in the runtime experiment factory.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
FX_ROOT = ROOT / "fx-quant-stack"
DEFAULT_PY = FX_ROOT / ".venv_win" / "Scripts" / "python.exe"
ARTIFACT_ROOT = ROOT / "artifacts" / "self_correction"
HISTORY_PATH = ARTIFACT_ROOT / "history.jsonl"
LATEST_PATH = ARTIFACT_ROOT / "latest.json"


def _python_exe() -> str:
    env_py = str(os.environ.get("TRADER_PYTHON_EXE") or "").strip()
    if env_py:
        return env_py
    if DEFAULT_PY.exists():
        return str(DEFAULT_PY)
    return sys.executable


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")


def _append_history(payload: dict[str, Any]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, sort_keys=True, default=str) + "\n")


def _latest_real_dataset() -> Path | None:
    candidates = list((FX_ROOT / "artifacts" / "reports" / "backtests").glob("**/scored_signals_real.parquet"))
    candidates += list((FX_ROOT / "artifacts" / "reports" / "backtests").glob("**/scored_signals.parquet"))
    candidates = [path for path in candidates if path.is_file()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _latest_backtest_run_with_signals() -> str:
    roots = [path.parent for path in (FX_ROOT / "artifacts" / "reports" / "backtests").glob("**/signals_sample.csv")]
    roots = [path for path in roots if path.is_dir()]
    if not roots:
        return ""
    return max(roots, key=lambda path: path.stat().st_mtime).name


def _run(cmd: list[str], *, env: dict[str, str], log_path: Path) -> dict[str, Any]:
    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "\n".join(
            [
                "$ " + " ".join(cmd),
                "",
                "STDOUT:",
                proc.stdout or "",
                "",
                "STDERR:",
                proc.stderr or "",
            ]
        ),
        encoding="utf-8",
    )
    return {
        "cmd": cmd,
        "returncode": int(proc.returncode),
        "seconds": round(time.time() - started, 3),
        "log_path": str(log_path),
    }


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _prepare_dataset(py: str, env: dict[str, str], run_dir: Path, explicit_dataset: str) -> tuple[str, dict[str, Any]]:
    explicit = str(explicit_dataset or "").strip()
    if explicit:
        return explicit, {"source": "explicit", "path": explicit}

    existing = _latest_real_dataset()
    if existing is not None:
        return str(existing), {"source": "latest_existing_real_dataset", "path": str(existing)}

    run_name = _latest_backtest_run_with_signals()
    if not run_name:
        return "", {"source": "synthetic_fallback", "reason": "no_real_scored_signal_dataset_or_signals_sample"}

    build_log = run_dir / "build_realdata_dataset.log"
    result = _run([py, str(ROOT / "tools" / "build_realdata_selfcorrect_dataset.py"), run_name], env=env, log_path=build_log)
    built = FX_ROOT / "artifacts" / "reports" / "backtests" / run_name / "scored_signals_real.parquet"
    if result["returncode"] == 0 and built.exists():
        return str(built), {"source": "built_from_latest_signals", "run_name": run_name, "path": str(built), "build": result}
    return "", {"source": "synthetic_fallback", "run_name": run_name, "build": result}


def run_cycle(args: argparse.Namespace) -> dict[str, Any]:
    stamp = _stamp()
    run_dir = ARTIFACT_ROOT / "runs" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    py = _python_exe()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    env["FXSTACK_AGENT_MODE"] = "shadow"
    env["FX_AGENT_EXECUTION_MODE"] = "shadow"
    env["FXSTACK_AUTONOMOUS_SELF_CORRECTION"] = "1"

    dataset, dataset_info = _prepare_dataset(py, env, run_dir, str(args.dataset))
    improve_cmd = [
        py,
        "-m",
        "src.trader.cli",
        "agent",
        "improve",
        "--out-dir",
        str(run_dir / "improve"),
        "--run-name",
        f"auto_{stamp}",
        "--iterations",
        str(int(args.iterations)),
        "--restarts",
        str(int(args.restarts)),
    ]
    if dataset:
        improve_cmd.extend(["--dataset", dataset])
    if bool(args.register):
        improve_cmd.append("--register")

    improve = _run(improve_cmd, env=env, log_path=run_dir / "agent_improve.log")
    best_config = run_dir / "improve" / "best_config.json"
    summary_path = run_dir / "improve" / "summary.json"
    proposal_path = run_dir / "improve" / "proposal.json"

    robustness: dict[str, Any] = {"skipped": True}
    if improve["returncode"] == 0 and best_config.exists():
        rob_cmd = [
            py,
            "-m",
            "src.trader.cli",
            "agent",
            "robustness",
            "--run-dir",
            str(run_dir / "improve"),
        ]
        if dataset:
            rob_cmd.extend(["--dataset", dataset])
        robustness = _run(rob_cmd, env=env, log_path=run_dir / "robustness.log")

    payload = {
        "stamp": stamp,
        "status": "ok" if improve["returncode"] == 0 else "failed",
        "mode": "optimize_only_shadow",
        "broker_execution": "disabled_by_runtime_agent_mode_guard",
        "dataset": dataset_info,
        "improve": improve,
        "robustness": robustness,
        "artifacts": {
            "run_dir": str(run_dir),
            "best_config": str(best_config) if best_config.exists() else "",
            "summary": str(summary_path) if summary_path.exists() else "",
            "proposal": str(proposal_path) if proposal_path.exists() else "",
        },
        "summary": _load_json(summary_path),
    }
    _json_write(run_dir / "cycle_summary.json", payload)
    _json_write(LATEST_PATH, payload)
    _append_history(payload)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Continuous optimize-only self-correction supervisor")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit")
    parser.add_argument("--interval-minutes", type=float, default=float(os.environ.get("FXSTACK_SELF_CORRECT_INTERVAL_MINUTES", "360")))
    parser.add_argument("--iterations", type=int, default=int(os.environ.get("FXSTACK_SELF_CORRECT_ITERATIONS", "12")))
    parser.add_argument("--restarts", type=int, default=int(os.environ.get("FXSTACK_SELF_CORRECT_RESTARTS", "4")))
    parser.add_argument("--dataset", default=os.environ.get("FXSTACK_SELF_CORRECT_DATASET", ""))
    parser.add_argument("--register", action=argparse.BooleanOptionalAction, default=True)
    args = parser.parse_args()

    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    while True:
        payload = run_cycle(args)
        print(json.dumps({"status": payload["status"], "stamp": payload["stamp"], "latest": str(LATEST_PATH)}, sort_keys=True))
        if bool(args.once):
            return 0 if payload["status"] == "ok" else 1
        time.sleep(max(60.0, float(args.interval_minutes) * 60.0))


if __name__ == "__main__":
    raise SystemExit(main())
