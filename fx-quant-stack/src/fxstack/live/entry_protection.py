# AGENT: ROLE: Pure broker-side entry protection geometry shared by live runtime
# and offline research.
# AGENT: ENTRYPOINT: `entry_protection_prices`.
# AGENT: STATE / SIDE EFFECTS: none; no settings, runtime, bridge, store, broker,
# or I/O access.
"""Construct deterministic ATR-based stop-loss and take-profit geometry."""

from __future__ import annotations

import math
from typing import Any

from fxstack.live.policy import infer_pip_size


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except Exception:
        return float(default)
    return float(out) if math.isfinite(out) else float(default)


def entry_protection_prices(
    *,
    pair: str,
    side: str,
    tick: dict[str, Any],
    row: Any,
    settings: Any,
) -> tuple[dict[str, float | str], str]:
    """Construct mandatory broker-side SL/TP from quote and closed-bar ATR.

    In adaptive managed mode the take-profit is a distant broker-side fail-safe;
    normal profit taking belongs to the lifecycle partial/exit path. The stop
    geometry is identical in both modes.
    """

    side_up = str(side or "").strip().upper()
    tick_payload = dict(tick or {})
    bid = _safe_float(tick_payload.get("bid"), 0.0)
    ask = _safe_float(tick_payload.get("ask"), 0.0)
    if side_up not in {"BUY", "SELL"}:
        return {}, "entry_protection_invalid_side"
    if not (math.isfinite(bid) and math.isfinite(ask) and bid > 0.0 and ask >= bid):
        return {}, "entry_protection_missing_quote"

    row_get = getattr(row, "get", None)
    atr_raw = row_get("atr_14", 0.0) if callable(row_get) else 0.0
    atr = _safe_float(atr_raw, 0.0)
    stop_multiple = _safe_float(
        getattr(settings, "entry_stop_atr_multiple", 1.2), 0.0
    )
    target_multiple = _safe_float(
        getattr(settings, "entry_take_profit_atr_multiple", 1.5), 0.0
    )
    managed_runner_tp_r = _safe_float(
        getattr(settings, "managed_runner_tp_r_multiple", 0.0), 0.0
    )
    managed_runner_mode = bool(
        getattr(settings, "adaptive_execution_enabled", False)
        and getattr(settings, "enable_lifecycle_actions", False)
        and managed_runner_tp_r >= 1.0
    )
    min_stop_pips = _safe_float(
        getattr(settings, "entry_min_stop_pips", 5.0), 0.0
    )
    if not math.isfinite(atr) or atr <= 0.0:
        return {}, "entry_protection_invalid_atr"
    if stop_multiple <= 0.0 or target_multiple <= 0.0 or min_stop_pips <= 0.0:
        return {}, "entry_protection_invalid_config"

    digits_raw = int(_safe_float(tick_payload.get("digits"), 0.0))
    digits = digits_raw if digits_raw in {2, 3, 4, 5} else None
    pip_size = infer_pip_size(pair=str(pair), digits=digits)
    point_default = pip_size / (10.0 if digits in {3, 5} else 1.0)
    point_size = _safe_float(tick_payload.get("point"), point_default)
    if point_size <= 0.0:
        point_size = point_default
    stops_level = max(
        0.0,
        *(
            _safe_float(tick_payload.get(name), 0.0)
            for name in ("stops_level", "stop_level", "trade_stops_level")
        ),
    )
    broker_distance = max(
        stops_level * point_size,
        _safe_float(tick_payload.get("min_stop_distance"), 0.0),
    )
    stop_distance = max(
        atr * stop_multiple,
        min_stop_pips * pip_size,
        broker_distance,
    )
    reward_ratio = target_multiple / stop_multiple
    target_distance = max(
        atr * target_multiple,
        stop_distance * reward_ratio,
        broker_distance,
    )
    entry_price = ask if side_up == "BUY" else bid

    if side_up == "BUY":
        sl_price = min(entry_price - stop_distance, bid - broker_distance)
        tp_price = max(entry_price + target_distance, ask + broker_distance)
    else:
        sl_price = max(entry_price + stop_distance, ask + broker_distance)
        tp_price = min(entry_price - target_distance, bid - broker_distance)
    if digits is not None:
        quantum = 10.0 ** (-digits)
        entry_price = round(entry_price, digits)
        if side_up == "BUY":
            sl_price = math.floor((sl_price / quantum) + 1e-9) * quantum
            tp_price = math.ceil((tp_price / quantum) - 1e-9) * quantum
        else:
            sl_price = math.ceil((sl_price / quantum) - 1e-9) * quantum
            tp_price = math.floor((tp_price / quantum) + 1e-9) * quantum
        sl_price = round(sl_price, digits)
        tp_price = round(tp_price, digits)

    actual_stop_distance = abs(entry_price - sl_price)
    if managed_runner_mode:
        managed_target_distance = max(
            abs(tp_price - entry_price),
            actual_stop_distance * managed_runner_tp_r,
        )
        if side_up == "BUY":
            managed_tp = entry_price + managed_target_distance
            if digits is not None:
                managed_tp = math.ceil((managed_tp / quantum) - 1e-9) * quantum
                managed_tp = round(managed_tp, digits)
            tp_price = max(tp_price, managed_tp)
        else:
            managed_tp = entry_price - managed_target_distance
            if digits is not None:
                managed_tp = math.floor((managed_tp / quantum) + 1e-9) * quantum
                managed_tp = round(managed_tp, digits)
            tp_price = min(tp_price, managed_tp)

    actual_target_distance = abs(tp_price - entry_price)
    if digits is not None:
        entry_ticks = int(round(entry_price / quantum))
        stop_ticks = abs(int(round(sl_price / quantum)) - entry_ticks)
        target_ticks = abs(int(round(tp_price / quantum)) - entry_ticks)
        actual_stop_distance = stop_ticks * quantum
        actual_target_distance = target_ticks * quantum
        effective_reward_ratio = target_ticks / max(stop_ticks, 1)
    else:
        effective_reward_ratio = actual_target_distance / max(
            actual_stop_distance, 1e-12
        )

    valid = (
        math.isfinite(sl_price)
        and math.isfinite(tp_price)
        and sl_price > 0.0
        and tp_price > 0.0
        and (
            (side_up == "BUY" and sl_price < bid <= ask < tp_price)
            or (side_up == "SELL" and tp_price < bid <= ask < sl_price)
        )
    )
    if not valid:
        return {}, "entry_protection_invalid_prices"
    return (
        {
            "entry_price": float(entry_price),
            "sl_price": float(sl_price),
            "tp_price": float(tp_price),
            "atr_14": float(atr),
            "stop_distance": float(actual_stop_distance),
            "target_distance": float(actual_target_distance),
            "reward_ratio": float(effective_reward_ratio),
            "managed_runner_tp_r_multiple": float(
                managed_runner_tp_r if managed_runner_mode else 0.0
            ),
            "protection_mode": (
                "managed_runner_fail_safe"
                if managed_runner_mode
                else "fixed_atr_target"
            ),
            "broker_min_distance": float(broker_distance),
            "source": "closed_bar_atr_14",
        },
        "",
    )


__all__ = ["entry_protection_prices"]
