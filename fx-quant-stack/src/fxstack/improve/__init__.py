# AGENT: ROLE: Self-improving research loop -- "the LLM proposes; deterministic code disposes".
# AGENT: ENTRYPOINT: `run_improvement_loop(...)` (CLI: `trader agent improve`).
# AGENT: PRIMARY INPUTS: scored-signals dataset (or synthetic), seed config, reflection memory.
# AGENT: PRIMARY OUTPUTS: best config, reflection memory, Phase-7 ExperimentProposal.
# AGENT: STATE / SIDE EFFECTS: writes artifacts under FXSTACK_IMPROVE_ARTIFACT_ROOT; no live execution.
# AGENT: GUARDRAILS: every proposed knob passes validate_change_set; risk caps may only tighten.
# AGENT: SEE: fxstack/improve/knobs.py (allowlist) ; fxstack/orchestration/experiments.py (factory)
from __future__ import annotations

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config, load_parquet_dataset
from fxstack.improve.knobs import (
    Knob,
    apply_change_set,
    default_config,
    knob_names,
    knob_values,
    validate_change_set,
)
from fxstack.improve.loop import (
    CampaignResult,
    ImprovementResult,
    run_improvement_campaign,
    run_improvement_loop,
)
from fxstack.improve.memory import ReflectionEntry, ReflectionMemory
from fxstack.improve.objective import CandidateScore, score_metrics
from fxstack.improve.proposer import HeuristicProposer, LLMProposer, Proposal, ProposedChangeSet

__all__ = [
    "Knob",
    "apply_change_set",
    "default_config",
    "knob_names",
    "knob_values",
    "validate_change_set",
    "build_synthetic_dataset",
    "evaluate_config",
    "load_parquet_dataset",
    "CandidateScore",
    "score_metrics",
    "ReflectionEntry",
    "ReflectionMemory",
    "HeuristicProposer",
    "LLMProposer",
    "Proposal",
    "ProposedChangeSet",
    "ImprovementResult",
    "CampaignResult",
    "run_improvement_loop",
    "run_improvement_campaign",
]
