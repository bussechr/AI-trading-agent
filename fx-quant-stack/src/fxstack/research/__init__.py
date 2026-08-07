"""Lazy public exports for the physically isolated research package."""

from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "run_vectorbt_research": "fxstack.research.vectorbt_harness",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
