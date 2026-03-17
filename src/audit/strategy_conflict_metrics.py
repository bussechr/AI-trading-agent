from __future__ import annotations

from typing import Any


def split_blockers(raw: Any) -> set[str]:
    if raw is None:
        return set()
    txt = str(raw).strip()
    if not txt or txt.lower() == "none":
        return set()
    out: set[str] = set()
    for part in txt.split(","):
        p = part.strip().lower()
        if p:
            out.add(p)
    return out


def throughput_suppression_ratio(rows: list[dict[str, Any]]) -> float:
    """
    Candidate opportunities suppressed before execution.
    0.0 means no suppression, 1.0 means full suppression.
    """
    cand_rows = [r for r in rows if str(r.get("phase", "")).lower() == "candidate"]
    if not cand_rows:
        return 0.0
    ready = sum(1 for r in cand_rows if bool(r.get("execution_ready", False)))
    return float(max(0.0, 1.0 - (ready / max(len(cand_rows), 1))))


def redundant_veto_index(rows: list[dict[str, Any]]) -> float:
    """
    Share of execution vetoes that duplicate entry-side blocker information.
    """
    exec_rejects = [
        r
        for r in rows
        if str(r.get("phase", "")).lower() == "execution"
        and str(r.get("outcome", "")).lower().startswith("rejected")
    ]
    if not exec_rejects:
        return 0.0

    redundant = 0
    for row in exec_rejects:
        reason = str(row.get("rejection_reason", "")).strip().lower()
        blockers = split_blockers(row.get("entry_blockers", row.get("blockers", "")))
        if reason == "exec_low_score_ratio" and (
            "low_score" in blockers or "soft_low_score" in blockers
        ):
            redundant += 1
            continue
        if reason == "exec_low_sharpe_ratio" and (
            "low_predictive_sharpe" in blockers
            or "soft_low_predictive_sharpe" in blockers
        ):
            redundant += 1
            continue
        if reason == "exec_low_confidence" and any(
            b in blockers for b in ("spread", "cost_gate", "soft_cost_gate", "heston_vol_guard")
        ):
            redundant += 1
            continue
    return float(redundant / max(len(exec_rejects), 1))


def component_nullification_index(rows: list[dict[str, Any]]) -> float:
    """
    Share of rows where non-trivial raw score is heavily damped before action.
    """
    cand_rows = [r for r in rows if str(r.get("phase", "")).lower() == "candidate"]
    if not cand_rows:
        return 0.0

    total = 0
    nullified = 0
    for row in cand_rows:
        score_raw = abs(float(row.get("score_raw", 0.0) or 0.0))
        score_eff = abs(float(row.get("score_effective", 0.0) or 0.0))
        if score_raw <= 1e-12:
            continue
        total += 1
        if score_eff <= max(score_raw * 0.35, 1e-12):
            nullified += 1
    if total <= 0:
        return 0.0
    return float(nullified / total)


def dead_zone_density(rows: list[dict[str, Any]]) -> float:
    """
    High-quality-looking rows that still fail execution readiness.
    Evaluated in (score_ratio, sharpe_ratio, confidence_exec) space.
    """
    cand_rows = [r for r in rows if str(r.get("phase", "")).lower() == "candidate"]
    if not cand_rows:
        return 0.0

    in_zone = []
    for row in cand_rows:
        score_ratio = float(row.get("score_ratio", 0.0) or 0.0)
        sharpe_ratio = float(row.get("sharpe_ratio", 0.0) or 0.0)
        conf_exec = float(row.get("confidence_exec", row.get("confidence", 0.0)) or 0.0)
        if score_ratio >= 0.80 and sharpe_ratio >= 0.80 and conf_exec >= 40.0:
            in_zone.append(row)

    if not in_zone:
        return 0.0
    blocked = sum(1 for row in in_zone if not bool(row.get("execution_ready", False)))
    return float(blocked / max(len(in_zone), 1))


def summarize_trace_metrics(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {
        "throughput_suppression_ratio": float(throughput_suppression_ratio(rows)),
        "redundant_veto_index": float(redundant_veto_index(rows)),
        "component_nullification_index": float(component_nullification_index(rows)),
        "dead_zone_density": float(dead_zone_density(rows)),
    }
