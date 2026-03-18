from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _resolve_latest_evidence_dir(evidence_root: Path) -> Path | None:
    if not evidence_root.exists():
        return None
    candidates = [p for p in evidence_root.glob("*_full_process") if p.is_dir()]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]


def _parse_gate_artifact(payload: dict[str, Any]) -> dict[str, Any]:
    # fast gate format: {passed: bool, checks: {...}, metrics: {...}}
    if "passed" in payload and isinstance(payload.get("passed"), bool):
        return {
            "status": "pass" if bool(payload.get("passed")) else "fail",
            "checks": dict(payload.get("checks", {}) or {}),
            "metrics": dict(payload.get("metrics", {}) or {}),
        }
    # shadow format: {gates: {passed: bool, checks: {...}}, ...}
    gates = dict(payload.get("gates", {}) or {})
    if "passed" in gates and isinstance(gates.get("passed"), bool):
        return {
            "status": "pass" if bool(gates.get("passed")) else "fail",
            "checks": dict(gates.get("checks", {}) or {}),
            "metrics": {
                "throughput_delta_entries_acked": gates.get("throughput_delta_entries_acked"),
                "rollback_triggers": list(gates.get("rollback_triggers", []) or []),
            },
        }
    return {"status": "pending", "checks": {}, "metrics": {}}


def _open_critical_high(blockers: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in list(blockers.get("blockers", []) or []):
        if not isinstance(row, dict):
            continue
        severity = str(row.get("severity", "")).strip().lower()
        status = str(row.get("status", "")).strip().lower()
        if severity in {"critical", "high"} and status not in {"closed", "resolved", "done"}:
            out.append(dict(row))
    return out


def _decision_payload(
    *,
    blockers: dict[str, Any],
    gate_summary: dict[str, Any],
    rollback_validated: bool,
) -> dict[str, Any]:
    open_ch = _open_critical_high(blockers)
    fast_status = str(((gate_summary.get("fast_gate") or {}).get("status") or "pending")).lower()
    shadow_status = str(((gate_summary.get("shadow_24h") or {}).get("status") or "pending")).lower()

    checks = {
        "no_open_critical_high": len(open_ch) == 0,
        "fast_gate_passed": fast_status == "pass",
        "shadow_24h_passed": shadow_status == "pass",
        "rollback_validated": bool(rollback_validated),
    }
    go = all(bool(v) for v in checks.values())

    reasons: list[str] = []
    if not checks["no_open_critical_high"]:
        reasons.append("open_critical_high_blockers")
    if not checks["fast_gate_passed"]:
        reasons.append("fast_gate_not_passed")
    if not checks["shadow_24h_passed"]:
        reasons.append("shadow_24h_not_passed")
    if not checks["rollback_validated"]:
        reasons.append("rollback_not_validated")

    return {
        "schema_version": 1,
        "generated_at": _now_iso(),
        "decision": "GO" if go else "HOLD",
        "go": bool(go),
        "checks": checks,
        "reasons": reasons,
        "open_critical_high_count": len(open_ch),
        "open_critical_high": open_ch,
    }


def run(args: argparse.Namespace) -> int:
    evidence_dir = Path(args.evidence_dir) if args.evidence_dir else _resolve_latest_evidence_dir(Path(args.evidence_root))
    if evidence_dir is None:
        raise SystemExit("No audit evidence directory found. Run tools/full_process_audit.py first.")
    evidence_dir = evidence_dir.resolve()

    blockers_path = evidence_dir / "blockers.json"
    gate_summary_path = evidence_dir / "gate_summary.json"

    blockers = _load_json(blockers_path)
    gate_summary = _load_json(gate_summary_path)
    if not blockers:
        blockers = {"schema_version": 1, "generated_at": _now_iso(), "blockers": []}
    if not gate_summary:
        gate_summary = {"schema_version": 1, "generated_at": _now_iso(), "fast_gate": {}, "shadow_24h": {}}

    if args.fast_gate_artifact:
        fast_art = _load_json(Path(args.fast_gate_artifact))
        parsed = _parse_gate_artifact(fast_art)
        gate_summary["fast_gate"] = {
            **dict(gate_summary.get("fast_gate", {}) or {}),
            "status": parsed["status"],
            "artifact_path": str(args.fast_gate_artifact),
            "checks": parsed["checks"],
            "metrics": parsed["metrics"],
        }

    if args.shadow_artifact:
        shadow_art = _load_json(Path(args.shadow_artifact))
        parsed = _parse_gate_artifact(shadow_art)
        gate_summary["shadow_24h"] = {
            **dict(gate_summary.get("shadow_24h", {}) or {}),
            "status": parsed["status"],
            "artifact_path": str(args.shadow_artifact),
            "checks": parsed["checks"],
            "metrics": parsed["metrics"],
        }

    gate_summary["finalized_at"] = _now_iso()
    gate_summary_path.write_text(json.dumps(gate_summary, indent=2, sort_keys=True), encoding="utf-8")

    decision = _decision_payload(
        blockers=blockers,
        gate_summary=gate_summary,
        rollback_validated=bool(args.rollback_validated),
    )
    go_no_go_path = evidence_dir / "go_no_go.json"
    go_no_go_path.write_text(json.dumps(decision, indent=2, sort_keys=True), encoding="utf-8")

    summary_lines = [
        "# Finalization Summary",
        "",
        f"Evidence directory: `{evidence_dir}`",
        f"Decision: **{decision.get('decision', 'HOLD')}**",
        f"Generated at: `{decision.get('generated_at', '')}`",
        "",
        "## Checks",
        "",
    ]
    for name, value in dict(decision.get("checks", {}) or {}).items():
        summary_lines.append(f"- `{name}`: `{value}`")
    summary_lines.extend(
        [
            "",
            "## Reasons",
            "",
        ]
    )
    reasons = list(decision.get("reasons", []) or [])
    if reasons:
        for reason in reasons:
            summary_lines.append(f"- `{reason}`")
    else:
        summary_lines.append("- (none)")
    summary_lines.append("")
    (evidence_dir / "finalization_summary.md").write_text("\n".join(summary_lines), encoding="utf-8")

    print(json.dumps({"evidence_dir": str(evidence_dir), "decision": decision.get("decision", "HOLD")}, indent=2))
    return 0 if bool(decision.get("go", False)) else 2


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Finalize full-process audit and emit GO/HOLD decision artifacts")
    ap.add_argument("--evidence-dir", default="")
    ap.add_argument("--evidence-root", default="docs/audit")
    ap.add_argument("--fast-gate-artifact", default="")
    ap.add_argument("--shadow-artifact", default="")
    ap.add_argument("--rollback-validated", action="store_true", default=False)
    return ap


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
