"""The change-set safety allowlist -- the deterministic core of "code disposes".

An LLM (or the heuristic fallback) may *propose* edits to strategy configuration,
but it can only ever touch knobs registered here, every value is clamped to hard
bounds, and risk-critical caps may only move in the *safer* direction relative to
the incumbent config. A compromised or hallucinating model therefore cannot widen
spreads, enlarge position size, loosen exposure, or disable a gate.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal

KnobKind = Literal["float", "int"]
# For risk-locked knobs, the ONLY direction a proposal may move relative to the
# incumbent value. "increase" => safer is larger (e.g. probability gates);
# "decrease" => safer is smaller (e.g. position caps, lot size, exposure).
SafeDir = Literal["increase", "decrease"] | None


@dataclass(frozen=True, slots=True)
class Knob:
    name: str
    path: tuple[str, ...]
    lo: float
    hi: float
    step: float
    kind: KnobKind = "float"
    risk_locked: bool = False
    safe_direction: SafeDir = None
    description: str = ""

    def coerce(self, value: Any) -> float | int:
        num = float(value)
        num = max(self.lo, min(self.hi, num))
        if self.kind == "int":
            return int(round(num))
        return round(num, 6)


# Registry order is stable so heuristic coordinate descent and hashing are deterministic.
_KNOBS: tuple[Knob, ...] = (
    # --- Probability / edge gates (free to tune within bounds) ---
    Knob("min_swing_prob", ("gates", "min_swing_prob"), 0.50, 0.80, 0.01, "float",
         description="Directional swing probability gate"),
    Knob("min_entry_prob", ("gates", "min_entry_prob"), 0.50, 0.85, 0.01, "float",
         description="Entry probability gate"),
    Knob("min_trade_prob", ("gates", "min_trade_prob"), 0.50, 0.85, 0.01, "float",
         description="Trade probability gate"),
    Knob("min_expected_edge_bps", ("gates", "min_expected_edge_bps"), 1.0, 8.0, 0.25, "float",
         description="Minimum expected edge net of cost (bps)"),
    Knob("min_expected_edge_rescue_margin_bps", ("gates", "min_expected_edge_rescue_margin_bps"),
         0.0, 2.0, 0.1, "float", description="Rescue margin below the edge hurdle (bps)"),
    # --- Cost knobs (free within sane bounds) ---
    Knob("slippage_bps", ("cost_model", "slippage_bps"), 0.0, 2.0, 0.05, "float",
         description="Assumed per-fill slippage (bps)"),
    # --- Risk-critical caps: may only TIGHTEN vs incumbent ---
    Knob("max_allowed_spread_bps", ("gates", "max_allowed_spread_bps"), 1.0, 6.0, 0.25, "float",
         risk_locked=True, safe_direction="decrease",
         description="Max raw spread allowed at entry (bps) -- tighten only"),
    Knob("max_total_positions", ("risk", "max_total_positions"), 1, 6, 1, "int",
         risk_locked=True, safe_direction="decrease",
         description="Max concurrent open positions -- tighten only"),
    Knob("max_pair_positions", ("risk", "max_pair_positions"), 1, 2, 1, "int",
         risk_locked=True, safe_direction="decrease",
         description="Max concurrent positions per pair -- tighten only"),
    Knob("default_order_lots", ("risk", "default_order_lots"), 0.01, 0.10, 0.01, "float",
         risk_locked=True, safe_direction="decrease",
         description="Default order size in lots -- tighten only"),
    Knob("max_pair_exposure", ("risk", "max_pair_exposure"), 0.005, 0.02, 0.001, "float",
         risk_locked=True, safe_direction="decrease",
         description="Max fraction of equity per pair -- tighten only"),
    Knob("max_total_exposure", ("risk", "max_total_exposure"), 0.01, 0.06, 0.005, "float",
         risk_locked=True, safe_direction="decrease",
         description="Max fraction of equity total -- tighten only"),
    Knob("max_realized_corr_share", ("portfolio", "max_realized_corr_share"), 0.50, 0.90, 0.05, "float",
         risk_locked=True, safe_direction="decrease",
         description="Max realised-correlation share of the book -- tighten only"),
)

KNOBS_BY_NAME: dict[str, Knob] = {k.name: k for k in _KNOBS}


def all_knobs() -> tuple[Knob, ...]:
    return _KNOBS


def knob_names() -> list[str]:
    return [k.name for k in _KNOBS]


def _get_path(config: dict[str, Any], path: tuple[str, ...]) -> Any:
    node: Any = config
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _set_path(config: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = config
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def knob_values(config: dict[str, Any]) -> dict[str, float | int]:
    """Read the current value of every registered knob from ``config``."""

    out: dict[str, float | int] = {}
    for knob in _KNOBS:
        raw = _get_path(config, knob.path)
        if raw is None:
            continue
        try:
            out[knob.name] = knob.coerce(raw)
        except (TypeError, ValueError):
            continue
    return out


@dataclass(slots=True)
class ChangeSetResult:
    sanitized: dict[str, float | int]
    rejected: list[dict[str, Any]]
    adjusted: list[dict[str, Any]]

    @property
    def ok(self) -> bool:
        return not self.rejected

    def as_dict(self) -> dict[str, Any]:
        return {
            "sanitized": dict(self.sanitized),
            "rejected": list(self.rejected),
            "adjusted": list(self.adjusted),
        }


def validate_change_set(
    change_set: dict[str, Any],
    *,
    incumbent: dict[str, Any] | None = None,
) -> ChangeSetResult:
    """Clamp/validate a proposed change-set against the allowlist.

    Returns the sanitized (safe-to-apply) subset, plus structured records of any
    rejected unknown knobs and any values that had to be clamped or blocked from
    loosening risk. This function is the authority -- callers must apply only
    ``result.sanitized``.
    """

    incumbent_values = knob_values(dict(incumbent or {}))
    sanitized: dict[str, float | int] = {}
    rejected: list[dict[str, Any]] = []
    adjusted: list[dict[str, Any]] = []

    for raw_name, raw_value in dict(change_set or {}).items():
        name = str(raw_name).strip()
        knob = KNOBS_BY_NAME.get(name)
        if knob is None:
            rejected.append({"knob": name, "reason": "unknown_knob"})
            continue
        try:
            value = knob.coerce(raw_value)
        except (TypeError, ValueError):
            rejected.append({"knob": name, "reason": "non_numeric", "value": raw_value})
            continue

        if float(value) != float(raw_value if isinstance(raw_value, (int, float)) else value):
            adjusted.append({"knob": name, "reason": "clamped_to_bounds",
                             "from": raw_value, "to": value, "lo": knob.lo, "hi": knob.hi})

        # Risk-locked knobs may only move toward the safer direction vs incumbent.
        if knob.risk_locked and knob.name in incumbent_values:
            base = incumbent_values[knob.name]
            if knob.safe_direction == "decrease" and value > base:
                adjusted.append({"knob": name, "reason": "risk_loosening_blocked",
                                 "from": value, "to": base, "incumbent": base})
                value = knob.coerce(base)
            elif knob.safe_direction == "increase" and value < base:
                adjusted.append({"knob": name, "reason": "risk_loosening_blocked",
                                 "from": value, "to": base, "incumbent": base})
                value = knob.coerce(base)

        sanitized[name] = value

    return ChangeSetResult(sanitized=sanitized, rejected=rejected, adjusted=adjusted)


def apply_change_set(base_config: dict[str, Any], sanitized: dict[str, float | int]) -> dict[str, Any]:
    """Return a deep copy of ``base_config`` with ``sanitized`` knob values written in."""

    out = copy.deepcopy(dict(base_config or {}))
    for name, value in dict(sanitized or {}).items():
        knob = KNOBS_BY_NAME.get(name)
        if knob is None:
            continue
        _set_path(out, knob.path, value)
    return out


def default_config(settings: Any | None = None) -> dict[str, Any]:
    """Seed config for the improvement loop, sourced from live settings defaults."""

    if settings is None:
        from fxstack.settings import get_settings

        settings = get_settings()

    def _f(attr: str, default: float) -> float:
        return float(getattr(settings, attr, default) or default)

    def _i(attr: str, default: int) -> int:
        return int(getattr(settings, attr, default) or default)

    return {
        "gates": {
            "min_swing_prob": _f("min_swing_prob", 0.58),
            "min_entry_prob": _f("min_entry_prob", 0.62),
            "min_trade_prob": _f("min_trade_prob", 0.60),
            "min_expected_edge_bps": _f("min_expected_edge_bps", 3.0),
            "min_expected_edge_rescue_margin_bps": _f("min_expected_edge_rescue_margin_bps", 0.5),
            "max_allowed_spread_bps": _f("max_allowed_spread_bps", 3.0),
        },
        "cost_model": {
            "slippage_bps": _f("slippage_bps", 0.25),
        },
        "risk": {
            "max_total_positions": _i("max_total_positions", 6),
            "max_pair_positions": _i("max_pair_positions", 1),
            "default_order_lots": _f("default_order_lots", 0.10),
            "max_pair_exposure": _f("max_pair_exposure", 0.02),
            "max_total_exposure": _f("max_total_exposure", 0.06),
        },
        "portfolio": {
            "max_realized_corr_share": _f("capital_max_realized_corr_share", 0.75),
        },
    }
