"""Unit tests for :mod:`fxstack.risk.envelope`.

Covers the envelope's contract:
* Kernel parity — running through an empty envelope must produce the same
  ``RiskDecision`` as calling :func:`evaluate_risk_decision` directly.
* Rule composition — post-rules run in declared order and see prior
  modifications.
* Failure isolation — a rule that raises does not crash the envelope; the
  decision is downgraded to ``hold`` and remaining rules are skipped.
* Built-in rules — ``governance_pause_rule`` blocks entries on pause and
  passes exits through.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from fxstack.risk import (
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskContext,
    RiskDecision,
    RiskEnvelope,
    RiskKernelConfig,
    RiskRuleTrace,
    default_envelope,
    evaluate_risk_decision,
    governance_pause_rule,
    make_rule,
)


def _baseline_context(**overrides: Any) -> RiskContext:
    """An "allow"-able snapshot — no failing rule by default."""
    defaults: dict[str, Any] = {
        "policy_intent": PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.7,
            expected_edge_bps=8.0,
            confidence=0.6,
            metadata={"requested_lots": 0.05},
        ),
        "market_state": MarketState(
            pair="EURUSD",
            ts="2026-05-20T12:00:00Z",
            session_bucket="london",
            spread_bps=1.2,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
            freshness_secs=1.0,
            freshness_limit_secs=30.0,
        ),
        "portfolio_state": PortfolioState(
            equity=10_000.0,
            balance=10_000.0,
            peak_equity=10_000.0,
            drawdown_pct=0.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=5,
            max_pair_positions=2,
            gross_exposure=0.0,
            net_exposure=0.0,
        ),
        "config": RiskKernelConfig(
            max_spread_bps=2.5,
            freshness_limit_secs=30.0,
            max_total_positions=5,
            max_pair_positions=2,
            max_drawdown_pct=20.0,
            max_gross_exposure=1.0,
            max_net_exposure=1.0,
            min_lots=0.01,
            lot_step=0.01,
            max_lots=1.0,
        ),
        "governance": {},
        "settings": None,
        "metadata": {},
    }
    defaults.update(overrides)
    return RiskContext(**defaults)


# ---------------------------------------------------------------------------
# Kernel parity
# ---------------------------------------------------------------------------


def test_empty_envelope_produces_kernel_identical_decision() -> None:
    """An envelope with no post-rules must be a transparent pass-through.

    Future changes can add rules, but the contract is: "no rules = legacy
    behavior, byte for byte." This test pins that.
    """
    ctx = _baseline_context()
    direct = evaluate_risk_decision(
        policy_intent=ctx.policy_intent,
        market_state=ctx.market_state,
        portfolio_state=ctx.portfolio_state,
        config=ctx.config,
    )
    via_envelope = RiskEnvelope().evaluate(ctx)
    assert via_envelope.verdict == direct.verdict
    assert via_envelope.reason == direct.reason
    assert via_envelope.final_lots == direct.final_lots
    assert via_envelope.close_lots == direct.close_lots
    assert via_envelope.lifecycle_action == direct.lifecycle_action
    assert len(via_envelope.trace) == len(direct.trace)
    assert [t.rule for t in via_envelope.trace] == [t.rule for t in direct.trace]


def test_default_envelope_is_kernel_only() -> None:
    """`default_envelope()` ships with no post-rules so live behavior is unchanged."""
    assert default_envelope().rules == ()


# ---------------------------------------------------------------------------
# Rule composition
# ---------------------------------------------------------------------------


def test_post_rule_can_observe_kernel_decision() -> None:
    """A rule sees the kernel's working decision and can add to its trace."""
    seen: list[str] = []

    def _observe(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        seen.append(decision.verdict)
        decision.trace.append(
            RiskRuleTrace(rule="observer", verdict=decision.verdict, reason="seen")
        )
        return decision

    envelope = RiskEnvelope(post_rules=[make_rule("observer", _observe)])
    result = envelope.evaluate(_baseline_context())
    assert seen == [result.verdict]
    assert any(t.rule == "observer" for t in result.trace)


def test_post_rules_run_in_declared_order() -> None:
    """Rules are folded over the decision in the order they were declared."""
    order: list[str] = []

    def _first(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        order.append("first")
        return decision

    def _second(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        order.append("second")
        return decision

    envelope = RiskEnvelope(
        post_rules=[make_rule("first", _first), make_rule("second", _second)]
    )
    envelope.evaluate(_baseline_context())
    assert order == ["first", "second"]


def test_with_rule_returns_new_envelope_without_mutating_original() -> None:
    """`with_rule` must be a pure functional composition."""
    base = RiskEnvelope()
    extended = base.with_rule(make_rule("noop", lambda c, d: d))
    assert base.rules == ()
    assert len(extended.rules) == 1


def test_rule_can_override_verdict_to_block() -> None:
    """A rule that flips verdict to block is reflected in the returned decision."""

    def _block(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        decision.trace.append(
            RiskRuleTrace(
                rule="hard_block",
                verdict="block",
                reason="test_override",
                changed_decision=True,
            )
        )
        return replace(decision, verdict="block", reason="test_override")

    envelope = RiskEnvelope(post_rules=[make_rule("hard_block", _block)])
    decision = envelope.evaluate(_baseline_context())
    assert decision.verdict == "block"
    assert decision.reason == "test_override"


# ---------------------------------------------------------------------------
# Failure isolation
# ---------------------------------------------------------------------------


def test_rule_exception_does_not_propagate() -> None:
    """A rule that raises must not crash the envelope."""

    def _broken(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        raise RuntimeError("synthetic")

    envelope = RiskEnvelope(post_rules=[make_rule("broken", _broken)])
    decision = envelope.evaluate(_baseline_context())
    assert decision.verdict in {"hold", "block"}  # downgraded from allow-or-worse
    error_traces = [t for t in decision.trace if "rule_error" in t.reason]
    assert error_traces, "expected a rule_error trace entry"
    assert error_traces[0].rule == "broken"


def test_rule_exception_short_circuits_remaining_rules() -> None:
    """After a rule raises, subsequent rules must not run."""
    visited: list[str] = []

    def _broken(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        visited.append("broken")
        raise RuntimeError("synthetic")

    def _after(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        visited.append("after")  # must not be called
        return decision

    envelope = RiskEnvelope(
        post_rules=[make_rule("broken", _broken), make_rule("after", _after)]
    )
    envelope.evaluate(_baseline_context())
    assert visited == ["broken"]


def test_rule_exception_preserves_block_verdict() -> None:
    """If the kernel already blocked, a rule_error must not silently re-allow."""
    # Force the kernel to block via stale data.
    ctx = _baseline_context(
        market_state=replace(
            _baseline_context().market_state,
            data_fresh=False,
            freshness_secs=120.0,
        )
    )

    def _broken(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        raise RuntimeError("synthetic")

    envelope = RiskEnvelope(post_rules=[make_rule("broken", _broken)])
    decision = envelope.evaluate(ctx)
    assert decision.verdict == "block"  # kernel's verdict wins


# ---------------------------------------------------------------------------
# Built-in: governance_pause_rule
# ---------------------------------------------------------------------------


def test_governance_pause_rule_blocks_entry_when_paused() -> None:
    envelope = RiskEnvelope(post_rules=[governance_pause_rule()])
    ctx = _baseline_context(
        governance={"paused": True, "reasons": ["latency_breach", "stale_features"]}
    )
    decision = envelope.evaluate(ctx)
    assert decision.verdict == "block"
    assert decision.reason == "capital_paused"
    pause_traces = [t for t in decision.trace if t.rule == "governance_pause"]
    assert pause_traces
    assert pause_traces[0].details["governance_reasons"] == [
        "latency_breach",
        "stale_features",
    ]


def test_governance_pause_rule_no_op_when_not_paused() -> None:
    envelope = RiskEnvelope(post_rules=[governance_pause_rule()])
    ctx = _baseline_context(governance={"paused": False})
    decision_with_rule = envelope.evaluate(ctx)
    decision_without_rule = RiskEnvelope().evaluate(ctx)
    assert decision_with_rule.verdict == decision_without_rule.verdict
    assert decision_with_rule.reason == decision_without_rule.reason
    # No spurious governance_pause trace when the rule does nothing.
    assert all(t.rule != "governance_pause" for t in decision_with_rule.trace)


@pytest.mark.parametrize("exit_action", ["exit", "partial_tp", "tighten_stop", "modify_sl"])
def test_governance_pause_rule_allows_lifecycle_exits(exit_action: str) -> None:
    """Pause must not stop a position from being closed or risk-reduced."""

    def _set_lifecycle(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        return replace(decision, lifecycle_action=exit_action)  # type: ignore[arg-type]

    envelope = RiskEnvelope(
        post_rules=[
            make_rule("set_lifecycle", _set_lifecycle),
            governance_pause_rule(),
        ]
    )
    ctx = _baseline_context(governance={"paused": True})
    decision = envelope.evaluate(ctx)
    # Verdict is whatever the kernel decided — not forced to block by pause.
    assert all(t.rule != "governance_pause" for t in decision.trace)
