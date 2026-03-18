from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ENV_PREFIXES = ("TRADER_", "MT4_", "FXSTACK_")


@dataclass(slots=True)
class CommandResult:
    name: str
    command: str
    cwd: str
    return_code: int
    passed: bool
    duration_secs: float
    log_file: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def _run_output(cmd: list[str], cwd: Path | None = None) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            check=False,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        merged = out
        if err:
            merged = f"{out}\n{err}".strip()
        return int(proc.returncode), merged
    except Exception as exc:
        return 127, f"{type(exc).__name__}: {exc}"


def _python_cmd(py_exe: Path, args: list[str]) -> list[str]:
    return [str(py_exe)] + list(args)


def _pick_fxstack_python(root: Path) -> Path:
    candidates = [
        root / "fx-quant-stack" / ".venv" / "bin" / "python",
        root / "fx-quant-stack" / ".venv" / "Scripts" / "python.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return Path(sys.executable)


def _launcher_defaults(root: Path) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    pattern = re.compile(r"if not defined ([A-Za-z0-9_]+)\s+set\s+\1=(.*)", re.IGNORECASE)
    for rel in ("run_bridge.bat", "run_agent.bat", "start.bat"):
        defaults: dict[str, str] = {}
        path = root / rel
        if not path.exists():
            out[rel] = defaults
            continue
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = pattern.search(line.strip())
            if not m:
                continue
            defaults[m.group(1).strip()] = m.group(2).strip()
        out[rel] = defaults
    return out


def _collect_metadata(root: Path) -> dict[str, Any]:
    rc_sha, git_sha = _run_output(["git", "rev-parse", "HEAD"], cwd=root)
    rc_py, py_ver = _run_output([sys.executable, "--version"], cwd=root)
    rc_node, node_ver = _run_output(["node", "--version"], cwd=root)
    rc_pnpm, pnpm_ver = _run_output(["pnpm", "--version"], cwd=root)
    rc_uv, uv_ver = _run_output(["uv", "--version"], cwd=root)
    env_snapshot = {
        k: os.environ[k]
        for k in sorted(os.environ.keys())
        if any(k.startswith(prefix) for prefix in ENV_PREFIXES)
    }
    return {
        "generated_at": _now_iso(),
        "git": {"sha": git_sha if rc_sha == 0 else "", "ok": rc_sha == 0},
        "versions": {
            "python": py_ver if rc_py == 0 else "",
            "node": node_ver if rc_node == 0 else "",
            "pnpm": pnpm_ver if rc_pnpm == 0 else "",
            "uv": uv_ver if rc_uv == 0 else "",
        },
        "env": env_snapshot,
        "launcher_defaults": _launcher_defaults(root),
    }


def _run_command(
    *,
    name: str,
    cmd: list[str],
    cwd: Path,
    logs_dir: Path,
    env: dict[str, str] | None = None,
) -> CommandResult:
    started = time.time()
    log_path = logs_dir / f"{name}.log"
    command_text = " ".join(shlex.quote(part) for part in cmd)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        duration = max(0.0, time.time() - started)
        body = [
            f"$ {command_text}",
            "",
            "## stdout",
            proc.stdout or "",
            "",
            "## stderr",
            proc.stderr or "",
        ]
        log_path.write_text("\n".join(body), encoding="utf-8")
        return CommandResult(
            name=name,
            command=command_text,
            cwd=str(cwd),
            return_code=int(proc.returncode),
            passed=int(proc.returncode) == 0,
            duration_secs=duration,
            log_file=str(log_path),
        )
    except Exception as exc:
        duration = max(0.0, time.time() - started)
        log_path.write_text(f"$ {command_text}\n\nERROR: {type(exc).__name__}: {exc}\n", encoding="utf-8")
        return CommandResult(
            name=name,
            command=command_text,
            cwd=str(cwd),
            return_code=127,
            passed=False,
            duration_secs=duration,
            log_file=str(log_path),
        )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _default_blockers() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "required_fields": [
            "id",
            "severity",
            "layer",
            "description",
            "evidence_path",
            "owner",
            "status",
            "target_fix_date",
        ],
        "blockers": [],
    }


