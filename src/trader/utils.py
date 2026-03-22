"""Shared numeric utilities for the trading system."""

from __future__ import annotations

from typing import Any


def safe_float(value: Any, default: float = 0.0) -> float:
    """Convert *value* to float, returning *default* on failure."""
    try:
        return float(value)
    except Exception:
        return float(default)


def clip(value: float, lo: float, hi: float) -> float:
    """Clamp *value* into the closed interval [lo, hi]."""
    return float(max(lo, min(hi, value)))
