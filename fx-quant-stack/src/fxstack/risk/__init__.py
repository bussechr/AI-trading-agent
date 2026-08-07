from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "ApprovedOrderIntent": "fxstack.risk.contracts",
    "LifecycleAction": "fxstack.risk.contracts",
    "MarketState": "fxstack.risk.contracts",
    "PortfolioState": "fxstack.risk.contracts",
    "PolicyIntent": "fxstack.risk.contracts",
    "RiskDecision": "fxstack.risk.contracts",
    "RiskRuleTrace": "fxstack.risk.contracts",
    "RiskKernelConfig": "fxstack.risk.kernel",
    "evaluate_risk_decision": "fxstack.risk.kernel",
    "Rule": "fxstack.risk.envelope",
    "RiskContext": "fxstack.risk.envelope",
    "RiskEnvelope": "fxstack.risk.envelope",
    "default_envelope": "fxstack.risk.envelope",
    "governance_pause_rule": "fxstack.risk.envelope",
    "make_rule": "fxstack.risk.envelope",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