def _default_gate_summary(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "fast_gate": {
            "status": "pending",
            "required_thresholds": {
                "require_nonzero_entries": True,
                "min_throughput_delta": 1,
                "max_timeout_rate": 0.05,
            },
            "artifact_path": "",
        },
        "shadow_24h": {
            "status": "pending",
            "required_thresholds": {
                "ack_timeout_rate_5m_max": 0.01,
                "no_duplicate_fill_symptoms": True,
                "no_hard_breaker_violations": True,
                "no_persistent_queue_backlog": True,
            },
            "artifact_path": "",
        },
        "targets": {
            "baseline_url": str(args.baseline_url),
            "candidate_url": str(args.candidate_url),
            "profile": str(args.profile),
        },
    }


def _go_no_go_from_files(blockers: dict[str, Any], gate_summary: dict[str, Any]) -> dict[str, Any]:
    open_blockers = []
    for row in list(blockers.get("blockers", []) or []):
        severity = str(row.get("severity", "")).strip().lower()
        status = str(row.get("status", "")).strip().lower()
        if severity in {"critical", "high"} and status not in {"closed", "resolved", "done"}:
            open_blockers.append(dict(row))

    fast_ok = str(((gate_summary.get("fast_gate") or {}).get("status") or "")).lower() == "pass"
    shadow_ok = str(((gate_summary.get("shadow_24h") or {}).get("status") or "")).lower() == "pass"
    go = bool((not open_blockers) and fast_ok and shadow_ok)
    reasons: list[str] = []
    if open_blockers:
        reasons.append("open_critical_high_blockers")
    if not fast_ok:
        reasons.append("fast_gate_not_passed")
    if not shadow_ok:
        reasons.append("shadow_24h_not_passed")

    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "decision": "GO" if go else "HOLD",
        "go": go,
        "reasons": reasons,
        "open_critical_high_count": len(open_blockers),
        "open_critical_high": open_blockers,
    }


def _render_master_report(
    *,
    metadata: dict[str, Any],
    checks: list[CommandResult],
    evidence_dir: Path,
    baseline_result: CommandResult,
) -> str:
    lines = [
        "# Full FX Quant Process Audit - Master Report",
        "",
        f"Generated at: `{metadata.get('generated_at', '')}`",
        f"Git SHA: `{(metadata.get('git', {}) or {}).get('sha', 'unknown')}`",
        f"Evidence directory: `{evidence_dir}`",
        "",
        "## Phase 0 Summary",
        "",
        f"- Baseline freeze command: `{'PASS' if baseline_result.passed else 'FAIL'}` (rc={baseline_result.return_code})",
        f"- Baseline freeze log: `{baseline_result.log_file}`",
        "",
        "## Phase 1 Static Checks",
        "",
    ]
    if checks:
        for item in checks:
            lines.append(
                f"- `{item.name}`: {'PASS' if item.passed else 'FAIL'} "
                f"(rc={item.return_code}, {item.duration_secs:.1f}s) -> `{item.log_file}`"
            )
    else:
        lines.append("- No static checks were executed.")

    lines.extend(
        [
            "",
            "## Pending Live Assurance Commands",
            "",
            "### 15m Fast Gate",
            "```bash",
            "python -m src.trader.cli scenario shadow-run -- \\",
            "  --baseline-url http://127.0.0.1:58710 \\",
            "  --candidate-url http://127.0.0.1:58711 \\",
            "  --duration-secs 900 \\",
            "  --poll-secs 2 \\",
            "  --min-throughput-delta 1 \\",
            "  --max-timeout-rate 0.05 \\",
            "  --require-nonzero-entries \\",
            "  --out-dir docs \\",
            "  --prefix canary_shadow_fast15m",
            "```",
            "",
            "### 24h Shadow",
            "```bash",
            "python -m src.trader.cli scenario shadow-run -- \\",
            "  --baseline-url http://127.0.0.1:58710 \\",
            "  --candidate-url http://127.0.0.1:58711 \\",
            "  --duration-secs 86400 \\",
            "  --poll-secs 2 \\",
            "  --min-throughput-delta 1 \\",
            "  --max-timeout-rate 0.01 \\",
            "  --require-nonzero-entries \\",
            "  --out-dir docs \\",
            "  --prefix canary_shadow_24h",
            "```",
            "",
            "## Signoff Rule",
            "",
            "- Zero open `Critical` and `High` blockers.",
            "- `fast_gate` and `shadow_24h` statuses are both `pass`.",
            "- `tools/finalize_build.py` emits `GO` in `go_no_go.json`.",
            "",
        ]
    )
    return "\n".join(lines)


