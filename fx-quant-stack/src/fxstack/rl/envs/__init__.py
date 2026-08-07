"""Runtime-visible reinforcement-learning environment package."""

from fxstack._lazy import bind_lazy_exports

_EXPORTS = {"FxTradingEnv": "fxstack.rl.envs.fx_env"}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
