"""Portfolio intelligence exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "ConcentrationSnapshot": "fxstack.portfolio.concentration",
    "CorrelationSnapshot": "fxstack.portfolio.correlation",
    "PortfolioAllocationDecision": "fxstack.portfolio.allocator",
    "PortfolioBook": "fxstack.portfolio.book",
    "StressResult": "fxstack.portfolio.stress",
    "build_portfolio_book": "fxstack.portfolio.book",
    "build_portfolio_telemetry": "fxstack.portfolio.telemetry",
    "compute_concentration_snapshot": "fxstack.portfolio.concentration",
    "compute_correlation_snapshot": "fxstack.portfolio.correlation",
    "evaluate_book_stress": "fxstack.portfolio.stress",
    "evaluate_portfolio_allocation": "fxstack.portfolio.allocator",
    "prepare_return_series_map": "fxstack.portfolio.correlation",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
