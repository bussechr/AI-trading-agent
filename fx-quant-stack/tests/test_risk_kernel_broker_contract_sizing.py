from __future__ import annotations

import pytest

from fxstack.risk import (
    ApprovedOrderIntent,
    MarketState,
    PolicyIntent,
    PortfolioState,
    RiskKernelConfig,
    evaluate_risk_decision,
)


def _spec(pair: str, **overrides: object) -> dict[str, object]:
    crypto = pair == "BTCUSD"
    point = 0.01 if crypto else (0.001 if pair.endswith("JPY") else 0.00001)
    values: dict[str, object] = {
        "symbol": pair,
        "broker_symbol": f"{pair}.IG",
        "lot_size": 1.0 if pair == "BTCUSD" else 100_000.0,
        "min_lot": 0.1,
        "lot_step": 0.1,
        "max_lot": 2.0,
        "point": point,
        "digits": 2 if crypto else (3 if pair.endswith("JPY") else 5),
        "tick_size": point,
        "tick_value": 1.0,
        "stop_level_points": 10.0,
        "freeze_level_points": 0.0,
        "trade_allowed": True,
        "margin_required": 100.0,
    }
    values.update(overrides)
    return values


def _broker_metadata(pair: str = "EURUSD") -> dict[str, object]:
    if pair == "BTCUSD":
        entry_price, sl_price = 1_000.0, 900.0
    elif pair.endswith("JPY"):
        entry_price, sl_price = 160.0, 159.5
    else:
        entry_price, sl_price = 1.1, 1.099
    return {
        "policy_allowed": True,
        "target_risk_pct": 0.01,
        "requested_lots": 9.9,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "broker_contract_required": True,
        "broker_contract_spec": _spec(pair),
        "broker_contract_available_margin": 9_000.0,
        "broker_contract_account_currency": "USD",
        "broker_contract_margin_utilization_cap": 0.25,
        "broker_contract_errors": [],
        "quote_rates": {"USDJPY": 150.0},
    }


