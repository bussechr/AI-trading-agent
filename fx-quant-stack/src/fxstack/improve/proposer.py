"""Strategy proposers -- the "LLM proposes" half of the loop.

Two implementations share one interface:

* :class:`LLMProposer` asks a local model for a schema-constrained change-set.
* :class:`HeuristicProposer` does deterministic guided local search and needs no
  model -- it is the offline/CI default and the per-iteration fallback whenever the
  LLM is unavailable or returns nothing usable.

Neither proposer decides anything: they emit a *candidate* change-set that the
allowlist sanitizes and the evaluator/loop judges.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fxstack.improve.knobs import KNOBS_BY_NAME, all_knobs, knob_values
from fxstack.improve.memory import change_set_signature
from fxstack.llm.client import LLMClient, LLMUnavailable
from fxstack.utils.hashing import hash_mapping


class ProposedChangeSet(BaseModel):
    """Schema the local model must fill (the structured-output contract)."""

    model_config = ConfigDict(extra="forbid")

    hypothesis: str = Field(description="One sentence: what edge this change should capture and why")
    change_set: dict[str, float] = Field(
        default_factory=dict,
        description="Map of allowlisted knob name -> proposed value",
    )


@dataclass(slots=True)
class ImprovementContext:
    incumbent_config: dict[str, Any]
    incumbent_metrics: dict[str, Any]
    incumbent_objective: float
    iteration: int
    seed: int
    recent_reflections: list[dict[str, Any]] = field(default_factory=list)
    tried_signatures: set[str] = field(default_factory=set)


@dataclass(slots=True)
class Proposal:
    hypothesis: str
    change_set: dict[str, float]
    proposer: str
    model_id: str = ""
    prompt_hash: str = ""


def _weak_metric(metrics: dict[str, Any]) -> str:
    sharpe = float(metrics.get("sharpe", 0.0) or 0.0)
    trades = float(metrics.get("trades", 0.0) or 0.0)
    win = float(metrics.get("win_rate", 0.0) or 0.0)
    if trades < 50:
        return "trades"
    if win < 0.5:
        return "win_rate"
    if sharpe < 1.0:
        return "sharpe"
    return "sharpe"


class HeuristicProposer:
    """Deterministic local search over the knob lattice.

    Proposes the next *untried* single-knob neighbour of the incumbent, rotating
    knobs/directions by iteration. When all neighbours are exhausted it makes a
    seeded multi-knob jump to escape local optima. Fully reproducible for a fixed
    seed + dataset, which is what makes the loop testable.
    """

    name = "heuristic"

    def propose(self, ctx: ImprovementContext) -> Proposal:
        values = knob_values(ctx.incumbent_config)
        knobs = list(all_knobs())
        weak = _weak_metric(ctx.incumbent_metrics)

        # Neighbour candidates: each knob nudged +/- one step from the incumbent.
        neighbours: list[tuple[str, float]] = []
        for knob in knobs:
            base = values.get(knob.name)
            if base is None:
                continue
            for sign in (+1.0, -1.0):
                cand = knob.coerce(float(base) + sign * knob.step)
                if float(cand) == float(base):
                    continue
                neighbours.append((knob.name, float(cand)))

        # Bias the rotation: when starved for trades, try the trade-count levers first.
        def _priority(item: tuple[str, float]) -> int:
            name, cand = item
            base = float(values.get(name, cand))
            looser = cand < base if name.startswith("min_") else cand > base
            if weak == "trades" and name.startswith("min_") and looser:
                return 0
            return 1

        neighbours.sort(key=lambda it: (_priority(it), it[0], it[1]))

        untried = [
            (name, cand)
            for (name, cand) in neighbours
            if change_set_signature({name: cand}) not in ctx.tried_signatures
        ]
        if untried:
            idx = int(ctx.iteration) % len(untried)
            name, cand = untried[idx]
            base = float(values.get(name, cand))
            direction = "raise" if cand > base else "lower"
            return Proposal(
                hypothesis=f"{direction} {name} from {base:g} to {cand:g} to improve {weak}",
                change_set={name: cand},
                proposer=self.name,
            )

        # All single-knob neighbours tried -> seeded multi-knob jump.
        rng = random.Random(int(ctx.seed) * 1_000_003 + int(ctx.iteration))
        pick = rng.sample(knobs, k=min(2, len(knobs)))
        change_set: dict[str, float] = {}
        for knob in pick:
            base = float(values.get(knob.name, knob.lo))
            sign = rng.choice((+1.0, -1.0))
            change_set[knob.name] = float(knob.coerce(base + sign * knob.step))
        return Proposal(
            hypothesis=f"explore multi-knob jump on {', '.join(sorted(change_set))} to escape local optimum",
            change_set=change_set,
            proposer=self.name,
        )


def _render_knob_catalog() -> str:
    lines = []
    for k in all_knobs():
        lock = " [risk-locked: tighten only]" if k.risk_locked else ""
        lines.append(f"- {k.name}: range [{k.lo}, {k.hi}] step {k.step} ({k.kind}){lock} -- {k.description}")
    return "\n".join(lines)


class LLMProposer:
    """Asks a local model for a schema-constrained change-set."""

    name = "llm"

    def __init__(self, client: LLMClient, *, temperature: float = 0.4) -> None:
        self._client = client
        self._temperature = float(temperature)

    def _prompt(self, ctx: ImprovementContext) -> str:
        values = knob_values(ctx.incumbent_config)
        reflections = ctx.recent_reflections[-6:]
        refl_lines = [
            f"- iter {r.get('iteration')}: {r.get('hypothesis')} -> "
            f"objective={float(r.get('objective', 0.0)):.4f} accepted={bool(r.get('accepted'))}"
            for r in reflections
        ]
        return (
            "You tune an FX trading strategy. Propose ONE small, testable change to the "
            "allowlisted knobs below to improve the risk-adjusted objective (Sharpe-like). "
            "Change at most two knobs. Risk-locked knobs may only be tightened.\n\n"
            f"Current knob values:\n{values}\n\n"
            f"Current metrics: {ctx.incumbent_metrics}\n"
            f"Current objective: {ctx.incumbent_objective:.4f}\n\n"
            f"Knob catalog:\n{_render_knob_catalog()}\n\n"
            f"Recent experiment history:\n" + ("\n".join(refl_lines) if refl_lines else "(none)") + "\n\n"
            "Do not repeat a change-set that was already tried and rejected."
        )

    def propose(self, ctx: ImprovementContext) -> Proposal:
        prompt = self._prompt(ctx)
        result = self._client.generate_structured(
            schema=ProposedChangeSet,
            prompt=prompt,
            system="You are a careful quantitative strategist. Output strict JSON only.",
            seed=int(ctx.seed) + int(ctx.iteration),
            temperature=self._temperature,
        )
        change_set = {
            str(k): float(v)
            for k, v in dict(result.change_set or {}).items()
            if str(k) in KNOBS_BY_NAME
        }
        if not change_set:
            raise LLMUnavailable("model returned no allowlisted knobs")
        prompt_hash = hash_mapping({"prompt": prompt, "iteration": int(ctx.iteration)})
        return Proposal(
            hypothesis=str(result.hypothesis or "llm proposal"),
            change_set=change_set,
            proposer=self.name,
            model_id=str(getattr(self._client, "model", "")),
            prompt_hash=prompt_hash,
        )


def propose_with_fallback(
    *,
    llm_proposer: LLMProposer | None,
    heuristic_proposer: HeuristicProposer,
    ctx: ImprovementContext,
) -> tuple[Proposal, str]:
    """Try the LLM proposer; fall back to heuristic on any unavailability.

    Returns ``(proposal, fallback_reason)`` where ``fallback_reason`` is empty when
    the LLM proposer succeeded.
    """

    if llm_proposer is not None:
        try:
            return llm_proposer.propose(ctx), ""
        except LLMUnavailable as exc:
            return heuristic_proposer.propose(ctx), f"llm_unavailable: {exc}"
        except Exception as exc:  # defensive: never let a bad model break the loop
            return heuristic_proposer.propose(ctx), f"llm_error: {exc}"
    return heuristic_proposer.propose(ctx), ""
