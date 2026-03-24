from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd


POLICY_VERSION = "fxstack_policy_v1"
EDGE_FORMULA_ID = "prob_weighted_opportunity_v2"


@dataclass(slots=True)
class PolicyGateDecision:
    allowed: bool
    reason: str
    policy_version: str = POLICY_VERSION
    edge_formula_id: str = EDGE_FORMULA_ID
    threshold_snapshot: dict[str, float] = field(default_factory=dict)
    spread_unit_source: str = "unknown"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _row_value(row: pd.DataFrame | pd.Series | dict[str, Any], key: str, default: float = 0.0) -> float:
    if isinstance(row, pd.DataFrame):
        if row.empty:
            return float(default)
        return _safe_float(row.iloc[0].get(key, default), default)
    if isinstance(row, pd.Series):
        return _safe_float(row.get(key, default), default)
    if isinstance(row, dict):
        return _safe_float(row.get(key, default), default)
    return float(default)


def _row_has_key(row: pd.DataFrame | pd.Series | dict[str, Any], key: str) -> bool:
    if isinstance(row, pd.DataFrame):
        return str(key) in set(row.columns)
    if isinstance(row, pd.Series):
        return str(key) in set(row.index)
    if isinstance(row, dict):
        return str(key) in row
    return False


def infer_pip_size(*, pair: str = "", digits: int | None = None) -> float:
    if digits is not None:
        if int(digits) in {2, 3}:
            return 0.01
        if int(digits) in {4, 5}:
            return 0.0001
    return 0.01 if str(pair).upper().endswith("JPY") else 0.0001


def directional_swing_confidence(*, swing_prob: float, side: str | None = None) -> float:
    swing_p = max(0.0, min(1.0, _safe_float(swing_prob, 0.5)))
    direction = str(side or ("long" if swing_p >= 0.5 else "short")).strip().lower()
    if direction == "short":
        return 1.0 - swing_p
    return swing_p


def compute_expected_edge_bps(
    row: pd.DataFrame | pd.Series | dict[str, Any],
    *,
    swing_prob: float | None = None,
    entry_prob: float | None = None,
    trade_prob: float | None = None,
    regime_prob: float | None = None,
    side: str | None = None,
) -> float:
    mid = max(1e-9, abs(_row_value(row, "mid_close", 0.0)))
    atr_bps = max(0.0, (_row_value(row, "atr_14", 0.0) / mid) * 10000.0)
    trend_bps = max(
        abs(_row_value(row, "trend_slope_20", 0.0)) * 10000.0,
        abs(_row_value(row, "trend_slope_60", 0.0)) * 10000.0,
    )
    vol_bps = max(abs(_row_value(row, "vol_20", 0.0)) * 10000.0, abs(_row_value(row, "vol_60", 0.0)) * 10000.0)
    opportunity_bps = max(atr_bps, trend_bps, vol_bps)
    if opportunity_bps <= 0.0:
        return 0.0

    if swing_prob is None and entry_prob is None and trade_prob is None and regime_prob is None:
        return float(opportunity_bps)

    swing_p = max(0.0, min(1.0, _safe_float(swing_prob, 0.5)))
    entry_p = max(0.0, min(1.0, _safe_float(entry_prob, 0.5)))
    trade_p = max(0.0, min(1.0, _safe_float(trade_prob, 0.5)))
    regime_p = max(0.0, min(1.0, _safe_float(regime_prob, 0.5)))
    direction = str(side or ("long" if swing_p >= 0.5 else "short")).strip().lower()
    directional_prob = directional_swing_confidence(swing_prob=swing_p, side=direction)
    blended_confidence = (
        (0.35 * directional_prob)
        + (0.25 * entry_p)
        + (0.25 * trade_p)
        + (0.15 * regime_p)
    )
    conviction = max(0.0, min(1.0, (blended_confidence - 0.5) * 2.0))
    return float(opportunity_bps * conviction)


