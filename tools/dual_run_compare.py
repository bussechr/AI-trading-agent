from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists() or not path.is_file():
        return rows
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _id_of(row: dict[str, Any]) -> str:
    for key in ("command_id", "signal_id", "id"):
        val = str(row.get(key, "")).strip()
        if val:
            return val
    return ""


def _status_of(row: dict[str, Any]) -> str:
    for key in ("status", "event_status", "outcome"):
        val = str(row.get(key, "")).strip().lower()
        if val:
            return val
    return "unknown"


def _ts_of(row: dict[str, Any]) -> float:
    for key in ("ts", "time", "updated_at", "created_at"):
        try:
            return float(row.get(key))
        except Exception:
            continue
    return 0.0


def _terminal_rank(status: str) -> int:
    if status in {"acked", "failed", "expired"}:
        return 3
    if status in {"delivered", "queued"}:
        return 2
    if status in {"ok"}:
        return 1
    return 0


def _index_terminal(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        cid = _id_of(row)
        if not cid:
            continue
        status = _status_of(row)
        ranked = {
            "command_id": cid,
            "status": status,
            "ts": _ts_of(row),
            "symbol": str(row.get("symbol", "")),
            "cmd": str(row.get("cmd", "")),
            "raw": row,
        }
        prev = out.get(cid)
        if prev is None:
            out[cid] = ranked
            continue
        if _terminal_rank(status) > _terminal_rank(str(prev.get("status", ""))):
            out[cid] = ranked
            continue
        if _terminal_rank(status) == _terminal_rank(str(prev.get("status", ""))):
            if float(ranked.get("ts", 0.0)) >= float(prev.get("ts", 0.0)):
                out[cid] = ranked
    return out


@dataclass(slots=True)
class CompareReport:
    generated_at: str
    baseline_path: str
    candidate_path: str
    baseline_commands: int
    candidate_commands: int
    overlap_commands: int
    status_match_rate: float
    only_baseline: int
    only_candidate: int
    mismatches: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _compare(base_idx: dict[str, dict[str, Any]], cand_idx: dict[str, dict[str, Any]], mismatch_limit: int) -> CompareReport:
    base_ids = set(base_idx.keys())
    cand_ids = set(cand_idx.keys())
    overlap = sorted(base_ids & cand_ids)

    mismatches: list[dict[str, Any]] = []
    match_n = 0
    for cid in overlap:
        b = base_idx[cid]
        c = cand_idx[cid]
        if str(b.get("status", "")) == str(c.get("status", "")):
            match_n += 1
        else:
            if len(mismatches) < mismatch_limit:
                mismatches.append(
                    {
                        "command_id": cid,
                        "baseline_status": str(b.get("status", "unknown")),
                        "candidate_status": str(c.get("status", "unknown")),
                        "symbol": str(c.get("symbol") or b.get("symbol") or ""),
                        "cmd": str(c.get("cmd") or b.get("cmd") or ""),
                    }
                )

    return CompareReport(
        generated_at=_iso_now(),
        baseline_path="",
        candidate_path="",
        baseline_commands=len(base_ids),
        candidate_commands=len(cand_ids),
        overlap_commands=len(overlap),
        status_match_rate=float(match_n / max(len(overlap), 1)),
        only_baseline=len(base_ids - cand_ids),
        only_candidate=len(cand_ids - base_ids),
        mismatches=mismatches,
    )


def _render_markdown(report: CompareReport) -> str:
    lines = [
        "# Dual-Run Comparison",
        "",
        f"Generated at: `{report.generated_at}`",
        f"Baseline: `{report.baseline_path}`",
        f"Candidate: `{report.candidate_path}`",
        "",
        "## Summary",
        "",
        f"- Baseline command IDs: **{report.baseline_commands}**",
        f"- Candidate command IDs: **{report.candidate_commands}**",
        f"- Overlap IDs: **{report.overlap_commands}**",
        f"- Status match rate: **{report.status_match_rate:.3f}**",
        f"- Only baseline: **{report.only_baseline}**",
        f"- Only candidate: **{report.only_candidate}**",
        "",
        "## Status Mismatches",
        "",
    ]
    if report.mismatches:
        for row in report.mismatches:
            lines.append(
                f"- `{row.get('command_id')}`: baseline={row.get('baseline_status')} candidate={row.get('candidate_status')}"
            )
    else:
        lines.append("- (none)")
    lines.append("")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> int:
    baseline = Path(str(args.baseline))
    candidate = Path(str(args.candidate))
    out_dir = Path(str(args.out_dir))
    out_dir.mkdir(parents=True, exist_ok=True)

    base_rows = _load_jsonl(baseline)
    cand_rows = _load_jsonl(candidate)

    report = _compare(_index_terminal(base_rows), _index_terminal(cand_rows), mismatch_limit=int(args.mismatch_limit))
    report.baseline_path = str(baseline)
    report.candidate_path = str(candidate)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"dual_run_compare_{stamp}.json"
    md_path = out_dir / f"dual_run_compare_{stamp}.md"

    json_path.write_text(json.dumps(report.to_dict(), indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")

    print(f"Wrote compare JSON: {json_path}")
    print(f"Wrote compare MD:   {md_path}")
    print(f"Overlap IDs:        {report.overlap_commands}")
    print(f"Status match rate:  {report.status_match_rate:.3f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Compare dual-run trace artifacts (baseline vs candidate)")
    ap.add_argument("--baseline", required=True, help="Baseline JSONL trace path")
    ap.add_argument("--candidate", required=True, help="Candidate JSONL trace path")
    ap.add_argument("--out-dir", default="docs")
    ap.add_argument("--mismatch-limit", type=int, default=50)
    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
