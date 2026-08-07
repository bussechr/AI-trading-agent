# AGENT: ROLE: Self-improving research loop -- "the LLM proposes; deterministic code disposes".
# AGENT: ENTRYPOINT: `run_improvement_loop(...)` (CLI: `trader agent improve`).
# AGENT: PRIMARY INPUTS: scored-signals dataset (or synthetic), seed config, reflection memory.
# AGENT: PRIMARY OUTPUTS: best config, reflection memory, and research-only proposal evidence.
# AGENT: STATE / SIDE EFFECTS: writes offline artifacts only; no runtime DB, factory registration, or live execution.
# AGENT: GUARDRAILS: every proposed knob passes validate_change_set; risk caps may only tighten.
# AGENT: ISOLATION: no production ops launcher or runtime-service crossover; transfer evidence explicitly for independent candidate validation.
# AGENT: SEE: fxstack/improve/knobs.py (allowlist) ; docs/agents/causal-research-and-runtime-validation.md
from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "Knob": "fxstack.improve.knobs",
    "apply_change_set": "fxstack.improve.knobs",
    "default_config": "fxstack.improve.knobs",
    "knob_names": "fxstack.improve.knobs",
    "knob_values": "fxstack.improve.knobs",
    "validate_change_set": "fxstack.improve.knobs",
    "build_synthetic_dataset": "fxstack.improve.evaluator",
    "evaluate_config": "fxstack.improve.evaluator",
    "load_parquet_dataset": "fxstack.improve.evaluator",
    "CandidateScore": "fxstack.improve.objective",
    "score_metrics": "fxstack.improve.objective",
    "ReflectionEntry": "fxstack.improve.memory",
    "ReflectionMemory": "fxstack.improve.memory",
    "HeuristicProposer": "fxstack.improve.proposer",
    "LLMProposer": "fxstack.improve.proposer",
    "Proposal": "fxstack.improve.proposer",
    "ProposedChangeSet": "fxstack.improve.proposer",
    "ImprovementResult": "fxstack.improve.loop",
    "CampaignResult": "fxstack.improve.loop",
    "run_improvement_loop": "fxstack.improve.loop",
    "run_improvement_campaign": "fxstack.improve.loop",
    "RunExplanation": "fxstack.improve.explain",
    "build_digest": "fxstack.improve.explain",
    "explain_run": "fxstack.improve.explain",
    "render_template": "fxstack.improve.explain",
    "ImprovementGraph": "fxstack.improve.graph",
    "run_improvement_graph": "fxstack.improve.graph",
    "robustness_report": "fxstack.improve.robustness",
    "ColumnMap": "fxstack.improve.dataset_builder",
    "build_scored_signals": "fxstack.improve.dataset_builder",
    "build_from_parquet": "fxstack.improve.dataset_builder",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
