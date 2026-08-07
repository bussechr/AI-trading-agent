"""Deterministic Phase 4 committee agents."""

from fxstack._lazy import bind_lazy_exports

_EXPORTS = {
    "BreakoutExpansionAgent": "fxstack.orchestration.agents.committee.breakout_expansion",
    "ExecutionQualityAgent": "fxstack.orchestration.agents.committee.execution_quality",
    "PortfolioRiskAgent": "fxstack.orchestration.agents.committee.portfolio_risk",
    "RangeMeanReversionAgent": "fxstack.orchestration.agents.committee.range_mean_reversion",
    "ReversalExitAgent": "fxstack.orchestration.agents.committee.reversal_exit",
    "SpreadMicrostructureAgent": "fxstack.orchestration.agents.committee.spread_microstructure",
    "TrendPullbackAgent": "fxstack.orchestration.agents.committee.trend_pullback",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
