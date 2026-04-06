from __future__ import annotations

from typing import Any


def optional_research_capabilities() -> dict[str, bool]:
    out: dict[str, bool] = {}
    for name in ("neuralforecast", "ruptures", "river"):
        try:
            __import__(name)
            out[name] = True
        except Exception:
            out[name] = False
    return out


def build_optional_research_features(*, frame: Any) -> dict[str, Any]:
    capabilities = optional_research_capabilities()
    return {
        "capabilities": capabilities,
        "features": {},
    }
