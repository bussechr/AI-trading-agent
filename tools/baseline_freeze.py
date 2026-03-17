from __future__ import annotations

import argparse
import importlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _count_jsonl_rows(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    n = 0
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _collect_audit_inventory(audit_dir: Path) -> list[dict[str, Any]]:
    if not audit_dir.exists() or not audit_dir.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for p in sorted(audit_dir.rglob("*.jsonl")):
        rows.append(
            {
                "path": str(p),
                "rows": int(_count_jsonl_rows(p)),
                "bytes": int(p.stat().st_size),
            }
        )
    return rows


def _collect_contract_matrix() -> list[dict[str, Any]]:
    bridge = importlib.import_module("bridge_api.bridge")
    app = bridge.app

    matrix: list[dict[str, Any]] = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: str(r.rule)):
        route = str(rule.rule)
        if route.startswith("/static"):
            continue
        methods = sorted(m for m in set(rule.methods or set()) if m not in {"HEAD", "OPTIONS"})
        matrix.append(
            {
                "route": route,
                "methods": methods,
                "endpoint": str(rule.endpoint),
            }
        )
    return matrix


def _collect_runtime_kpis(db_path: Path) -> dict[str, Any]:
    if not db_path.exists() or not db_path.is_file():
        return {
            "db_path": str(db_path),
            "db_exists": False,
            "commands": {},
            "pending": 0,
            "decision_snapshots": 0,
            "governance_events": 0,
        }

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        commands = {
            str(r["status"]): int(r["n"])
            for r in con.execute("SELECT status, COUNT(*) AS n FROM commands GROUP BY status").fetchall()
        }
        pending = con.execute(
            "SELECT COUNT(*) AS n FROM commands WHERE status IN ('queued','delivered')"
        ).fetchone()
        decisions = con.execute("SELECT COUNT(*) AS n FROM decision_snapshots").fetchone()
        governance = con.execute("SELECT COUNT(*) AS n FROM governance_events").fetchone()

        return {
            "db_path": str(db_path),
            "db_exists": True,
            "commands": commands,
            "pending": int((pending["n"] if pending else 0) or 0),
            "decision_snapshots": int((decisions["n"] if decisions else 0) or 0),
            "governance_events": int((governance["n"] if governance else 0) or 0),
            "commands_total": int(sum(commands.values())),
            "acked_rate": float(commands.get("acked", 0) / max(sum(commands.values()), 1)),
        }
    finally:
        con.close()


@dataclass(slots=True)
class BaselineReport:
    generated_at: str
    runtime_db: str
    audit_dir: str
    contract_matrix: list[dict[str, Any]]
    kpis: dict[str, Any]
    audit_inventory: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)



def _render_markdown(report: BaselineReport) -> str:
    cmds = dict(report.kpis.get("commands", {}) or {})
    lines = [
        "# Runtime Baseline Freeze",
        "",
        f"Generated at: `{report.generated_at}`",
        f"Runtime DB: `{report.runtime_db}`",
        f"Audit dir: `{report.audit_dir}`",
        "",
        "## KPI Snapshot",
        "",
        f"- Commands total: **{int(report.kpis.get('commands_total', 0))}**",
        f"- Pending (queued+delivered): **{int(report.kpis.get('pending', 0))}**",
        f"- Acked rate: **{float(report.kpis.get('acked_rate', 0.0)):.3f}**",
        f"- Decision snapshots: **{int(report.kpis.get('decision_snapshots', 0))}**",
        f"- Governance events: **{int(report.kpis.get('governance_events', 0))}**",
        "",
        "### Command Status Counts",
        "",
    ]
    if cmds:
        for k in sorted(cmds.keys()):
            lines.append(f"- `{k}`: {int(cmds[k])}")
    else:
        lines.append("- (none)")

    lines.extend(["", "## HTTP Contract Matrix", ""])
    if report.contract_matrix:
        for row in report.contract_matrix:
            methods = ",".join(row.get("methods", []))
            lines.append(f"- `{methods}` {row.get('route')} -> `{row.get('endpoint')}`")
    else:
        lines.append("- (none)")

    lines.extend(["", "## Audit Inventory", ""])
    if report.audit_inventory:
        for row in report.audit_inventory:
            lines.append(
                f"- `{row.get('path')}`: rows={int(row.get('rows', 0))}, bytes={int(row.get('bytes', 0))}"
            )
    else:
        lines.append("- (none)")

    lines.append("")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    db_path = Path(str(args.db_path))
    audit_dir = Path(str(args.audit_dir))
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    report = BaselineReport(
        generated_at=_iso_now(),
        runtime_db=str(db_path),
        audit_dir=str(audit_dir),
        contract_matrix=_collect_contract_matrix(),
        kpis=_collect_runtime_kpis(db_path),
        audit_inventory=_collect_audit_inventory(audit_dir),
    )

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"baseline_freeze_{stamp}.json"
    md_path = out_dir / f"baseline_freeze_{stamp}.md"

    payload = report.to_dict()
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    print(f"Wrote baseline JSON: {json_path}")
    print(f"Wrote baseline MD:   {md_path}")
    print(f"routes frozen:       {len(report.contract_matrix)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Generate baseline KPI and HTTP contract freeze artifacts")
    ap.add_argument("--db-path", default="data/state/runtime.db")
    ap.add_argument("--audit-dir", default="data/state/audit")
    ap.add_argument("--out-dir", default="docs")
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
