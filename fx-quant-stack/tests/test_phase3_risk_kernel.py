from __future__ import annotations

from fxstack.risk import MarketState, PolicyIntent, PortfolioState, RiskKernelConfig, evaluate_risk_decision


def test_risk_kernel_blocks_stale_entry_data() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.72,
            expected_edge_bps=8.0,
            confidence=0.72,
            metadata={"requested_lots": 0.12, "policy_allowed": True},
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T10:00:00Z",
            spread_bps=1.2,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=False,
            freshness_secs=901.0,
            freshness_limit_secs=600.0,
        ),
        portfolio_state=PortfolioState(
            equity=10000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=RiskKernelConfig(
            max_spread_bps=2.5,
            freshness_limit_secs=600.0,
            max_total_positions=6,
            max_pair_positions=1,
            min_lots=0.01,
            lot_step=0.01,
        ),
    )
    assert decision.verdict == "block"
    assert decision.reason == "data_stale"
    assert decision.approved_order is None
    assert [item.rule for item in decision.trace] == ["data_freshness"]


def test_risk_kernel_allows_existing_position_exit_when_entry_gates_are_bad() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="SELL",
            intent="EXIT_MODEL",
            action="exit",
            action_score=0.91,
            metadata={
                "policy_allowed": False,
                "policy_block_reason": "spread_too_wide",
                "lifecycle_action": "exit",
                "has_open_position": True,
            },
        ),
        market_state=MarketState(
            pair="EURUSD",
            ts="2026-04-07T10:05:00Z",
            spread_bps=8.0,
            allowed_spread_bps=2.5,
            marketable=False,
            market_open=False,
            data_fresh=False,
        ),
        portfolio_state=PortfolioState(
            equity=9800.0,
            open_position_count=1,
            pair_position_count=1,
            max_total_positions=6,
            max_pair_positions=1,
            drawdown_pct=4.0,
        ),
        config=RiskKernelConfig(max_spread_bps=2.5, max_total_positions=6, max_pair_positions=1),
    )
    assert decision.verdict == "allow"
    assert decision.lifecycle_action == "exit"
    assert decision.approved_order is not None
    assert decision.approved_order.command == "CLOSE"
    assert decision.trace[0].reason == "bypass_existing_position"


def test_risk_kernel_builds_entry_order_from_requested_lots() -> None:
    decision = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="GBPUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.68,
            confidence=0.68,
            expected_edge_bps=6.5,
            metadata={"requested_lots": 0.137, "policy_allowed": True},
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
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01, max_lots=0.0, max_total_positions=6, max_pair_positions=1),
    )
    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert abs(decision.approved_order.lots - 0.13) < 1e-9
    assert decision.approved_order.command == "BUY"
