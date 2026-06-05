"""Natural-language explanation of a self-improvement run.

Realizes the "the agent can summarise and explain decisions" half of the goal,
without ever letting the model decide anything. A deterministic digest is computed
from the run's own artifacts (summary + reflection memory); a local LLM may narrate
it, and if no model is available a deterministic template renders the same facts.
The numbers always come from code, never from the model.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from fxstack.llm.client import LLMClient, LLMUnavailable, build_llm_client


class RunExplanation(BaseModel):
    """Schema the local model fills when narrating a run."""

    model_config = ConfigDict(extra="forbid")

    narrative: str = Field(description="2-4 sentence plain-language summary of the run")
    key_findings: list[str] = Field(default_factory=list, description="Bullet findings grounded in the digest")
    risk_notes: list[str] = Field(default_factory=list, description="Any risk/caution notes")


def _fmt(value: Any) -> str:
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return "n/a"


def build_digest(summary: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic, code-computed digest of a run (no model involved)."""

    entries = [dict(e or {}) for e in (entries or [])]
    accepted = [e for e in entries if e.get("accepted") and int(e.get("iteration", 0) or 0) > 0]
    overfit = [e for e in entries if "rejected_overfit" in str(e.get("reason", ""))]
    guardrail = [e for e in entries if str(e.get("reason", "")).startswith("rejected_guardrails")]
    no_improve = [e for e in entries if "rejected_no_improvement" in str(e.get("reason", ""))]

    final_changes: dict[str, Any] = {}
    for e in accepted:
        for k, v in dict(e.get("sanitized") or {}).items():
            final_changes[k] = v

    return {
        "iterations": summary.get("iterations"),
        "baseline_objective": summary.get("baseline_objective"),
        "best_objective": summary.get("best_objective"),
        "improvement": summary.get("improvement"),
        "accepted_count": len(accepted),
        "overfit_rejections": len(overfit),
        "guardrail_rejections": len(guardrail),
        "no_improvement_rejections": len(no_improve),
        "oos_fraction": summary.get("oos_fraction"),
        "incumbent_oos_objective": summary.get("incumbent_oos_objective"),
        "proposer_usage": summary.get("proposer_usage"),
        "fallback_count": summary.get("fallback_count"),
        "llm_backend": summary.get("llm_backend"),
        "best_change_set": dict(summary.get("best_change_set") or final_changes),
        "final_changes": final_changes,
    }


def render_template(digest: dict[str, Any]) -> str:
    """Deterministic prose from a digest -- always available, no model needed."""

    imp = digest.get("improvement")
    try:
        improved = float(imp) > 0
    except (TypeError, ValueError):
        improved = False
    direction = "improved" if improved else "did not improve"
    lines = [
        f"The self-improvement loop ran {digest.get('iterations')} iterations and {direction} the "
        f"risk-adjusted objective from {_fmt(digest.get('baseline_objective'))} to "
        f"{_fmt(digest.get('best_objective'))} (delta {_fmt(imp)}).",
        f"It accepted {digest.get('accepted_count')} change(s); "
        f"{digest.get('overfit_rejections')} candidate(s) were rejected for failing out-of-sample, "
        f"{digest.get('guardrail_rejections')} for guardrail violations.",
    ]
    changes = dict(digest.get("best_change_set") or {})
    if changes:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(changes.items()))
        lines.append(f"Net configuration change versus baseline: {rendered}.")
    else:
        lines.append("No configuration change beat the incumbent, so the baseline was retained.")
    lines.append(
        f"Proposer usage was {digest.get('proposer_usage')} with {digest.get('fallback_count')} LLM fallback(s); "
        f"all changes passed the deterministic safety allowlist."
    )
    return " ".join(lines)


def explain_run(
    *,
    summary: dict[str, Any],
    entries: list[dict[str, Any]] | None = None,
    llm_client: LLMClient | None = None,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Explain a run. Uses the local LLM when available, else a deterministic template."""

    digest = build_digest(dict(summary or {}), list(entries or []))
    template = render_template(digest)

    if llm_client is None:
        llm_client = build_llm_client(settings)

    if getattr(llm_client, "backend", "null") != "null":
        prompt = (
            "Explain this FX strategy self-improvement run in 2-4 sentences for an operator. "
            "Use ONLY the numbers in this digest; do not invent figures. Be precise and sober.\n\n"
            f"Digest: {digest}\n\n"
            f"A deterministic baseline summary you may refine (do not contradict its numbers):\n{template}"
        )
        try:
            result = llm_client.generate_structured(
                schema=RunExplanation,
                prompt=prompt,
                system="You are a careful quantitative analyst. Output strict JSON only.",
            )
            return {
                "narrative": str(result.narrative or template),
                "key_findings": list(result.key_findings or []),
                "risk_notes": list(result.risk_notes or []),
                "source": "llm",
                "model_id": str(getattr(llm_client, "model", "")),
                "digest": digest,
            }
        except LLMUnavailable:
            pass
        except Exception:
            pass

    return {
        "narrative": template,
        "key_findings": [],
        "risk_notes": [],
        "source": "template",
        "model_id": "",
        "digest": digest,
    }