def _write_template(path: Path, title: str, body_lines: list[str]) -> None:
    lines = [f"# {title}", ""] + body_lines + [""]
    path.write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    root = _repo_root()
    stamp = datetime.now().strftime("%Y%m%d")
    evidence_dir = _ensure_dir(Path(args.evidence_root) / f"{stamp}_full_process")
    logs_dir = _ensure_dir(evidence_dir / "logs")

    metadata = _collect_metadata(root)
    _write_json(evidence_dir / "metadata.json", metadata)

    baseline_cmd = _python_cmd(
        Path(sys.executable),
        [
            str(root / "fx-quant-stack" / "scripts" / "freeze_baseline.py"),
            "--runtime-db",
            str(args.runtime_db),
            "--audit-dir",
            str(args.audit_dir),
            "--out-dir",
            str(evidence_dir),
        ],
    )
    baseline_result = _run_command(
        name="phase0_baseline_freeze",
        cmd=baseline_cmd,
        cwd=root,
        logs_dir=logs_dir,
    )

    checks: list[CommandResult] = []
    if not bool(args.skip_static_checks):
        uv_bin = shutil.which("uv")
        if uv_bin:
            checks.append(
                _run_command(
                    name="phase1_uv_sync",
                    cmd=[uv_bin, "sync", "--frozen", "--python", "3.11", "--extra", "dev"],
                    cwd=root / "fx-quant-stack",
                    logs_dir=logs_dir,
                )
            )
        else:
            missing_uv_log = logs_dir / "phase1_uv_sync.log"
            missing_uv_log.write_text("uv binary not found on PATH\n", encoding="utf-8")
            checks.append(
                CommandResult(
                    name="phase1_uv_sync",
                    command="uv sync --frozen --python 3.11 --extra dev",
                    cwd=str(root / "fx-quant-stack"),
                    return_code=127,
                    passed=False,
                    duration_secs=0.0,
                    log_file=str(missing_uv_log),
                )
            )

        root_tests = [
            "tests/test_trader_cli.py",
            "tests/test_runtime_service_v2.py",
            "tests/test_decision_pipeline.py",
            "tests/test_trader_cli_fxstack_commands.py",
        ]
        checks.append(
            _run_command(
                name="phase1_root_compat_tests",
                cmd=_python_cmd(Path(sys.executable), ["-m", "pytest", "-q", "-s"] + root_tests),
                cwd=root,
                logs_dir=logs_dir,
            )
        )

        fxstack_py = _pick_fxstack_python(root)
        checks.append(
            _run_command(
                name="phase1_fxstack_tests",
                cmd=_python_cmd(fxstack_py, ["-m", "pytest", "-q", "-s", "tests"]),
                cwd=root / "fx-quant-stack",
                logs_dir=logs_dir,
                env={
                    **os.environ,
                    "PYTHONPATH": "src",
                    "FXSTACK_DATA_PROVIDER": os.environ.get("FXSTACK_DATA_PROVIDER", "dukascopy"),
                    "FXSTACK_DUKASCOPY_SOURCE_ROOT": os.environ.get("FXSTACK_DUKASCOPY_SOURCE_ROOT", "./data/dukascopy"),
                    "FXSTACK_DATABASE_URL": os.environ.get(
                        "FXSTACK_DATABASE_URL",
                        "sqlite+pysqlite:///./test_runtime.db",
                    ),
                },
            )
        )

        if not bool(args.skip_frontend):
            checks.append(
                _run_command(
                    name="phase1_frontend_install",
                    cmd=["pnpm", "install", "--frozen-lockfile"],
                    cwd=root,
                    logs_dir=logs_dir,
                )
            )
            checks.append(
                _run_command(
                    name="phase1_frontend_build",
                    cmd=["pnpm", "build"],
                    cwd=root,
                    logs_dir=logs_dir,
                )
            )

    _write_json(
        evidence_dir / "phase1_static_checks.json",
        {
            "generated_at": _now_iso(),
            "results": [item.to_dict() for item in checks],
            "all_passed": all(item.passed for item in checks) if checks else True,
        },
    )

    blockers = _default_blockers()
    blockers_path = evidence_dir / "blockers.json"
    if not blockers_path.exists():
        _write_json(blockers_path, blockers)
    else:
        blockers = json.loads(blockers_path.read_text(encoding="utf-8"))

    gate_summary = _default_gate_summary(args)
    gate_summary_path = evidence_dir / "gate_summary.json"
    if not gate_summary_path.exists():
        _write_json(gate_summary_path, gate_summary)
    else:
        gate_summary = json.loads(gate_summary_path.read_text(encoding="utf-8"))

    go_no_go = _go_no_go_from_files(blockers, gate_summary)
    _write_json(evidence_dir / "go_no_go.json", go_no_go)

    _write_template(
        evidence_dir / "cutover_checklist.md",
        "Cutover Checklist",
        [
            "- [ ] Fast gate passed (`15m`, strict).",
            "- [ ] 24h shadow passed.",
            "- [ ] `go_no_go.json` decision is `GO`.",
            "- [ ] Runtime policy remains v2-only.",
            "- [ ] Rollback command validated before cutover.",
        ],
    )

    _write_template(
        evidence_dir / "rollback_runbook.md",
        "Rollback Runbook",
        [
            "1. Stop runtime and bridge processes.",
            "2. Restore last-known-good model activation manifest (and DB snapshot if required).",
            "3. Restart v2 bridge/runtime launchers.",
            "4. Verify `/v2/health`, `/v2/state`, `/v2/metrics` are healthy.",
            "5. Record rollback event in blocker register and governance log.",
        ],
    )

    report = _render_master_report(
        metadata=metadata,
        checks=checks,
        evidence_dir=evidence_dir,
        baseline_result=baseline_result,
    )
    (evidence_dir / "master_report.md").write_text(report, encoding="utf-8")

    print(json.dumps({"evidence_dir": str(evidence_dir), "go_no_go": go_no_go.get("decision", "HOLD")}, indent=2))

    failed = (not baseline_result.passed) or any(not c.passed for c in checks)
    if failed and bool(args.strict):
        return 2
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Bootstrap full FX quant process audit evidence and static checks")
    ap.add_argument("--evidence-root", default="docs/audit")
    ap.add_argument("--runtime-db", default="data/state/runtime_v2.db")
    ap.add_argument("--audit-dir", default="data/state/audit")
    ap.add_argument("--baseline-url", default="http://127.0.0.1:58710")
    ap.add_argument("--candidate-url", default="http://127.0.0.1:58711")
    ap.add_argument("--profile", default="balanced")
    ap.add_argument("--skip-static-checks", action="store_true", default=False)
    ap.add_argument("--skip-frontend", action="store_true", default=False)
    ap.add_argument("--strict", action="store_true", default=False)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
