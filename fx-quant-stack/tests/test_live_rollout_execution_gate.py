"""Regression tests for the two defects that made the live stack refuse every trade.

Measured on the IG MT4 demo, 2026-07-31, over a 6h EURUSD session:
113 governor-approved `enter` decisions -> 0 submitted commands.

1. `rollout_mode == "live"` was not recognised as an active rollout by the risk
   kernel (it tested `== "canary"`), while both submission gates accepted
   {"canary", "live"}. The kernel published `active=False`, the gates read that
   flag, and every order died as `live_rollout_inactive`. Promotion from canary
   to live DISABLED execution.

2. `_evaluate_adaptive_entry` overwrote an explicit `no_trade` playbook verdict
   with an environment-derived name without recomputing the three mask-gated
   scores, so 0.61 of `setup_score` stayed at 0.0 and entry became
   arithmetically impossible -- reported as `setup_quality_below_floor`.
"""

from __future__ import annotations

import pytest

from fxstack.risk import (
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskKernelConfig,
    evaluate_risk_decision,
)
from fxstack.risk.kernel import (
    ROLLOUT_BUDGET_THROTTLED_MODES,
    ROLLOUT_EXECUTION_MODES,
)
from fxstack.strategy.adaptive_policy import (
    ENTRY_SETUP_FLOOR,
    PLAYBOOK_NO_TRADE,
)


def _decision(mode: str, *, allowlisted: bool = True, budget_scale: float = 0.25):
    return evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            action="entry",
            metadata={"requested_lots": 0.40, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-07-31T09:00:00Z",
            spread_bps=0.52,
            allowed_spread_bps=3.0,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=6604.25,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
            rollout_mode=mode,
            rollout_pair_allowlisted=allowlisted,
            rollout_budget_scale=budget_scale,
        ),
    )


# --------------------------------------------------------------------------- #
# 1. rollout mode "live"
# --------------------------------------------------------------------------- #
def test_live_rollout_mode_is_an_active_rollout() -> None:
    """The defect: promoting canary -> live turned execution off."""
    rollout = dict(_decision("live").metadata.get("rollout") or {})
    assert rollout["mode"] == "live"
    assert rollout["configured"] is True
    assert rollout["active"] is True, (
        "a live allowlisted pair must report an ACTIVE rollout -- both submission "
        "gates refuse the order as live_rollout_inactive when this is False"
    )


def test_live_rollout_runs_at_full_budget_but_canary_is_throttled() -> None:
    """Execution permission and budget throttling are separate questions."""
    live = dict(_decision("live", budget_scale=0.25).metadata.get("rollout") or {})
    canary = dict(_decision("canary", budget_scale=0.25).metadata.get("rollout") or {})

    assert live["active"] is canary["active"] is True
    assert live["budget_scale"] == pytest.approx(1.0)
    assert canary["budget_scale"] == pytest.approx(0.25)
    assert live["reduced_budget"] is False
    # The graduated state must not silently size smaller than the probation one.
    assert float(live["final_lots"]) >= float(canary["final_lots"])


def test_live_rollout_still_enforces_the_pair_allowlist() -> None:
    """Widening the rollout block to live must TIGHTEN, not loosen.

    Previously the whole rollout block was skipped for live pairs, so the
    allowlist was not enforced there at all.
    """
    decision = _decision("live", allowlisted=False)
    assert decision.verdict == "block"
    assert decision.reason == "rollout_pair_not_allowlisted"


def test_unknown_rollout_mode_is_not_an_active_rollout() -> None:
    for mode in ("", "shadow", "paper", "advisory_only"):
        rollout = dict(_decision(mode).metadata.get("rollout") or {})
        assert rollout["active"] is False, mode
        assert rollout["configured"] is False, mode


def test_execution_and_budget_mode_sets_agree_with_the_gates() -> None:
    """The three copies of "what counts as a live rollout" must not drift again.

    That drift -- kernel said {canary}, gates said {canary, live} -- IS the bug.
    """
    from fxstack.runtime import orchestration_bridge, service

    assert ROLLOUT_EXECUTION_MODES == frozenset({"canary", "live"})
    assert ROLLOUT_BUDGET_THROTTLED_MODES < ROLLOUT_EXECUTION_MODES
    # Both gates must reference the canonical set, not a local literal.
    assert service.ROLLOUT_EXECUTION_MODES is ROLLOUT_EXECUTION_MODES
    assert orchestration_bridge.ROLLOUT_EXECUTION_MODES is ROLLOUT_EXECUTION_MODES


