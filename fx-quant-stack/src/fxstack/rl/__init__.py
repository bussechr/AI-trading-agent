"""Reinforcement-learning exports, loaded only when requested."""

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "RLEpisodeEvent": "fxstack.rl.contracts",
    "RLEpisodeRow": "fxstack.rl.contracts",
    "RLLinearCheckpoint": "fxstack.rl.checkpoint",
    "RLObservation": "fxstack.rl.contracts",
    "RLPortfolioAction": "fxstack.rl.contracts",
    "RLPortfolioObservation": "fxstack.rl.contracts",
    "RLPortfolioProposal": "fxstack.rl.proposal",
    "RLPortfolioProposalBundle": "fxstack.rl.proposal",
    "RLRunConfig": "fxstack.rl.contracts",
    "RLTradeAction": "fxstack.rl.contracts",
    "build_episode_from_rows": "fxstack.rl.contracts",
    "build_portfolio_rl_proposal_bundle": "fxstack.rl.proposal",
    "normalize_episode_rows": "fxstack.rl.contracts",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
