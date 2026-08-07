"""Backtest PnL exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "ExecutionCostModel": "fxstack.backtest.pnl.execution_costs",
    "all_in_cost_bps": "fxstack.backtest.pnl.execution_costs",
    "apply_bps_slippage": "fxstack.backtest.pnl.execution_costs",
    "conservative_fx_cost_model": "fxstack.backtest.pnl.execution_costs",
    "FillEngine": "fxstack.backtest.pnl.fill_engine",
    "FillPlan": "fxstack.backtest.pnl.fill_engine",
    "FillResult": "fxstack.backtest.pnl.fill_engine",
    "build_fill_plan": "fxstack.backtest.pnl.fill_engine",
    "LifecycleEvent": "fxstack.backtest.pnl.lifecycle",
    "LifecycleState": "fxstack.backtest.pnl.lifecycle",
    "apply_lifecycle_event": "fxstack.backtest.pnl.lifecycle",
    "next_lifecycle_event": "fxstack.backtest.pnl.lifecycle",
    "PositionLedger": "fxstack.backtest.pnl.portfolio",
    "PortfolioSnapshot": "fxstack.backtest.pnl.portfolio",
    "TradeFill": "fxstack.backtest.pnl.portfolio",
    "build_portfolio_snapshot": "fxstack.backtest.pnl.portfolio",
    "fx_mark_to_market_equity": "fxstack.backtest.pnl.portfolio",
    "fx_quote_to_usd_rate": "fxstack.backtest.pnl.portfolio",
    "fx_realized_pnl_usd": "fxstack.backtest.pnl.portfolio",
    "build_ledger_report": "fxstack.backtest.pnl.reports",
    "normalize_ledger_rows": "fxstack.backtest.pnl.reports",
    "SignalAdapter": "fxstack.backtest.pnl.signal_adapter",
    "SimSignal": "fxstack.backtest.pnl.signal_adapter",
    "adapt_signal_row": "fxstack.backtest.pnl.signal_adapter",
    "adapt_signal_rows": "fxstack.backtest.pnl.signal_adapter",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