def _decision(
    *,
    pair: str = "EURUSD",
    metadata: dict[str, object] | None = None,
    config: RiskKernelConfig | None = None,
    portfolio_state: PortfolioState | None = None,
):
    return evaluate_risk_decision(
        policy_intent=PolicyIntent(
            pair=pair,
            side="BUY",
            intent="ENTRY",
            action="entry",
            action_score=0.75,
            confidence=0.75,
            expected_edge_bps=8.0,
            metadata=dict(metadata or _broker_metadata(pair)),
        ),
        market_state=MarketState(
            pair=pair,
            ts="2026-08-03T12:00:00Z",
            spread_bps=0.8,
            allowed_spread_bps=2.5,
            marketable=True,
            market_open=True,
            data_fresh=True,
        ),
        portfolio_state=portfolio_state
        or PortfolioState(
            equity=10_000.0,
            open_position_count=0,
            pair_position_count=0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
        config=config
        or RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=0.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )


def _sizing_trace(decision) -> dict[str, object]:
    trace = next(item for item in decision.trace if item.rule == "final_sizing_order")
    return dict(trace.details)


def test_required_broker_path_uses_contract_quantum_and_ignores_legacy_lots() -> None:
    decision = _decision(
        pair="BTCUSD",
        config=RiskKernelConfig(
            # Broker min/step replace these generic FX-era values.
            min_lots=5.0,
            lot_step=5.0,
            # The operator maximum remains a stricter ceiling and is rounded
            # down on the broker's 0.1 quantum.
            max_lots=0.45,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert decision.approved_order.lots == pytest.approx(0.4)
    assert decision.approved_order.lots != pytest.approx(9.9)

    diagnostics = decision.metadata["broker_contract_sizing"]
    assert diagnostics["status"] == "approved"
    assert diagnostics["broker_min_lot"] == pytest.approx(0.1)
    assert diagnostics["broker_lot_step"] == pytest.approx(0.1)
    assert diagnostics["broker_max_lot"] == pytest.approx(2.0)
    assert diagnostics["operator_max_lots"] == pytest.approx(0.45)
    assert diagnostics["effective_max_lot"] == pytest.approx(0.4)
    assert diagnostics["ignored_requested_lots"] == pytest.approx(9.9)
    assert diagnostics["lots"] == pytest.approx(0.4)
    assert diagnostics["value_per_price_unit"] == pytest.approx(1.0)
    assert diagnostics["money_at_risk"] == pytest.approx(40.0)
    assert decision.approved_order.metadata["broker_contract_sizing"] == diagnostics


def test_required_broker_path_uses_highest_lot_within_risk_and_exposure() -> None:
    decision = _decision(
        pair="BTCUSD",
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=100.0,
            max_gross_exposure=0.30,
            max_net_exposure=0.20,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    # Risk sizing requests 1.0 BTC lot, but directional net exposure permits
    # 0.20. The kernel must resize to that highest valid quantum, not reject.
    assert decision.approved_order.lots == pytest.approx(0.20)
    budget_plan = _sizing_trace(decision)
    assert budget_plan["sensible_lot_cap"] == pytest.approx(0.20)
    assert budget_plan["sensible_lot_cap_sources"] == ["net_exposure"]
    diagnostics = decision.metadata["broker_contract_sizing"]
    assert diagnostics["broker_max_lot"] == pytest.approx(2.0)
    assert diagnostics["effective_max_lot"] == pytest.approx(0.20)
    assert diagnostics["money_at_risk"] == pytest.approx(20.0)


def test_required_broker_path_blocks_a_missing_spec_without_lot_fallback() -> None:
    metadata = _broker_metadata()
    metadata.pop("broker_contract_spec")

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "broker_contract_spec_missing"
    assert decision.approved_order is None
    assert _sizing_trace(decision)["risk_sizing_refusal"] == (
        "broker_contract_spec_missing"
    )
    assert decision.metadata["broker_contract_sizing"]["status"] == "refused"


@pytest.mark.parametrize(
    ("spec", "reason"),
    [
        ("not-a-contract", "broker_contract_spec_malformed"),
        (_spec("EURUSD", min_lot=0.0), "broker_contract_min_lot_invalid"),
    ],
)
def test_required_broker_path_blocks_malformed_specs(
    spec: object,
    reason: str,
) -> None:
    metadata = _broker_metadata()
    metadata["broker_contract_spec"] = spec

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == reason
    assert decision.approved_order is None
    assert _sizing_trace(decision)["risk_sizing_refusal"] == reason


def test_required_broker_path_blocks_unattested_free_margin() -> None:
    metadata = _broker_metadata()
    metadata.pop("broker_contract_available_margin")

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "free_margin_unattested"
    assert decision.approved_order is None


def test_required_broker_path_blocks_unresolved_account_conversion() -> None:
    metadata = _broker_metadata("EURJPY")
    metadata["quote_rates"] = {"EURUSD": 1.1}

    decision = _decision(pair="EURJPY", metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "conversion_unresolvable"
    assert decision.approved_order is None


def test_required_broker_path_blocks_broker_stop_floor_violation() -> None:
    metadata = _broker_metadata()
    metadata["entry_price"] = 1.10000
    metadata["sl_price"] = 1.09995

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "stop_below_broker_minimum"
    assert decision.approved_order is None


def test_required_broker_path_blocks_margin_infeasibility() -> None:
    metadata = _broker_metadata()
    metadata["broker_contract_spec"] = _spec(
        "EURUSD",
        margin_required=1_000.0,
    )
    metadata["broker_contract_available_margin"] = 50.0

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "margin_infeasible"
    assert decision.approved_order is None


def test_required_broker_path_blocks_missing_account_currency() -> None:
    metadata = _broker_metadata()
    metadata["broker_contract_account_currency"] = ""

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "broker_contract_account_currency_unattested"
    assert decision.approved_order is None


def test_required_broker_path_surfaces_upstream_state_refusal() -> None:
    metadata = _broker_metadata()
    metadata["broker_contract_errors"] = ["broker_contract_specs_stale"]

    decision = _decision(metadata=metadata)

    assert decision.verdict == "block"
    assert decision.reason == "broker_contract_specs_stale"
    assert decision.metadata["broker_contract_sizing"]["state_errors"] == [
        "broker_contract_specs_stale"
    ]


def test_required_broker_path_cannot_be_bypassed_by_custom_builder() -> None:
    called = False

    def _builder(
        intent: PolicyIntent,
        _market: MarketState,
        _portfolio: PortfolioState,
    ) -> ApprovedOrderIntent:
        nonlocal called
        called = True
        return ApprovedOrderIntent(
            command="BUY",
            symbol=intent.pair,
            lots=9.9,
        )

    metadata = _broker_metadata()
    metadata.pop("broker_contract_spec")
    decision = _decision(
        metadata=metadata,
        config=RiskKernelConfig(
            max_total_positions=6,
            max_pair_positions=1,
            order_builder=_builder,
        ),
    )

    assert decision.verdict == "block"
    assert decision.reason == "broker_contract_spec_missing"
    assert decision.approved_order is None
    assert called is False


def test_absent_required_marker_preserves_legacy_requested_lot_path() -> None:
    metadata = {
        "policy_allowed": True,
        "requested_lots": 0.137,
        # These incomplete broker fields are inert unless the required marker
        # opts this intent into the fail-closed broker-contract path.
        "broker_contract_spec": None,
        "broker_contract_available_margin": 0.0,
    }

    decision = _decision(
        metadata=metadata,
        config=RiskKernelConfig(
            min_lots=0.01,
            lot_step=0.01,
            max_lots=0.0,
            max_total_positions=6,
            max_pair_positions=1,
        ),
    )

    assert decision.verdict == "allow"
    assert decision.approved_order is not None
    assert decision.approved_order.lots == pytest.approx(0.13)
    assert "broker_contract_sizing" not in decision.metadata
