from __future__ import annotations

from fxstack.backtest.pnl import (
    FillEngine,
    LifecycleState,
    PositionLedger,
    SignalAdapter,
    TradeFill,
    adapt_signal_row,
    build_ledger_report,
    build_portfolio_snapshot,
    conservative_fx_cost_model,
    next_lifecycle_event,
    normalize_ledger_rows,
)


def test_signal_adapter_normalizes_row() -> None:
    sig = adapt_signal_row({"pair": "eurusd", "ts": "2024-01-01T00:00:00Z", "side": "long", "allowed": True, "trade_prob": 0.7})
    assert sig.pair == "EURUSD"
    assert sig.side == "long"
    assert sig.allowed is True


def test_fill_engine_uses_conservative_costs_and_spread_gate() -> None:
    engine = FillEngine(cost_model=conservative_fx_cost_model(), max_spread_bps=2.0)
    sig = SignalAdapter().adapt({"pair": "EURUSD", "ts": "2024-01-01T00:00:00Z", "side": "long", "expected_edge_bps": 12.0, "trade_prob": 0.8})
    result = engine.execute(sig, bid=1.1000, ask=1.1002, mid=1.1001, requested_lots=0.10)
    assert result.accepted is True
    assert result.filled_lots == 0.10

    rejected = engine.execute(sig, bid=1.1000, ask=1.1010, mid=1.1005, requested_lots=0.10)
    assert rejected.accepted is False
    assert rejected.rejection_reason == "spread_too_wide"


def test_lifecycle_events_cover_partial_stop_exit_and_reversal() -> None:
    base = LifecycleState(pair="EURUSD", side="long", lots=1.0, entry_price=1.1, open_ts="2024-01-01T00:00:00Z", age_bars=10)
    partial = next_lifecycle_event(state=base, signal=SignalAdapter().adapt({"pair": "EURUSD", "ts": "2024-01-01T01:00:00Z", "side": "long"}), exit_action_selected="partial_tp")
    assert partial.action == "partial_tp"

    stopped = next_lifecycle_event(state=LifecycleState(pair="EURUSD", side="long", lots=1.0, entry_price=1.1, open_ts="2024-01-01T00:00:00Z", age_bars=100), signal=SignalAdapter().adapt({"pair": "EURUSD", "ts": "2024-01-01T01:00:00Z", "side": "long"}))
    assert stopped.action == "exit"
    assert stopped.reason == "hard_time_stop"

    reversed_exit = next_lifecycle_event(state=base, signal=SignalAdapter().adapt({"pair": "EURUSD", "ts": "2024-01-01T01:00:00Z", "side": "short", "allowed": True, "reversal_ready": True}))
    assert reversed_exit.action == "exit"
    assert reversed_exit.reason == "reversal_models_exit"


def test_portfolio_and_report_normalize_ledger_rows() -> None:
    ledger = PositionLedger(pair="EURUSD", side="long", open_lots=1.0, entry_price=1.1, realized_pnl_usd=12.5, unrealized_pnl_usd=3.2)
    snapshot = build_portfolio_snapshot([ledger], equity_usd=10_000.0)
    assert snapshot.open_positions == 1
    report = build_ledger_report(snapshot)
    assert report["summary"]["open_positions"] == 1
    df = normalize_ledger_rows([ledger])
    assert list(df["pair"]) == ["EURUSD"]


def test_trade_fill_roundtrip() -> None:
    ledger = PositionLedger(pair="EURUSD", side="long")
    fill = TradeFill(pair="EURUSD", side="long", lots=0.5, price=1.1, ts="2024-01-01T00:00:00Z", event_type="entry")
    from fxstack.backtest.pnl.portfolio import record_fill

    updated = record_fill(ledger, fill=fill)
    assert updated.open_lots == 0.5
    assert updated.avg_entry_price == 1.1

