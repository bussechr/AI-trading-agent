from __future__ import annotations

import pytest

from fxstack.strategy.adaptive_policy import evaluate_adaptive_entry
from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision
from fxstack.settings import get_settings


@pytest.mark.parametrize("metadata_key", ["portfolio_book", "portfolio_telemetry"])
def test_risk_kernel_exposure_checks_use_lot_units_from_portfolio_metadata(
    metadata_key: str,
) -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.74,
            confidence=0.74,
            expected_edge_bps=7.0,
            metadata={"requested_lots": 0.10, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T10:20:00Z",
            spread_bps=1.0,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=10000.0,
            gross_exposure=95_000.0,
            net_exposure=95_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
            metadata={
                metadata_key: {
                    "exposure_unit": "notional_units",
                    "gross_lot_exposure": 0.95,
                    "net_lot_exposure": 0.95,
                }
            },
        ),
        config=RiskKernelConfig(
            max_gross_exposure=2.0,
            max_net_exposure=2.0,
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
        ),
    )

    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    exposure_trace = next(item for item in decision.trace if item.rule == "exposure")
    assert exposure_trace.reason == "exposure_ok"
    assert exposure_trace.details["exposure_unit"] == "lot_units"
    assert exposure_trace.details["projected_gross_exposure"] == pytest.approx(1.05)
    assert exposure_trace.details["projected_net_exposure"] == pytest.approx(1.05)


def test_risk_kernel_blocks_sub_min_lot_entries_instead_of_rounding_up() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="GBPUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.68,
            confidence=0.68,
            expected_edge_bps=6.5,
            metadata={"requested_lots": 0.005, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="GBPUSD",
            ts="2026-04-07T11:00:00Z",
            spread_bps=1.1,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=12000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=0.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.verdict == "block"
    assert decision.reason == "requested_lots_below_min_lot"
    assert decision.approved_order is None
    final_trace = next(item for item in decision.trace if item.rule == "final_sizing_order")
    assert final_trace.reason == "requested_lots_below_min_lot"


def test_risk_budget_below_lot_quantum_gets_a_distinct_rejection_reason() -> None:
    """A derivable stop + equity whose composed risk budget rounds below the
    broker's minimum lot must NOT report as 'missing stop or equity' -- the
    audit found the two were conflated, making 'entry inexpressible at this
    lot quantum' indistinguishable from 'no signal' in cycle telemetry."""

    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.70,
            confidence=0.70,
            expected_edge_bps=6.0,
            metadata={
                "requested_lots": 0.0,
                # 0.03% of 10k equity = $3 budget; at a 50-pip stop on a 100k
                # contract that is 0.006 lots -- below the 0.01 minimum.
                "target_risk_pct": 0.0003,
                "entry_price": 1.1000,
                "sl_price": 1.0950,
                "value_per_price_unit": 100_000.0,
                "policy_allowed": True,
            },
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-07-31T11:00:00Z",
            spread_bps=1.0,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=10_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=0.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.verdict == "block"
    assert decision.reason == "entry_risk_budget_below_min_lot_quantum"
    assert decision.approved_order is None


def test_canary_rollout_budget_scale_zero_refuses_instead_of_full_size() -> None:
    """`effective or requested` treated a correctly-computed 0.0 canary scale
    as falsy and silently restored the FULL unscaled risk fraction -- an
    over-sizing fail-open. A zero scale must refuse with a named reason, on
    BOTH sizing paths."""

    def _decision(metadata: dict) -> object:
        return evaluate_risk_decision(
            policy_intent=PolicyIntent(
                pair="EURUSD",
                side="BUY",
                intent="ENTRY",
                action="entry",
                action_score=0.70,
                confidence=0.70,
                expected_edge_bps=6.0,
                metadata={**metadata, "policy_allowed": True},
            ),
            market_state=MarketState(
                pair="EURUSD",
                ts="2026-07-31T12:00:00Z",
                spread_bps=1.0,
                allowed_spread_bps=2.5,
                marketable=True,
                market_open=True,
                data_fresh=True,
            ),
            portfolio_state=PortfolioState(
                equity=10_000.0,
                open_position_count=0,
                pair_position_count=0,
                max_total_positions=6,
                max_pair_positions=1,
            ),
            config=RiskKernelConfig(
                min_lots=0.01,
                lot_step=0.01,
                max_lots=0.0,
                max_total_positions=6,
                max_pair_positions=1,
                rollout_mode="canary",
                rollout_pair_allowlisted=True,
                rollout_budget_scale=0.0,
            ),
        )

    # Two layers defend this: the rollout_canary rule blocks first with
    # "rollout_budget_zero"; the sizing layer independently refuses with
    # "rollout_budget_scale_zero" (removing the old `effective or requested`
    # falsy-or that restored the FULL unscaled fraction) in case the rule
    # ordering ever changes. Either named reason is a correct block; a full-size
    # approval is the failure this test forbids.
    blocked_reasons = {"rollout_budget_zero", "rollout_budget_scale_zero"}

    # Risk-fraction path: must refuse, never size at the unscaled fraction.
    risk_path = _decision(
        {
            "requested_lots": 0.0,
            "target_risk_pct": 0.005,
            "entry_price": 1.1000,
            "sl_price": 1.0950,
            "value_per_price_unit": 100_000.0,
        }
    )
    assert risk_path.verdict == "block"
    assert risk_path.reason in blocked_reasons
    assert risk_path.approved_order is None

    # Legacy lot path: previously fell through as a silent 0-lot "approval".
    legacy_path = _decision({"requested_lots": 0.10})
    assert legacy_path.verdict == "block"
    assert legacy_path.reason in blocked_reasons
    assert legacy_path.approved_order is None


def test_risk_kernel_blocks_notional_exposure_when_lot_metadata_is_missing() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.74,
            confidence=0.74,
            expected_edge_bps=7.0,
            metadata={"requested_lots": 0.10, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T10:20:00Z",
            spread_bps=1.0,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=10000.0,
            gross_exposure=95_000.0,
            net_exposure=95_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
            metadata={"portfolio_book": {"exposure_unit": "notional_units"}},
        ),
        config=RiskKernelConfig(
            max_gross_exposure=2.0,
            max_net_exposure=2.0,
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
        ),
    )

    assert decision.verdict == "block"
    assert decision.reason == "exposure_unit_mismatch"
    assert decision.approved_order is None
    exposure_trace = next(item for item in decision.trace if item.rule == "exposure")
    assert exposure_trace.reason == "exposure_unit_mismatch"
    assert exposure_trace.details["exposure_math_safe"] is False
    assert exposure_trace.details["exposure_unit"] == "notional_units"


