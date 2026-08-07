from __future__ import annotations

import json
import math

import pytest

import fxstack.risk.contracts as risk_contracts
import fxstack.risk.kernel as risk_kernel
from fxstack.risk import (
    ApprovedOrderIntent,
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskDecision,
    RiskKernelConfig,
    RiskRuleTrace,
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


class _CountingNumber:
    def __init__(self, value: float) -> None:
        self.value = value
        self.calls = 0

    def __float__(self) -> float:
        self.calls += 1
        return self.value


def test_entry_budget_converts_each_source_number_once() -> None:
    numbers = {
        "requested_lots": _CountingNumber(0.1),
        "target_risk_pct": _CountingNumber(0.0),
        "action_score": _CountingNumber(0.7),
        "expected_edge_bps": _CountingNumber(7.0),
        "confidence": _CountingNumber(0.7),
        "min_lots": _CountingNumber(0.01),
        "lot_step": _CountingNumber(0.01),
        "max_lots": _CountingNumber(1.0),
        "tp_price": _CountingNumber(1.104),
        "sl_price": _CountingNumber(1.099),
    }
    plan = risk_kernel._entry_budget_plan(
        intent=PolicyIntent(
            pair="EURUSD",
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=numbers["action_score"],  # type: ignore[arg-type]
            expected_edge_bps=numbers["expected_edge_bps"],  # type: ignore[arg-type]
            confidence=numbers["confidence"],  # type: ignore[arg-type]
            metadata={
                "requested_lots": numbers["requested_lots"],
                "target_risk_pct": numbers["target_risk_pct"],
                "tp_price": numbers["tp_price"],
                "sl_price": numbers["sl_price"],
            },
        ),
        portfolio=_portfolio(),
        config=RiskKernelConfig(
            min_lots=numbers["min_lots"],  # type: ignore[arg-type]
            lot_step=numbers["lot_step"],  # type: ignore[arg-type]
            max_lots=numbers["max_lots"],  # type: ignore[arg-type]
            require_entry_protection=True,
        ),
    )

    assert plan["rejection_reason"] == ""
    assert plan["final_lots"] == pytest.approx(0.1)
    assert {name: number.calls for name, number in numbers.items()} == {
        name: 1 for name in numbers
    }


def test_approved_order_validation_converts_each_source_number_once() -> None:
    numbers = {
        "lots": _CountingNumber(0.1),
        "close_lots": _CountingNumber(0.0),
        "action_score": _CountingNumber(0.7),
        "risk_budget_pct": _CountingNumber(0.005),
        "tp_price": _CountingNumber(1.104),
        "sl_price": _CountingNumber(1.099),
    }
    order = ApprovedOrderIntent(
        command="BUY",
        symbol="EURUSD",
        lots=numbers["lots"],  # type: ignore[arg-type]
        close_lots=numbers["close_lots"],  # type: ignore[arg-type]
        action_score=numbers["action_score"],  # type: ignore[arg-type]
        risk_budget_pct=numbers["risk_budget_pct"],  # type: ignore[arg-type]
        tp_price=numbers["tp_price"],  # type: ignore[arg-type]
        sl_price=numbers["sl_price"],  # type: ignore[arg-type]
    )

    assert risk_kernel._approved_order_numeric_errors(order) == []
    assert {name: number.calls for name, number in numbers.items()} == {
        name: 1 for name in numbers
    }


def test_zero_contract_value_refuses_instead_of_using_default_contract() -> None:
    plan = risk_kernel._entry_budget_plan(
        intent=_intent(
            requested_lots=0.0,
            target_risk_pct=0.005,
            entry_price=1.1,
            sl_price=1.095,
            value_per_price_unit=0.0,
        ),
        portfolio=_portfolio(),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01),
    )

    assert plan["final_lots"] == 0.0
    assert plan["risk_sizing_refusal"] == "non_positive_contract_value"
    assert plan["rejection_reason"] == "target_risk_pct_unsizeable_missing_stop_or_equity"


def test_entry_budget_plan_is_reused_for_final_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = risk_kernel._entry_budget_plan
    calls = 0

    def _counted_entry_budget_plan(**kwargs):
        nonlocal calls
        calls += 1
        return original(**kwargs)

    monkeypatch.setattr(
        risk_kernel,
        "_entry_budget_plan",
        _counted_entry_budget_plan,
    )

    decision = evaluate_risk_decision(
        policy_intent=_intent(),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            require_entry_protection=False,
        ),
    )

    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert calls == 1
    final_trace = next(
        item for item in decision.trace if item.rule == "final_sizing_order"
    )
    assert final_trace.details == {"source": "requested_lots"}


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