def test_runtime_policy_resolver_treats_live_as_a_configured_rollout() -> None:
    """The upstream source of the same drift, in `_resolve_main_runtime_rollout_policy`.

    It feeds `RiskKernelConfig.rollout_mode` and `meta["rollout_active"]`, so a
    canary-only test here reproduced the block even with the kernel fixed.
    """
    from fxstack.runtime.runner import _resolve_main_runtime_rollout_policy

    live = _resolve_main_runtime_rollout_policy(
        pair="EURUSD",
        metadata={
            "main_runtime_rollout": {
                "mode": "live",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
                "budget_scale": 1.0,
            }
        },
    )
    assert live["mode"] == "live"
    assert live["configured"] is True
    assert live["active"] is True

    # A pair outside the allowlist is still inactive, in live mode too.
    other = _resolve_main_runtime_rollout_policy(
        pair="GBPUSD",
        metadata={
            "main_runtime_rollout": {
                "mode": "live",
                "enabled": True,
                "allowlisted_pairs": ["EURUSD"],
            }
        },
    )
    assert other["active"] is False


def test_submission_gate_admits_a_live_rollout() -> None:
    from fxstack.runtime.service import FinalEntryApproval

    def _approval(**kwargs) -> FinalEntryApproval:
        return FinalEntryApproval(
            pair="EURUSD",
            side="BUY",
            risk_approved_payload={"lots": 0.1},
            canonical_ready=True,
            governed_allowed=True,
            **kwargs,
        )

    assert (
        _approval(rollout_active=True, rollout_mode="live").validation_error({})
        != "live_rollout_inactive"
    )
    assert (
        _approval(rollout_active=True, rollout_mode="canary").validation_error({})
        != "live_rollout_inactive"
    )
    # The flag still binds -- this test proves the mode set widened, not the gate.
    assert (
        _approval(rollout_active=False, rollout_mode="live").validation_error({})
        == "live_rollout_inactive"
    )
    assert (
        _approval(rollout_active=True, rollout_mode="shadow").validation_error({})
        == "live_rollout_inactive"
    )


# --------------------------------------------------------------------------- #
# 2. playbook resurrection
# --------------------------------------------------------------------------- #
def test_zeroed_mask_gated_scores_cannot_reach_the_setup_floor() -> None:
    """Why resurrecting a `no_trade` verdict guaranteed a reject.

    playbook / location / trigger carry 0.25 + 0.18 + 0.18 of `setup_score`.
    With all three at 0.0 the remaining terms cap it at 0.39, and because
    0.39 < 0.5 the reliability shrink toward 0.5 raises it but never reaches
    ENTRY_SETUP_FLOOR for any reliability in [0, 1].
    """
    ceiling = 0.14 + 0.10 + 0.10 + 0.05
    assert ceiling == pytest.approx(0.39)
    assert ceiling < 0.5

    for reliability in (0.0, 0.25, 0.5, 0.60616, 0.75, 1.0):
        reliable = 0.5 + ((ceiling - 0.5) * reliability)
        assert reliable < ENTRY_SETUP_FLOOR, (
            f"reliability={reliability}: setup can still not pass, by construction"
        )


def test_explicit_no_trade_playbook_is_not_resurrected() -> None:
    """`no_trade` is a verdict, not a missing value."""
    from fxstack.strategy import adaptive_policy

    source = adaptive_policy.__file__
    with open(source, encoding="utf-8") as handle:
        text = handle.read()

    # The exact resurrection that shipped:
    assert (
        "if playbook == PLAYBOOK_NO_TRADE:\n"
        "        playbook = _playbook_from_environment(environment_state)"
    ) not in text, "the no_trade verdict is being overwritten again"
    assert 'hard_block_reason = "no_eligible_playbook"' in text


def test_conjunct_floor_settings_default_to_the_module_constants() -> None:
    """Making the floors configurable must not change production behaviour.

    The defaults have to keep tracking the constants they replaced, or an
    override-capable knob silently becomes a behaviour change.
    """
    from fxstack.settings import Settings
    from fxstack.strategy import adaptive_policy as ap

    fields = Settings.model_fields
    pairs = (
        ("entry_model_floor", ap.ENTRY_MODEL_FLOOR),
        ("entry_setup_floor", ap.ENTRY_SETUP_FLOOR),
        ("min_entry_evidence_margin", ap.MIN_ENTRY_EVIDENCE_MARGIN),
        ("cost_edge_multiple", ap.COST_EDGE_MULTIPLE),
    )
    for name, constant in pairs:
        assert name in fields, f"{name} missing from Settings"
        assert fields[name].default == pytest.approx(constant), (
            f"{name} default drifted from {constant}"
        )


def test_playbook_from_environment_still_names_an_unknown_playbook() -> None:
    """A row that never went through attach_adaptive_context is still nameable."""
    from fxstack.strategy.adaptive_policy import _playbook_from_environment

    assert _playbook_from_environment("BalancedRange") == "range_mean_reversion"
    assert _playbook_from_environment("ExpansionBreakout") == "breakout_expansion"
    # ...but naming is for UNKNOWN, never for an explicit no_trade.
    assert _playbook_from_environment("") != PLAYBOOK_NO_TRADE