def test_risk_kernel_fails_explicitly_when_target_risk_pct_needs_custom_builder() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="USDJPY",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.79,
            confidence=0.79,
            expected_edge_bps=9.0,
            metadata={"target_risk_pct": 0.02, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="USDJPY",
            ts="2026-04-07T11:15:00Z",
            spread_bps=0.9,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=50_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=0.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    # The kernel now sizes from ``target_risk_pct`` natively (risk/sizing.py), so
    # this no longer fails for "no custom builder". It still fails CLOSED, and for
    # the accurate reason: this intent carries no sl_price/entry_price/stop_distance,
    # so the risk cannot be converted into lots. Sizing without a stated stop is
    # exactly the defect that was removed.
    assert decision.verdict == "block"
    assert decision.reason == "target_risk_pct_unsizeable_missing_stop_or_equity"
    assert decision.approved_order is None
    final_trace = next(item for item in decision.trace if item.rule == "final_sizing_order")
    assert final_trace.reason == "target_risk_pct_unsizeable_missing_stop_or_equity"


def test_risk_kernel_sizes_natively_from_target_risk_pct_when_stop_is_known() -> None:
    """The formerly-dead risk-percent path now produces a real order."""

    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.79,
            confidence=0.79,
            expected_edge_bps=9.0,
            metadata={
                "target_risk_pct": 0.01,
                "policy_allowed": True,
                "entry_price": 1.1000,
                "sl_price": 1.0980,   # 20-pip stop
                "tp_price": 1.1080,
            },
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T11:15:00Z",
            spread_bps=0.9,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=PortfolioState(
            equity=10_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=1.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.approved_order is not None, decision.reason
    # $10k * 1% = $100 budget over a 20-pip stop -> 0.5 lots.
    assert decision.approved_order.lots == 0.5
    # The number that was previously 0.0 on every order ever sent.
    assert decision.approved_order.risk_budget_pct > 0.0


def test_adaptive_entry_accepts_live_low_trade_prob_reason_for_exception_path() -> None:
    settings = get_settings()
    decision = evaluate_adaptive_entry(
        row={
            "pair": "NZDUSD",
            "side": "short",
            "signal_side": "short",
            "baseline_rejection_reason": "low_trade_prob",
            "session_bucket": "asia",
            "session_entry_blocked": False,
            "session_entry_block_reason": "",
            "spread_bps": 1.0,
            "uncertainty_score": 0.08,
            "model_disagreement_score": 0.08,
            "playbook": "trend_pullback",
            "playbook_score": 0.84,
            "location_score": 0.72,
            "trigger_score": 0.83,
            "macro_coherence_score": 1.0,
            "regime_prob": 0.76,
            "swing_prob": 0.78,
            "entry_prob": 0.75,
            "trade_prob": 0.77,
            "expected_edge_bps": settings.min_expected_edge_bps * 3.0,
            "structure_timing_score": 0.72,
            "extension_penalty_score": 0.12,
            "environment_state": "PersistentTrend",
            "extreme_chase": False,
            "adaptive_base_rejection_reason": "approved",
            "calibrated_ev_bps": settings.min_expected_edge_bps * 3.0,
        },
        strict_ready=False,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["adaptive_rejection_reason"] == "approved"
