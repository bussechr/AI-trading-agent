"""Offline-safe run explainer: deterministic template + optional LLM narration."""

from __future__ import annotations

from fxstack.improve.explain import RunExplanation, build_digest, explain_run, render_template
from fxstack.improve.loop import run_improvement_loop
from fxstack.llm.client import LLMClient


class _ScriptedClient(LLMClient):
    backend = "scripted"

    def __init__(self, reply: str) -> None:
        super().__init__(model="scripted-model", base_url="", max_retries=1)
        self._reply = reply

    def health(self):  # pragma: no cover - unused
        raise NotImplementedError

    def _complete_json(self, *, system, prompt, seed, temperature):
        return self._reply


def _summary():
    return {
        "iterations": 10,
        "baseline_objective": -1.0,
        "best_objective": -0.4,
        "improvement": 0.6,
        "accepted": 3,
        "oos_fraction": 0.3,
        "incumbent_oos_objective": -0.3,
        "proposer_usage": {"llm": 0, "heuristic": 10, "baseline": 0},
        "fallback_count": 0,
        "best_change_set": {"min_entry_prob": 0.61},
        "llm_backend": "null",
    }


def _entries():
    return [
        {"iteration": 0, "accepted": True, "reason": "baseline", "sanitized": {}},
        {"iteration": 1, "accepted": True, "reason": "accepted: ...", "sanitized": {"min_entry_prob": 0.61}},
        {"iteration": 2, "accepted": False, "reason": "rejected_overfit: ...", "sanitized": {"min_swing_prob": 0.6}},
        {"iteration": 3, "accepted": False, "reason": "rejected_guardrails: too_few_trades", "sanitized": {}},
    ]


def test_digest_counts_outcomes():
    d = build_digest(_summary(), _entries())
    assert d["accepted_count"] == 1
    assert d["overfit_rejections"] == 1
    assert d["guardrail_rejections"] == 1
    assert d["final_changes"] == {"min_entry_prob": 0.61}


def test_template_is_deterministic_and_grounded():
    d = build_digest(_summary(), _entries())
    t1 = render_template(d)
    t2 = render_template(d)
    assert t1 == t2
    assert "improved" in t1
    assert "min_entry_prob=0.61" in t1
    assert "out-of-sample" in t1


def test_explain_run_falls_back_to_template_offline():
    out = explain_run(summary=_summary(), entries=_entries())  # default null client
    assert out["source"] == "template"
    assert out["narrative"]
    assert out["digest"]["accepted_count"] == 1


def test_explain_run_uses_llm_when_available():
    client = _ScriptedClient(
        '{"narrative": "The loop improved the objective from -1.0 to -0.4.", '
        '"key_findings": ["accepted 1 change"], "risk_notes": ["demo only"]}'
    )
    out = explain_run(summary=_summary(), entries=_entries(), llm_client=client)
    assert out["source"] == "llm"
    assert out["model_id"] == "scripted-model"
    assert "objective" in out["narrative"]
    assert out["key_findings"]


def test_explain_run_falls_back_when_llm_returns_garbage():
    out = explain_run(summary=_summary(), entries=_entries(), llm_client=_ScriptedClient("not json"))
    assert out["source"] == "template"


def test_explain_real_run_summary():
    # A real loop summary explains without error.
    r = run_improvement_loop(iterations=6, seed=5)
    out = explain_run(summary=r.summary, entries=[])
    assert out["narrative"]
    assert isinstance(RunExplanation(narrative=out["narrative"]), RunExplanation)
