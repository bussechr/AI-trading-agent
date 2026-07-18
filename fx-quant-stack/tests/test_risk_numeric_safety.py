from __future__ import annotations

import json

import pytest

from fxstack.risk import (
    ApprovedOrderIntent,
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskKernelConfig,
    evaluate_risk_decision,
)


def _intent(**metadata):
    return PolicyIntent(
        pair="EURUSD",
        side="BUY",
        intent="ENTRY",
        action="entry",
        action_score=0.7,
        expected_edge_bps=7.0,
        confidence=0.7,
        metadata={"requested_lots": 0.1, "policy_allowed": True, **metadata},
    )


def _market(**overrides):
    values = {
        "pair": "EURUSD",
        "ts": "2026-04-07T10:20:00Z",
        "spread_bps": 1.0,
        "allowed_spread_bps": 0.0,
        "marketable": True,
        "market_open": True,
        "data_fresh": True,
    }
    values.update(overrides)
    return MarketState(**values)


def _portfolio(**overrides):
    values = {
        "equity": 10_000.0,
        "gross_exposure": 0.0,
        "net_exposure": 0.0,
        "drawdown_pct": 0.0,
        "open_position_count": 0,
        "pair_position_count": 0,
    }
    values.update(overrides)
    return PortfolioState(**values)


@pytest.mark.parametrize(
    ("market_overrides", "portfolio_overrides", "expected_reason"),
    [
        ({"freshness_secs": float("nan")}, {}, "invalid_freshness_contract"),
        ({"spread_bps": float("nan")}, {}, "invalid_spread_contract"),
        ({}, {"gross_exposure": float("nan")}, "invalid_exposure_values"),
        ({}, {"drawdown_pct": float("nan")}, "invalid_drawdown_contract"),
    ],
)
def test_risk_kernel_blocks_nonfinite_entry_state_even_when_limits_are_disabled(
    market_overrides: dict[str, float],
    portfolio_overrides: dict[str, float],
    expected_reason: str,
) -> None:
    decision = evaluate_risk_decision(
        policy_intent=_intent(),
        market_state=_market(**market_overrides),
        portfolio_state=_portfolio(**portfolio_overrides),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01),
    )

    assert decision.verdict == "block"
    assert decision.reason == expected_reason
    assert decision.approved_order is None
    json.dumps(decision.to_dict(), allow_nan=False)


def test_risk_kernel_rejects_nonfinite_entry_and_partial_close_sizing() -> None:
    entry = evaluate_risk_decision(
        policy_intent=_intent(requested_lots=float("nan")),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01),
    )
    assert entry.verdict == "block"
    assert entry.reason == "invalid_order_numeric_contract"
    final_trace = next(item for item in entry.trace if item.rule == "final_sizing_order")
    assert "nonfinite:requested_lots" in final_trace.details["budget_plan"]["numeric_input_errors"]

    partial = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="SELL",
            intent="EXIT_MODEL",
            action="partial_tp",
            action_score=0.8,
            metadata={
                "lifecycle_action": "partial_tp",
                "has_open_position": True,
                "close_lots": float("nan"),
            },
        ),
        market_state=_market(),
        portfolio_state=_portfolio(open_position_count=1, pair_position_count=1),
        config=RiskKernelConfig(),
    )
    assert partial.verdict == "block"
    assert partial.reason == "invalid_close_lots"
    assert partial.approved_order is None
    json.dumps(partial.to_dict(), allow_nan=False)


def test_risk_kernel_validates_custom_builder_output_and_preserves_protective_exit() -> None:
    def invalid_builder(intent, market, portfolio):
        return ApprovedOrderIntent(
            command="BUY",
            symbol=intent.pair,
            lots=float("inf"),
            side="BUY",
        )

    built = evaluate_risk_decision(
        policy_intent=_intent(),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(order_builder=invalid_builder),
    )
    assert built.verdict == "block"
    assert built.reason == "invalid_approved_order_numeric_contract"
    assert built.approved_order is None

    protective_exit = evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair="EURUSD",
            side="SELL",
            intent="EXIT_MODEL",
            action="exit",
            action_score=0.8,
            metadata={"lifecycle_action": "exit", "has_open_position": True},
        ),
        market_state=_market(spread_bps=float("nan"), freshness_secs=float("nan"), data_fresh=False),
        portfolio_state=_portfolio(
            gross_exposure=float("nan"),
            net_exposure=float("nan"),
            drawdown_pct=float("nan"),
            open_position_count=1,
            pair_position_count=1,
        ),
        config=RiskKernelConfig(),
    )
    assert protective_exit.verdict == "allow"
    assert protective_exit.approved_order is not None
    assert protective_exit.approved_order.command == "CLOSE"
    json.dumps(protective_exit.to_dict(), allow_nan=False)