def test_risk_contract_serialization_round_trip_is_copy_isolated() -> None:
    decision = evaluate_risk_decision(
        policy_intent=_intent(nested={"labels": ["source"]}),
        market_state=_market(metadata={"feed": {"status": "fresh"}}),
        portfolio_state=_portfolio(
            metadata={"portfolio_telemetry": {"gross_lot_exposure": 0.25}}
        ),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01),
    )

    payload = decision.to_dict()
    restored = RiskDecision.from_dict(payload)

    assert restored.to_dict() == payload
    payload["policy_intent"]["metadata"]["nested"]["labels"].append("changed")
    payload["market_state"]["metadata"]["feed"]["status"] = "changed"
    payload["trace"][0]["details"]["changed"] = True
    assert decision.policy_intent.metadata["nested"]["labels"] == ["source"]
    assert decision.market_state.metadata["feed"]["status"] == "fresh"
    assert "changed" not in decision.trace[0].details


def test_risk_trace_runtime_serialization_validates_external_details() -> None:
    class _Mapping(dict):
        pass

    class _Sequence(list):
        pass

    external = RiskRuleTrace(
        rule="external",
        verdict="hold",
        details=_Mapping(
            {
                7: _Sequence(
                    [float("nan"), {"value": float("inf")}]
                )
            }
        ),
    )

    assert external.to_runtime_dict() == external.to_dict()
    payload = external.to_runtime_dict()
    assert payload["details"] == {"7": [None, {"value": None}]}
    payload["details"]["7"][1]["value"] = "changed"
    assert external.details[7][1]["value"] == float("inf")


def test_approved_order_command_serialization_validates_and_isolates_metadata() -> None:
    order = ApprovedOrderIntent(
        command="BUY",
        symbol="EURUSD",
        lots=0.1,
        metadata={"nested": {"values": [1.0, float("nan")]}, 9: "numeric-key"},
    )

    payload = order.to_command_payload()
    assert payload["nested"] == {"values": [1.0, None]}
    assert payload["9"] == "numeric-key"
    payload["nested"]["values"].append("changed")
    assert order.metadata["nested"]["values"][0] == 1.0
    assert math.isnan(order.metadata["nested"]["values"][1])


def test_kernel_trace_runtime_serialization_matches_public_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normalization_calls: list[object] = []
    original_json_safe_mapping = risk_contracts._json_safe_mapping

    def _record_json_safe_mapping(value):
        normalization_calls.append(value)
        return original_json_safe_mapping(value)

    monkeypatch.setattr(
        risk_contracts,
        "_json_safe_mapping",
        _record_json_safe_mapping,
    )
    decision = evaluate_risk_decision(
        policy_intent=_intent(),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01),
    )
    assert normalization_calls == []
    assert decision._trusted_trace_details is True
    assert "_trusted_trace_details" not in decision.to_dict()

    trusted_payloads = decision._to_runtime_trace_payloads()
    assert trusted_payloads == [trace.to_dict() for trace in decision.trace]
    for trace, trusted_payload in zip(decision.trace, trusted_payloads, strict=True):
        trusted_payload["details"]["trusted_runtime_only"] = True
        assert "trusted_runtime_only" not in trace.details

    for trace in decision.trace:
        runtime_payload = trace.to_runtime_dict()
        assert runtime_payload == trace.to_dict()
        assert "_details_json_safe" not in runtime_payload
        assert not hasattr(trace, "_details_json_safe")
        runtime_payload["details"]["runtime_only"] = True
        assert "runtime_only" not in trace.details
    assert len(normalization_calls) == len(decision.trace)


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
    assert "nonfinite:requested_lots" in final_trace.details["numeric_input_errors"]

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


def test_risk_kernel_requires_both_entry_protection_prices_when_enabled() -> None:
    missing = evaluate_risk_decision(
        policy_intent=_intent(),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01, require_entry_protection=True),
    )
    assert missing.verdict == "block"
    assert missing.reason == "invalid_order_numeric_contract"
    missing_trace = next(item for item in missing.trace if item.rule == "final_sizing_order")
    assert missing_trace.details["numeric_input_errors"] == [
        "missing:sl_price",
        "missing:tp_price",
    ]

    protected = evaluate_risk_decision(
        policy_intent=_intent(sl_price=1.0990, tp_price=1.1040, entry_protection_required=True),
        market_state=_market(),
        portfolio_state=_portfolio(),
        config=RiskKernelConfig(min_lots=0.01, lot_step=0.01, require_entry_protection=True),
    )
    assert protected.verdict == "allow"
    assert protected.approved_order is not None
    assert protected.approved_order.sl_price == 1.0990
    assert protected.approved_order.tp_price == 1.1040


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
