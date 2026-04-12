from __future__ import annotations

import pytest

from fxstack.backtest.adaptive_policy import evaluate_adaptive_entry
from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision
from fxstack.settings import get_settings


def test_risk_kernel_exposure_checks_use_lot_units_from_portfolio_book_metadata() -> None:
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
                "portfolio_book": {
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

    assert decision.verdict == "block"
    assert decision.reason == "target_risk_pct_requires_custom_order_builder"
    assert decision.approved_order is None
    final_trace = next(item for item in decision.trace if item.rule == "final_sizing_order")
    assert final_trace.reason == "target_risk_pct_requires_custom_order_builder"


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
            "calibrated_ev_bps_shadow": settings.min_expected_edge_bps * 3.0,
        },
        strict_ready=False,
        open_positions={},
        settings=settings,
        fallback_margin=0.08,
    )

    assert decision["adaptive_allowed"] is True
    assert decision["adaptive_rejection_reason"] == "approved"