def _normalize_spread_bps_from_unit(
    *,
    spread_value: float,
    unit: str,
    pair: str = "",
    digits: int | None = None,
    mid_price: float = 0.0,
) -> float:
    spread = max(0.0, _safe_float(spread_value, 0.0))
    txt = str(unit).strip().lower()
    mid = max(0.0, _safe_float(mid_price, 0.0))

    if spread <= 0.0:
        return 0.0
    if txt == "bps":
        return spread
    if mid <= 0.0:
        return 0.0
    if txt == "price":
        return float((spread / mid) * 10000.0)

    pip_size = infer_pip_size(pair=pair, digits=digits)
    if txt == "points":
        points_per_pip = 10.0 if int(digits or 0) in {3, 5} else 1.0
        spread = spread / points_per_pip
        txt = "pips"
    if txt == "pips":
        return float(((spread * pip_size) / mid) * 10000.0)
    return 0.0


def normalize_spread_bps(
    *,
    tick: dict[str, Any] | None = None,
    row: pd.DataFrame | pd.Series | dict[str, Any] | None = None,
    pair: str = "",
) -> tuple[float, str]:
    t = dict(tick or {})
    r = row
    mid_from_tick = (_safe_float(t.get("bid"), 0.0) + _safe_float(t.get("ask"), 0.0)) / 2.0
    if mid_from_tick <= 0.0 and r is not None:
        mid_from_tick = _row_value(r, "mid_close", 0.0)
    digits_raw = t.get("digits")
    digits = int(_safe_float(digits_raw, 0.0)) if digits_raw is not None else None

    if "spread_bps" in t and t.get("spread_bps") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_bps"), 0.0),
            unit="bps",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_bps"
    if "spread_points" in t and t.get("spread_points") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_points"), 0.0),
            unit="points",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_points"
    if "spread_pips" in t and t.get("spread_pips") is not None:
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread_pips"), 0.0),
            unit="pips",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_pips"
    if "spread" in t and t.get("spread") is not None:
        # Backward-compatible bridge alias: treat `spread` from ticks as pips.
        return _normalize_spread_bps_from_unit(
            spread_value=_safe_float(t.get("spread"), 0.0),
            unit="pips",
            pair=pair,
            digits=digits,
            mid_price=mid_from_tick,
        ), "tick.spread_legacy_pips"

    if r is not None:
        mid = _row_value(r, "mid_close", 0.0)
        if _row_has_key(r, "spread_bps"):
            return _normalize_spread_bps_from_unit(
                spread_value=_row_value(r, "spread_bps", 0.0),
                unit="bps",
                pair=pair,
                digits=digits,
                mid_price=mid,
            ), "row.spread_bps"
        spread = _row_value(r, "spread", 0.0)
        if spread > 0.0 and mid > 0.0:
            return _normalize_spread_bps_from_unit(
                spread_value=spread,
                unit="price",
                pair=pair,
                digits=digits,
                mid_price=mid,
            ), "row.spread_price"

    return 0.0, "missing"


def gate_decision(
    *,
    swing_prob: float,
    entry_prob: float,
    trade_prob: float,
    spread_bps: float,
    expected_edge_bps: float,
    side: str | None = None,
    min_swing_prob: float,
    min_entry_prob: float,
    min_trade_prob: float,
    max_spread_bps: float,
    min_expected_edge_bps: float,
    spread_unit_source: str = "unknown",
) -> PolicyGateDecision:
    thresholds = {
        "min_swing_prob": float(min_swing_prob),
        "min_entry_prob": float(min_entry_prob),
        "min_trade_prob": float(min_trade_prob),
        "max_spread_bps": float(max_spread_bps),
        "min_expected_edge_bps": float(min_expected_edge_bps),
    }

    if float(spread_bps) > float(max_spread_bps):
        return PolicyGateDecision(
            allowed=False,
            reason="spread_too_wide",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(expected_edge_bps) < float(min_expected_edge_bps):
        return PolicyGateDecision(
            allowed=False,
            reason="edge_below_hurdle",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    directional_swing_prob = directional_swing_confidence(swing_prob=float(swing_prob), side=side)
    if float(directional_swing_prob) < float(min_swing_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="weak_swing",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(entry_prob) < float(min_entry_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="weak_entry",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    if float(trade_prob) < float(min_trade_prob):
        return PolicyGateDecision(
            allowed=False,
            reason="meta_reject",
            threshold_snapshot=thresholds,
            spread_unit_source=str(spread_unit_source or "unknown"),
        )
    return PolicyGateDecision(
        allowed=True,
        reason="approved",
        threshold_snapshot=thresholds,
        spread_unit_source=str(spread_unit_source or "unknown"),
    )
