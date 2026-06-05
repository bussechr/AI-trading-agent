"""LangGraph expression of the self-improvement research loop.

The user's stack calls for "LangGraph for stateful trading workflows". This module
runs the same propose -> dispose -> reflect cycle as :mod:`fxstack.improve.loop` but
as a checkpointed ``StateGraph``, giving per-node observability, durable state, and a
natural seam for human-approval interrupts. It reuses the exact shared primitives
(validate_change_set / apply_change_set / evaluate_config / score_metrics), so the
deterministic "code disposes" guarantees are identical. It is a thin alternative
runner -- the canonical loop in loop.py owns OOS guarding, campaigns, and factory
emission.
"""

from __future__ import annotations

import copy
from typing import Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from fxstack.improve.evaluator import build_synthetic_dataset, evaluate_config
from fxstack.improve.knobs import apply_change_set, default_config, knob_values, validate_change_set
from fxstack.improve.loop import _diff_change_set
from fxstack.improve.memory import change_set_signature
from fxstack.improve.objective import score_metrics
from fxstack.improve.proposer import (
    HeuristicProposer,
    ImprovementContext,
    LLMProposer,
    propose_with_fallback,
)
from fxstack.llm.client import build_llm_client


class ImproveState(TypedDict, total=False):
    iteration: int
    incumbent_config: dict[str, Any]
    incumbent_metrics: dict[str, Any]
    incumbent_objective: float
    baseline_objective: float
    accepted: int
    tried: list[str]
    entries: list[dict[str, Any]]
    best_change_set: dict[str, Any]
    proposal: dict[str, Any]
    fallback: str


