"""Tests for the risk-based kernel order builder.

``RiskKernelConfig.order_builder`` was declared and never assigned, so the
kernel's risk-percent branch was unreachable and every order carried
``risk_budget_pct = 0.0``. These tests exercise the missing implementation
against the real contracts, including the refusal paths -- a builder that
silently sizes without a stop is the bug it exists to prevent.
"""

from __future__ import annotations

import pytest

from fxstack.risk.contracts import MarketState, PolicyIntent, PortfolioState
from fxstack.risk.kernel import RiskKernelConfig
from fxstack.risk.order_builders import (
    DEFAULT_RISK_FRACTION,
    risk_based_order_builder,
    stop_distance_from_intent,
)

PIP = 0.0001


def _intent(**meta) -> PolicyIntent:
    base = {"entry_price": 1.1000, "sl_price": 1.0980, "tp_price": 1.1080}
    base.update(meta)
    return PolicyIntent(
        pair="EURUSD", side="BUY", intent="ENTRY", action="entry",
        action_score=0.7, confidence=0.4, metadata=base,
    )


def _market() -> MarketState:
    return MarketState(pair="EURUSD", ts="2026-07-30T00:00:00Z", spread_bps=0.5, marketable=True)


def _portfolio(equity: float = 10_000.0) -> PortfolioState:
    return PortfolioState(equity=equity, balance=equity, peak_equity=equity)


def test_order_builder_interface_is_assignable_to_the_kernel_config():
    """The declared-but-never-assigned interface must actually accept this."""

    cfg = RiskKernelConfig(order_builder=risk_based_order_builder())
    assert cfg.order_builder is not None
    assert callable(cfg.order_builder)


def test_sizes_from_stop_distance_and_risk_fraction():
    build = risk_based_order_builder(risk_fraction=0.01, max_lots=10.0)
    out = build(_intent(), _market(), _portfolio(10_000.0))
    assert out is not None
    # 20-pip stop, $100 budget -> 0.5 lots
    assert out.lots == pytest.approx(0.5, abs=1e-9)
    assert out.risk_budget_pct == pytest.approx(0.01)
    assert out.metadata["sizing_money_at_risk"] == pytest.approx(100.0, rel=1e-6)
    assert out.metadata["sizing_source"] == "risk_based_order_builder"
    assert out.command == "BUY"
    assert out.symbol == "EURUSD"


def test_risk_budget_pct_is_no_longer_always_zero():
    """The specific defect: every order ever sent carried risk_budget_pct 0.0."""

    out = risk_based_order_builder(max_lots=10.0)(_intent(), _market(), _portfolio())
    assert out is not None
    assert out.risk_budget_pct > 0.0
    assert out.risk_budget_pct == pytest.approx(DEFAULT_RISK_FRACTION)


def test_wider_stop_yields_smaller_position_same_money():
    build = risk_based_order_builder(risk_fraction=0.01, max_lots=10.0)
    tight = build(_intent(sl_price=1.0995), _market(), _portfolio())   # 5 pip
    wide = build(_intent(sl_price=1.0975), _market(), _portfolio())    # 25 pip
    assert tight is not None and wide is not None
    assert wide.lots < tight.lots
    assert wide.metadata["sizing_money_at_risk"] == pytest.approx(
        tight.metadata["sizing_money_at_risk"], rel=1e-6
    )


def test_refuses_without_a_stop():
    build = risk_based_order_builder()
    assert build(_intent(sl_price=0.0), _market(), _portfolio()) is None


def test_refuses_without_equity():
    build = risk_based_order_builder()
    assert build(_intent(), _market(), _portfolio(equity=0.0)) is None


def test_refuses_when_budget_below_min_lot():
    # Tiny equity + tiny risk + wide stop -> sub-minimum size. Refuse, never round up.
    build = risk_based_order_builder(risk_fraction=0.0005, min_lots=0.01, max_lots=1.0)
    assert build(_intent(sl_price=1.0500), _market(), _portfolio(equity=300.0)) is None


def test_refuses_unknown_side():
    build = risk_based_order_builder()
    bad = PolicyIntent(pair="EURUSD", side="", metadata={"entry_price": 1.1, "sl_price": 1.098})
    assert build(bad, _market(), _portfolio()) is None


def test_sell_side_builds_sell_command():
    build = risk_based_order_builder(risk_fraction=0.01, max_lots=10.0)
    sell = PolicyIntent(
        pair="EURUSD", side="SELL", intent="ENTRY", action="entry",
        metadata={"entry_price": 1.1000, "sl_price": 1.1020, "tp_price": 1.0920},
    )
    out = build(sell, _market(), _portfolio())
    assert out is not None and out.command == "SELL"
    assert out.lots > 0.0


def test_explicit_stop_distance_metadata_wins():
    i = _intent(stop_distance=30 * PIP, sl_price=1.0999)
    assert stop_distance_from_intent(i, _market()) == pytest.approx(30 * PIP)


def test_stop_distance_derived_from_sl_and_entry():
    assert stop_distance_from_intent(_intent(), _market()) == pytest.approx(20 * PIP, rel=1e-6)


def test_stop_distance_zero_when_underivable():
    i = PolicyIntent(pair="EURUSD", side="BUY", metadata={"sl_price": 1.09})
    # MarketState carries no bid/ask, and no entry_price in metadata.
    assert stop_distance_from_intent(i, _market()) == 0.0


def test_max_lots_cap_is_respected():
    build = risk_based_order_builder(risk_fraction=0.02, max_lots=0.10)
    out = build(_intent(), _market(), _portfolio(equity=1_000_000.0))
    assert out is not None
    assert out.lots == pytest.approx(0.10)


def test_kelly_mode_declines_a_negative_expectancy_bet():
    """confidence 0.14 at ~4R is below breakeven -> refuse to size."""

    build = risk_based_order_builder(risk_fraction=0.01, use_kelly=True, max_lots=10.0)
    weak = PolicyIntent(
        pair="EURUSD", side="BUY", intent="ENTRY", confidence=0.14,
        metadata={"entry_price": 1.1000, "sl_price": 1.0980, "tp_price": 1.1080},
    )
    assert build(weak, _market(), _portfolio()) is None


def test_kelly_mode_sizes_a_positive_expectancy_bet():
    build = risk_based_order_builder(risk_fraction=0.02, use_kelly=True, max_lots=10.0)
    strong = PolicyIntent(
        pair="EURUSD", side="BUY", intent="ENTRY", confidence=0.55,
        metadata={"entry_price": 1.1000, "sl_price": 1.0980, "tp_price": 1.1080},
    )
    out = build(strong, _market(), _portfolio())
    assert out is not None
    assert 0.0 < out.risk_budget_pct <= 0.02


def test_kelly_is_off_by_default():
    """Probabilities are not calibrated yet; Kelly must be opt-in."""

    build = risk_based_order_builder(risk_fraction=0.01, max_lots=10.0)
    out = build(_intent(), _market(), _portfolio())
    assert out is not None
    assert out.risk_budget_pct == pytest.approx(0.01)
