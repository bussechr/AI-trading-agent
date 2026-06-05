"""The self-improvement loop driver.

Closes the cycle the rest of the stack was built for:

    propose (LLM or heuristic)  ->  validate against the safety allowlist
      ->  apply  ->  backtest  ->  score + guardrail gate  ->  accept/reject
      ->  reflect (memory)  ->  repeat  ->  emit a Phase-7 ExperimentProposal

The proposer is the only non-deterministic actor, and even it is fenced: every
value passes through ``validate_change_set`` and every candidate must beat the
incumbent *and* clear hard guardrails before it is accepted. Run offline with the
heuristic proposer and it is fully reproducible for a fixed seed + dataset.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from uuid import NAMESPACE_URL, uuid5

import pandas as pd

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
from fxstack.improve.knobs import (
    all_knobs,
    apply_change_set,
    default_config,
    knob_values,
    validate_change_set,
)
from fxstack.improve.memory import ReflectionEntry, ReflectionMemory, change_set_signature
from fxstack.improve.objective import score_metrics
from fxstack.improve.proposer import (
    HeuristicProposer,
    ImprovementContext,
    LLMProposer,
    Proposal,
    propose_with_fallback,
)
from fxstack.llm.client import LLMClient, build_llm_client
from fxstack.orchestration.contracts import ExperimentProposal


@dataclass(slots=True)
class ImprovementResult:
    best_config: dict[str, Any]
    best_change_set: dict[str, float]
    best_objective: float
    best_metrics: dict[str, float]
    baseline_objective: float
    baseline_metrics: dict[str, float]
    iterations: int
    accepted: int
    proposer_usage: dict[str, int]
    fallback_count: int
    summary: dict[str, Any]
    artifact_dir: str = ""
    experiment_proposal: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "best_change_set": dict(self.best_change_set),
            "best_objective": float(self.best_objective),
            "best_metrics": dict(self.best_metrics),
            "baseline_objective": float(self.baseline_objective),
            "baseline_metrics": dict(self.baseline_metrics),
            "iterations": int(self.iterations),
            "accepted": int(self.accepted),
            "proposer_usage": dict(self.proposer_usage),
            "fallback_count": int(self.fallback_count),
            "improvement": float(self.best_objective - self.baseline_objective),
            "artifact_dir": str(self.artifact_dir),
            "experiment_proposal": self.experiment_proposal,
            "summary": dict(self.summary),
        }


def _diff_change_set(base_config: dict[str, Any], config: dict[str, Any]) -> dict[str, float]:
    base_values = knob_values(base_config)
    cur_values = knob_values(config)
    out: dict[str, float] = {}
    for name, value in cur_values.items():
        if name not in base_values or float(base_values[name]) != float(value):
            out[name] = value
    return out


def _write_json(path: Path, payload: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return str(path)


def build_experiment_proposal(
    *,
    experiment_id: str,
    base_config: dict[str, Any],
    best_config: dict[str, Any],
    best_entry: ReflectionEntry | None,
    seed: int,
    evaluation_plan: dict[str, Any],
    evidence_refs: list[str],
) -> ExperimentProposal:
    """Render a contract-valid Phase-7 ExperimentProposal from the loop result."""

    base_values = knob_values(base_config)
    change_set = [
        {"knob": name, "from": base_values.get(name), "to": value}
        for name, value in sorted(_diff_change_set(base_config, best_config).items())
    ]
    risk_notes = [
        "All knob edits passed the deterministic change-set allowlist (validate_change_set).",
        "Risk-locked caps may only tighten vs the incumbent; loosening is blocked at apply time.",
        "Promotion guardrails: min_trades and max_drawdown_pct enforced by objective.score_metrics.",
    ]
    hypothesis = (best_entry.hypothesis if best_entry else "") or "No improving change found; incumbent retained."
    return ExperimentProposal(
        experiment_id=uuid5(NAMESPACE_URL, f"fxstack.improve:{experiment_id}"),
        source_run_id=None,
        hypothesis=str(hypothesis),
        change_set=change_set,
        evaluation_plan=dict(evaluation_plan),
        risk_notes=risk_notes,
        evidence_refs=[str(r) for r in evidence_refs if str(r).strip()],
        prompt_hash=str(best_entry.prompt_hash if best_entry else ""),
        model_id=str(best_entry.model_id if best_entry else ""),
        decision_seed=int(seed),
        config_diff={str(k): v for k, v in _diff_change_set(base_config, best_config).items()},
        approval_status="draft",
        latest_stage="proposed",
    )


def _non_duplicate_sanitized(
    *,
    proposal: Proposal,
    incumbent_config: dict[str, Any],
    heuristic: HeuristicProposer,
    base_ctx: ImprovementContext,
    tried: set[str],
) -> tuple[dict[str, float], Proposal]:
    """Return a sanitized, non-duplicate change-set, escalating to the heuristic."""

    result = validate_change_set(proposal.change_set, incumbent=incumbent_config)
    sanitized = result.sanitized
    sig = change_set_signature(sanitized)
    if sanitized and sig not in tried:
        return sanitized, proposal

    # Escalate: ask the heuristic for fresh untried neighbours.
    for k in range(1, len(all_knobs()) + 2):
        alt_ctx = ImprovementContext(
            incumbent_config=incumbent_config,
            incumbent_metrics=base_ctx.incumbent_metrics,
            incumbent_objective=base_ctx.incumbent_objective,
            iteration=base_ctx.iteration + k,
            seed=base_ctx.seed,
            recent_reflections=base_ctx.recent_reflections,
            tried_signatures=tried,
        )
        alt = heuristic.propose(alt_ctx)
        alt_sanitized = validate_change_set(alt.change_set, incumbent=incumbent_config).sanitized
        alt_sig = change_set_signature(alt_sanitized)
        if alt_sanitized and alt_sig not in tried:
            return alt_sanitized, alt
    return sanitized, proposal


def run_improvement_loop(
    *,
    dataset: pd.DataFrame | None = None,
    base_config: dict[str, Any] | None = None,
    settings: Any | None = None,
    llm_client: LLMClient | None = None,
    memory_path: str | Path | None = None,
    iterations: int | None = None,
    seed: int | None = None,
    min_trades: int | None = None,
    max_drawdown_pct: float | None = None,
    accept_margin: float | None = None,
    artifact_dir: str | Path | None = None,
    emit_experiment: bool = True,
    experiment_id: str = "",
    now: Callable[[], datetime] | None = None,
) -> ImprovementResult:
    if settings is None:
        from fxstack.settings import get_settings

        settings = get_settings()

    iterations = int(iterations if iterations is not None else getattr(settings, "improve_max_iterations", 12))
    seed = int(seed if seed is not None else getattr(settings, "improve_seed", 1729))
    min_trades = int(min_trades if min_trades is not None else getattr(settings, "improve_min_trades", 30))
    max_drawdown_pct = float(
        max_drawdown_pct if max_drawdown_pct is not None else getattr(settings, "improve_max_drawdown_pct", 12.0)
    )
    accept_margin = float(
        accept_margin if accept_margin is not None else getattr(settings, "improve_accept_margin", 1e-6)
    )
    clock = now or (lambda: datetime.now(UTC))

    base_config = copy.deepcopy(base_config) if base_config is not None else default_config(settings)
    dataset_source = "provided"
    if dataset is None:
        dataset = build_synthetic_dataset(seed=seed)
        dataset_source = "synthetic"

    memory = ReflectionMemory(memory_path)
    if llm_client is None:
        llm_client = build_llm_client(settings)
    llm_proposer = LLMProposer(llm_client, temperature=float(getattr(settings, "llm_temperature", 0.4))) \
        if getattr(llm_client, "backend", "null") != "null" else None
    heuristic = HeuristicProposer()

    # Resume from prior best if memory carries one.
    incumbent_config = copy.deepcopy(base_config)
    resumed_best = memory.best()
    if resumed_best is not None and resumed_best.sanitized:
        incumbent_config = apply_change_set(
            base_config, validate_change_set(resumed_best.sanitized, incumbent=base_config).sanitized
        )

    baseline_metrics = evaluate_config(base_config, dataset)
    baseline_score = score_metrics(baseline_metrics, min_trades=min_trades, max_drawdown_pct=max_drawdown_pct)
    incumbent_metrics = evaluate_config(incumbent_config, dataset)
    incumbent_score = score_metrics(incumbent_metrics, min_trades=min_trades, max_drawdown_pct=max_drawdown_pct)

    tried: set[str] = set(memory.tried_signatures())
    tried.add(change_set_signature({}))
    if not memory.entries():
        memory.append(
            ReflectionEntry(
                iteration=0,
                hypothesis="baseline incumbent",
                change_set={},
                sanitized={},
                objective=incumbent_score.objective,
                accepted=True,
                reason="baseline",
                metrics=incumbent_metrics,
                proposer="baseline",
                ts=clock().isoformat(),
            )
        )

    proposer_usage = {"llm": 0, "heuristic": 0, "baseline": 0}
    fallback_count = 0
    accepted = 0
    best_entry: ReflectionEntry | None = None

    for i in range(1, iterations + 1):
        ctx = ImprovementContext(
            incumbent_config=incumbent_config,
            incumbent_metrics=incumbent_metrics,
            incumbent_objective=incumbent_score.objective,
            iteration=i,
            seed=seed,
            recent_reflections=[e.as_dict() for e in memory.recent(6)],
            tried_signatures=set(tried),
        )
        proposal, fallback_reason = propose_with_fallback(
            llm_proposer=llm_proposer, heuristic_proposer=heuristic, ctx=ctx
        )
        if fallback_reason:
            fallback_count += 1
        proposer_usage[proposal.proposer] = proposer_usage.get(proposal.proposer, 0) + 1

        sanitized, used_proposal = _non_duplicate_sanitized(
            proposal=proposal, incumbent_config=incumbent_config, heuristic=heuristic, base_ctx=ctx, tried=tried
        )
        sig = change_set_signature(sanitized)
        if not sanitized or sig in tried:
            memory.append(
                ReflectionEntry(
                    iteration=i, hypothesis=used_proposal.hypothesis, change_set=used_proposal.change_set,
                    sanitized=sanitized, objective=incumbent_score.objective, accepted=False,
                    reason="duplicate_or_empty_change_set", metrics=incumbent_metrics,
                    proposer=used_proposal.proposer, model_id=used_proposal.model_id,
                    prompt_hash=used_proposal.prompt_hash, ts=clock().isoformat(),
                )
            )
            tried.add(sig)
            continue

        candidate_config = apply_change_set(incumbent_config, sanitized)
        metrics = evaluate_config(candidate_config, dataset)
        score = score_metrics(metrics, min_trades=min_trades, max_drawdown_pct=max_drawdown_pct)
        improved = score.objective > incumbent_score.objective + accept_margin
        accept = bool(score.passed_guardrails and improved)
        if accept:
            reason = f"accepted: objective {incumbent_score.objective:.4f} -> {score.objective:.4f}"
        elif not score.passed_guardrails:
            reason = "rejected_guardrails: " + ",".join(score.guardrail_failures)
        else:
            reason = f"rejected_no_improvement: {score.objective:.4f} <= {incumbent_score.objective:.4f}"
        if fallback_reason:
            reason = f"{reason} [{fallback_reason}]"

        entry = ReflectionEntry(
            iteration=i, hypothesis=used_proposal.hypothesis, change_set=used_proposal.change_set,
            sanitized=sanitized, objective=score.objective, accepted=accept, reason=reason,
            metrics=metrics, proposer=used_proposal.proposer, model_id=used_proposal.model_id,
            prompt_hash=used_proposal.prompt_hash, ts=clock().isoformat(),
        )
        memory.append(entry)
        tried.add(sig)
        if accept:
            accepted += 1
            incumbent_config = candidate_config
            incumbent_metrics = metrics
            incumbent_score = score
            if best_entry is None or score.objective > best_entry.objective:
                best_entry = entry

    best_change_set = _diff_change_set(base_config, incumbent_config)
    summary = {
        **memory.summary(),
        "baseline_objective": baseline_score.objective,
        "best_objective": incumbent_score.objective,
        "improvement": incumbent_score.objective - baseline_score.objective,
        "proposer_usage": dict(proposer_usage),
        "fallback_count": fallback_count,
        "llm_backend": getattr(llm_client, "backend", "null"),
    }

    artifact_dir_str = ""
    evidence_refs: list[str] = []
    experiment_proposal_payload: dict[str, Any] | None = None
    if artifact_dir is not None:
        out = Path(artifact_dir)
        out.mkdir(parents=True, exist_ok=True)
        artifact_dir_str = str(out)
        evidence_refs.append(_write_json(out / "best_config.json", incumbent_config))
        evidence_refs.append(_write_json(out / "summary.json", summary))
        if memory_path is None:
            with (out / "reflection_memory.jsonl").open("w", encoding="utf-8") as fh:
                for e in memory.entries():
                    fh.write(json.dumps(e.as_dict(), sort_keys=True) + "\n")
            evidence_refs.append(str(out / "reflection_memory.jsonl"))

    if emit_experiment:
        exp_id = str(experiment_id or f"improve-{change_set_signature(best_change_set)[:24]}")
        evaluation_plan = {
            "objective": "sharpe_like",
            "iterations": iterations,
            "rows": int(len(dataset)),
            "guardrails": {"min_trades": min_trades, "max_drawdown_pct": max_drawdown_pct},
            "dataset": dataset_source,
        }
        proposal_model = build_experiment_proposal(
            experiment_id=exp_id, base_config=base_config, best_config=incumbent_config,
            best_entry=best_entry, seed=seed, evaluation_plan=evaluation_plan, evidence_refs=evidence_refs,
        )
        experiment_proposal_payload = proposal_model.model_dump(mode="json")
        if artifact_dir is not None:
            out = Path(artifact_dir)
            _write_json(out / "proposal.json", experiment_proposal_payload)
            _write_json(
                out / "reflection_memory.json",
                memory.to_reflection_payload(experiment_id=exp_id, updated_at=clock().isoformat()),
            )

    return ImprovementResult(
        best_config=incumbent_config,
        best_change_set=best_change_set,
        best_objective=incumbent_score.objective,
        best_metrics=incumbent_metrics,
        baseline_objective=baseline_score.objective,
        baseline_metrics=baseline_metrics,
        iterations=iterations,
        accepted=accepted,
        proposer_usage=proposer_usage,
        fallback_count=fallback_count,
        summary=summary,
        artifact_dir=artifact_dir_str,
        experiment_proposal=experiment_proposal_payload,
    )