class ImprovementGraph:
    """A checkpointed LangGraph runner for the improvement loop."""

    def __init__(
        self,
        *,
        dataset: Any | None = None,
        base_config: dict[str, Any] | None = None,
        settings: Any | None = None,
        llm_client: Any | None = None,
        min_trades: int | None = None,
        max_drawdown_pct: float | None = None,
        accept_margin: float | None = None,
        seed: int | None = None,
        max_iterations: int | None = None,
    ) -> None:
        if settings is None:
            from fxstack.settings import get_settings

            settings = get_settings()
        self.seed = int(seed if seed is not None else getattr(settings, "improve_seed", 1729))
        self.max_iterations = int(
            max_iterations if max_iterations is not None else getattr(settings, "improve_max_iterations", 12)
        )
        self.min_trades = int(min_trades if min_trades is not None else getattr(settings, "improve_min_trades", 30))
        self.max_drawdown_pct = float(
            max_drawdown_pct if max_drawdown_pct is not None else getattr(settings, "improve_max_drawdown_pct", 12.0)
        )
        self.accept_margin = float(
            accept_margin if accept_margin is not None else getattr(settings, "improve_accept_margin", 1e-6)
        )
        self.base_config = copy.deepcopy(base_config) if base_config is not None else default_config(settings)
        self.dataset = dataset if dataset is not None else build_synthetic_dataset(seed=self.seed)

        if llm_client is None:
            llm_client = build_llm_client(settings)
        self.llm_proposer = (
            LLMProposer(llm_client, temperature=float(getattr(settings, "llm_temperature", 0.4)))
            if getattr(llm_client, "backend", "null") != "null"
            else None
        )
        self.heuristic = HeuristicProposer()

        graph = StateGraph(ImproveState)
        graph.add_node("propose", self._propose)
        graph.add_node("dispose", self._dispose)
        graph.add_edge(START, "propose")
        graph.add_edge("propose", "dispose")
        graph.add_conditional_edges("dispose", self._route, {"propose": "propose", END: END})
        self._compiled = graph.compile(checkpointer=InMemorySaver())

    def _propose(self, state: ImproveState) -> dict[str, Any]:
        iteration = int(state.get("iteration", 0)) + 1
        ctx = ImprovementContext(
            incumbent_config=dict(state["incumbent_config"]),
            incumbent_metrics=dict(state.get("incumbent_metrics") or {}),
            incumbent_objective=float(state["incumbent_objective"]),
            iteration=iteration,
            seed=self.seed,
            recent_reflections=list(state.get("entries") or [])[-6:],
            tried_signatures=set(state.get("tried") or []),
        )
        proposal, fallback = propose_with_fallback(
            llm_proposer=self.llm_proposer, heuristic_proposer=self.heuristic, ctx=ctx
        )
        return {
            "iteration": iteration,
            "proposal": {
                "hypothesis": proposal.hypothesis,
                "change_set": proposal.change_set,
                "proposer": proposal.proposer,
            },
            "fallback": fallback,
        }

    def _dispose(self, state: ImproveState) -> dict[str, Any]:
        proposal = dict(state.get("proposal") or {})
        iteration = int(state.get("iteration", 0))
        inc_cfg = dict(state["incumbent_config"])
        inc_obj = float(state["incumbent_objective"])
        tried = list(state.get("tried") or [])
        entries = list(state.get("entries") or [])
        accepted = int(state.get("accepted", 0))

        sanitized = validate_change_set(proposal.get("change_set") or {}, incumbent=inc_cfg).sanitized
        sig = change_set_signature(sanitized)
        if not sanitized or sig in set(tried):
            entries.append({
                "iteration": iteration, "hypothesis": proposal.get("hypothesis", ""), "sanitized": sanitized,
                "objective": inc_obj, "accepted": False, "reason": "duplicate_or_empty_change_set",
                "proposer": proposal.get("proposer", ""),
            })
            tried.append(sig)
            return {"tried": tried, "entries": entries}

        candidate = apply_change_set(inc_cfg, sanitized)
        metrics = evaluate_config(candidate, self.dataset)
        score = score_metrics(metrics, min_trades=self.min_trades, max_drawdown_pct=self.max_drawdown_pct)
        accept = bool(score.passed_guardrails and score.objective > inc_obj + self.accept_margin)
        entries.append({
            "iteration": iteration, "hypothesis": proposal.get("hypothesis", ""), "sanitized": sanitized,
            "objective": score.objective, "accepted": accept,
            "reason": "accepted" if accept else ("rejected_guardrails" if not score.passed_guardrails else "rejected_no_improvement"),
            "proposer": proposal.get("proposer", ""),
        })
        tried.append(sig)
        update: dict[str, Any] = {"tried": tried, "entries": entries}
        if accept:
            update.update({
                "incumbent_config": candidate,
                "incumbent_metrics": metrics,
                "incumbent_objective": score.objective,
                "accepted": accepted + 1,
                "best_change_set": _diff_change_set(self.base_config, candidate),
            })
        return update

    def _route(self, state: ImproveState) -> str:
        return END if int(state.get("iteration", 0)) >= self.max_iterations else "propose"

    def run(self, *, thread_id: str = "improve") -> dict[str, Any]:
        base_metrics = evaluate_config(self.base_config, self.dataset)
        base_score = score_metrics(base_metrics, min_trades=self.min_trades, max_drawdown_pct=self.max_drawdown_pct)
        init: ImproveState = {
            "iteration": 0,
            "incumbent_config": copy.deepcopy(self.base_config),
            "incumbent_metrics": base_metrics,
            "incumbent_objective": base_score.objective,
            "baseline_objective": base_score.objective,
            "accepted": 0,
            "tried": [change_set_signature({})],
            "entries": [],
            "best_change_set": {},
        }
        config = {
            "configurable": {"thread_id": str(thread_id)},
            "recursion_limit": self.max_iterations * 3 + 10,
        }
        final = self._compiled.invoke(init, config=config)
        best_change_set = dict(final.get("best_change_set") or {})
        return {
            "runner": "langgraph",
            "iterations": self.max_iterations,
            "baseline_objective": float(final.get("baseline_objective", base_score.objective)),
            "best_objective": float(final.get("incumbent_objective", base_score.objective)),
            "improvement": float(final.get("incumbent_objective", base_score.objective)) - float(base_score.objective),
            "accepted": int(final.get("accepted", 0)),
            "best_change_set": best_change_set,
            "best_config_knobs": knob_values(dict(final.get("incumbent_config") or self.base_config)),
            "entries": list(final.get("entries") or []),
        }


def run_improvement_graph(
    *,
    dataset: Any | None = None,
    base_config: dict[str, Any] | None = None,
    settings: Any | None = None,
    llm_client: Any | None = None,
    seed: int | None = None,
    max_iterations: int | None = None,
    min_trades: int | None = None,
    max_drawdown_pct: float | None = None,
    accept_margin: float | None = None,
    thread_id: str = "improve",
) -> dict[str, Any]:
    """Run the improvement loop as a checkpointed LangGraph workflow."""

    graph = ImprovementGraph(
        dataset=dataset, base_config=base_config, settings=settings, llm_client=llm_client,
        seed=seed, max_iterations=max_iterations, min_trades=min_trades,
        max_drawdown_pct=max_drawdown_pct, accept_margin=accept_margin,
    )
    return graph.run(thread_id=thread_id)
