"""Tiny private coercion helpers shared across runtime modules.

The small numeric coercions in this module are the canonical runtime
implementations. Boundary modules import them instead of carrying local
copies of the same exception-handling code.

Private (leading-underscore module) on purpose: this is not a public
fxstack API, just an internal coercion convenience. Other top-level
fxstack modules can migrate their local copies here over time.
"""

from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Coerce ``value`` to float; fall back to ``default`` on any failure.

    Intentionally swallows every exception — callers use this at boundary
    layers where they cannot guarantee input types (broker reports, EA
    payloads, JSON from disk). When the conversion is critical, validate
    explicitly upstream instead of relying on the default.
    """
    try:
        return float(value)
    except Exception:
        return float(default)


def safe_int(value: Any, default: int = 0) -> int:
    """Coerce ``value`` to int; fall back to ``default`` on input failure."""

    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def clip01(value: Any) -> float:
    """Coerce to float and clamp to ``[0.0, 1.0]``. 0.0 on failure."""
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


__all__ = ["clip01", "safe_float", "safe_int"]
