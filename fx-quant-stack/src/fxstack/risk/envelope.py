"""Single front door for composing risk decisions.

The legacy ``evaluate_risk_decision`` kernel in ``fxstack.risk.kernel`` already
contains the live trading logic (freshness, spread, exposure, drawdown,
canary, sizing, lifecycle). This module does **not** duplicate or replace any
of that. Instead it provides:

* A small **``Rule`` protocol** that any new operator-driven or compliance
  check can satisfy without modifying kernel internals.
* A **``RiskEnvelope``** that runs the kernel first (so live behavior remains
  bit-identical until rules are migrated one at a time with parity tests) and
  then folds post-rules over the working decision in declared order.
* A **``RiskContext``** dataclass that bundles all inputs every rule needs,
  so adding a rule does not change the call sites that build context.

Why a thin wrapper instead of a rewrite:

* The risk layer is 2.5k lines across kernel/policy/governance/agents. A
  big-bang refactor would risk regressing live trading.
* What's actually missing today is a **stable plug-in surface** — somewhere a
  new check (e.g., "block when broker reports degraded mode", "scale down
  during regional holidays") can be added without editing five files.
* The envelope is that surface. Future migrations of existing scattered rules
  into envelope rules can happen incrementally, each with a parity test.

Rule contract:

* Each rule is a callable ``(ctx, decision) -> decision``.
* Rules **must not** raise — but if they do, the envelope catches the
  exception, records a ``rule_error`` trace, downgrades verdict to ``hold``
  if the decision was ``allow``, and stops applying further rules. The live
  loop continues; the offending rule does not poison the cycle.
* Rules **may** mutate the trace list of the working decision (it is a list)
  but **should not** mutate the immutable input snapshots in ``ctx``.
* Rules are pure: deterministic functions of ``(ctx, decision)``. No I/O,
  no clock reads, no random — supply those via ``ctx.metadata`` if needed.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any, Callable, Protocol, runtime_checkable

from fxstack.risk.contracts import (
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskDecision,
    RiskRuleTrace,
)
from fxstack.risk.kernel import RiskKernelConfig, evaluate_risk_decision


@dataclass(slots=True, frozen=True)
class RiskContext:
    """Immutable bundle of inputs handed to every rule.

    Frozen + slotted so rules cannot accidentally mutate inputs and so each
    field has a documented purpose. New context fields go here, not on
    individual rules — that keeps the call site honest and rules cheap to
    write.
    """

    policy_intent: PolicyIntent
    market_state: MarketState
    portfolio_state: PortfolioState
    config: RiskKernelConfig
    governance: dict[str, Any] = field(default_factory=dict)
    settings: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Rule(Protocol):
    """A pluggable post-rule.

    ``name`` is what shows up in the audit trace; ``__call__`` receives the
    immutable context and the working decision and returns the next decision.
    Implementations may be plain classes, dataclasses, or callables wrapped
    via :func:`make_rule`.
    """

    name: str

    def __call__(self, ctx: RiskContext, decision: RiskDecision) -> RiskDecision: ...


@dataclass(slots=True, frozen=True)
class _CallableRule:
    name: str
    fn: Callable[[RiskContext, RiskDecision], RiskDecision]

    def __call__(self, ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        return self.fn(ctx, decision)


def make_rule(
    name: str,
    fn: Callable[[RiskContext, RiskDecision], RiskDecision],
) -> Rule:
    """Wrap a plain function as a :class:`Rule`.

    Use this for one-off rules. For rules that need configuration or state
    (e.g., a sliding-window counter), implement the protocol directly so the
    state lives on the rule instance, not in a closure.
    """
    return _CallableRule(name=str(name), fn=fn)


class RiskEnvelope:
    """Composes the legacy risk kernel with pluggable post-rules.

    Construction is cheap (no I/O); reuse a single instance per process where
    possible so its rule list is stable for the cycle.
    """

    __slots__ = ("_post_rules",)

    def __init__(self, post_rules: list[Rule] | tuple[Rule, ...] | None = None) -> None:
        self._post_rules: tuple[Rule, ...] = tuple(post_rules or ())

    @property
    def rules(self) -> tuple[Rule, ...]:
        return self._post_rules

    def with_rule(self, rule: Rule) -> "RiskEnvelope":
        """Return a new envelope with ``rule`` appended (immutable composition)."""
        return RiskEnvelope(post_rules=list(self._post_rules) + [rule])

    def evaluate(self, ctx: RiskContext) -> RiskDecision:
        """Run the kernel, then fold post-rules over the working decision."""
        decision = evaluate_risk_decision(
            policy_intent=ctx.policy_intent,
            market_state=ctx.market_state,
            portfolio_state=ctx.portfolio_state,
            config=ctx.config,
        )
        for rule in self._post_rules:
            try:
                decision = rule(ctx, decision)
            except Exception as exc:  # defensive: never crash the live loop
                rule_name = getattr(rule, "name", repr(rule))
                decision.trace.append(
                    RiskRuleTrace(
                        rule=str(rule_name),
                        verdict="hold",
                        reason=f"rule_error: {type(exc).__name__}: {exc}",
                        changed_decision=(decision.verdict == "allow"),
                    )
                )
                if decision.verdict == "allow":
                    decision = replace(
                        decision, verdict="hold", reason=f"rule_error:{rule_name}"
                    )
                break
        return decision


# ----- Built-in adapter rules -----
#
# These wrap policy already expressed elsewhere in the codebase. They are
# **optional** — the default envelope below does not include them, so live
# behavior is unchanged. They serve as worked examples and as plug-ins that
# operators can attach via :func:`RiskEnvelope.with_rule` when needed.


def governance_pause_rule() -> Rule:
    """Block entries when capital governance reports a pause.

    The runner currently encodes this indirectly via ``market_state.marketable
    = ... and (not paused)``, which the kernel then rejects as a marketability
    failure. This rule expresses the same outcome with a clearer trace label
    (``governance_pause`` vs ``marketable_fail``), and honors lifecycle exits
    so risk-down actions still flow during a pause.

    Currently **not** added to the default envelope to avoid changing live
    behavior. Attach it explicitly when an operator wants the clearer trace
    label, after running a parity check against your historical decision log.
    """

    _EXIT_ACTIONS = frozenset({"exit", "partial_tp", "tighten_stop", "modify_sl"})

    def _fn(ctx: RiskContext, decision: RiskDecision) -> RiskDecision:
        gov = dict(ctx.governance or {})
        if not gov.get("paused"):
            return decision
        lifecycle = str(decision.lifecycle_action or "").lower()
        if lifecycle in _EXIT_ACTIONS:
            return decision
        was_allowed = decision.verdict == "allow"
        decision.trace.append(
            RiskRuleTrace(
                rule="governance_pause",
                verdict="block",
                reason="capital_paused",
                changed_decision=was_allowed,
                details={"governance_reasons": list(gov.get("reasons") or [])},
            )
        )
        if was_allowed:
            return replace(decision, verdict="block", reason="capital_paused")
        return decision

    return make_rule("governance_pause", _fn)


def default_envelope() -> RiskEnvelope:
    """Default envelope used by the runtime.

    Currently kernel-only (no post-rules) so behavior is bit-identical to
    calling ``evaluate_risk_decision`` directly. The envelope's value today
    is the stable contract surface; rules will be added or migrated in over
    time, each with a parity test against the legacy path.
    """
    return RiskEnvelope(post_rules=[])


__all__ = [
    "Rule",
    "RiskContext",
    "RiskEnvelope",
    "default_envelope",
    "governance_pause_rule",
    "make_rule",
]
