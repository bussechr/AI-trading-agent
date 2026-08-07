"""Phase 1 orchestration contracts and substrate exports."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "AgentProposal": "fxstack.orchestration.contracts",
    "AgentTrace": "fxstack.orchestration.contracts",
    "DecisionContext": "fxstack.orchestration.contracts",
    "DecisionPacket": "fxstack.orchestration.contracts",
    "ExperimentLineage": "fxstack.orchestration.contracts",
    "ExperimentPromotion": "fxstack.orchestration.contracts",
    "ExperimentProposal": "fxstack.orchestration.contracts",
    "GovernedDecision": "fxstack.orchestration.contracts",
    "ORCHESTRATION_SCHEMA_VERSION": "fxstack.orchestration.schema_version",
    "VersionBundle": "fxstack.orchestration.contracts",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
