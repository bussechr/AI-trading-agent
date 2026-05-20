"""Lot-sizing, partial-close planning, and position-signature helpers.

Carved out of ``fxstack.runtime.runner``. These functions sit between the
risk-decision layer (which decides "close 30% of this") and the
broker-facing command payload (which needs a quantized lot count plus a
"exit/partial_tp/hold" verdict).

All functions are pure — no I/O, no clock reads. Inputs:

* For sizing: open lots, requested fraction, broker quantization (min lot,
  lot step, max lot).
* For partial-close guards: a tracker dict (per-position state the runner
  maintains), the current loop timestamp, and a settings object the runner
  consults for ``max_partial_closes_per_position`` and
  ``partial_close_cooldown_secs``.

Position-signature helpers translate broker-reported position dicts into a
stable string key so the runner can dedupe and prune state across cycles.

The runner.py re-imports each of these under its original underscored
name so existing callers (and tests that reach into runner internals)
continue to work unchanged.
"""

from __future__ import annotations

import math
from typing import Any

from fxstack.runtime._util import safe_float


def round_lot_size(*, lots: float, min_lot: float, lot_step: float, max_lot: float) -> float:
    """Quantize a desired lot size to the broker's min/step/max grid.

    Floors to the nearest multiple of ``lot_step`` (with a small epsilon for
    floating-point safety), then clamps to ``[min_lot, max_lot]``. Returns
    the result rounded to a sane number of decimal places based on the step
    granularity, so wire payloads don't carry 12 trailing zeros.
    """
    step = max(1e-9, float(lot_step))
    minimum = max(0.0, float(min_lot))
    maximum = max(0.0, float(max_lot))
    raw = max(0.0, float(lots))
    quantized = math.floor((raw / step) + 1e-9) * step
    quantized = max(minimum, quantized)
    if maximum > 0.0:
        quantized = min(maximum, quantized)
    decimals = max(0, int(round(-math.log10(step)))) if step < 1.0 else 0
    return round(float(quantized), decimals)


def position_side(positions: list[dict[str, Any]]) -> str:
    """Determine net side ('long'/'short'/'flat') from a list of positions.

    Reads the first parseable ``type`` (or ``order_type`` / ``position_type``)
    field across positions, with MT4 conventions: 0 = long, 1 = short.
    Falls back to string fields (``side``, ``position_side``, ``direction``,
    ``cmd``) for non-numeric broker payloads. Returns ``'flat'`` if nothing
    parses.
    """
    if not positions:
        return "flat"
    for raw in positions:
        pos = dict(raw or {})
        for key in ("type", "order_type", "position_type"):
            value = pos.get(key)
            if value is None or str(value).strip() == "":
                continue
            try:
                typ = int(float(value))
            except Exception:
                typ = -1
            if typ == 0:
                return "long"
            if typ == 1:
                return "short"
            txt = str(value).strip().lower()
            if txt in {"buy", "long", "op_buy"}:
                return "long"
            if txt in {"sell", "short", "op_sell"}:
                return "short"
        for key in ("side", "position_side", "direction", "cmd"):
            txt = str(pos.get(key) or "").strip().lower()
            if txt in {"buy", "long"}:
                return "long"
            if txt in {"sell", "short"}:
                return "short"
    return "flat"


def partial_close_plan(*, lots_open: float, fraction: float, settings: Any) -> tuple[str, float]:
    """Translate "close fraction X of an open position" into a wire-ready plan.

    Returns ``("hold", 0.0)``, ``("partial_tp", lots_to_close)``, or
    ``("exit", full_lots)``. Picks 'exit' when the remaining residue after
    the partial close would be below ``min_order_lots`` — the broker can't
    hold a sub-minimum residue and would reject the order otherwise.
    """
    open_lots = max(0.0, float(lots_open))
    close_fraction = max(0.0, float(fraction))
    if open_lots <= 0.0 or close_fraction <= 0.0:
        return "hold", 0.0

    min_lot = max(0.0, safe_float(getattr(settings, "min_order_lots", 0.01), 0.01))
    lot_step = max(1e-9, safe_float(getattr(settings, "order_lot_step", 0.01), 0.01))
    requested_close = open_lots * close_fraction
    rounded_close = round_lot_size(
        lots=requested_close,
        min_lot=min_lot,
        lot_step=lot_step,
        max_lot=open_lots,
    )
    tolerance = max(1e-9, lot_step / 10.0)
    remaining_lots = max(0.0, open_lots - rounded_close)
    if rounded_close <= 0.0:
        return "hold", 0.0
    if rounded_close >= (open_lots - tolerance):
        return "exit", round(float(open_lots), 8)
    if 0.0 < remaining_lots < (min_lot - tolerance):
        return "exit", round(float(open_lots), 8)
    return "partial_tp", round(float(rounded_close), 8)


def position_signature(position: dict[str, Any]) -> str:
    """Stable string key for a broker-reported position.

    Comprises symbol, side, open_time, open_price (8 decimals), and magic
    number. Used by the runner to dedupe positions and to prune the
    partial-close tracker once a position no longer exists.
    """
    pos = dict(position or {})
    symbol = str(pos.get("symbol") or pos.get("broker_symbol") or "").strip().upper()
    side = position_side([pos])
    try:
        open_time = int(float(pos.get("open_time", 0.0) or 0.0))
    except Exception:
        open_time = 0
    open_price = safe_float(pos.get("open_price", 0.0), 0.0)
    try:
        magic = int(float(pos.get("magic", 0.0) or 0.0))
    except Exception:
        magic = 0
    return f"{symbol}|{side}|{open_time}|{float(open_price):.8f}|{magic}"


def active_position_signatures(state: dict[str, Any]) -> set[str]:
    """Collect signatures of every open position in the runtime state."""
    out: set[str] = set()
    for raw in list(state.get("positions", []) or []):
        key = position_signature(dict(raw or {}))
        if key:
            out.add(key)
    return out


def prune_partial_close_tracker(
    tracker: dict[str, dict[str, Any]],
    *,
    active_signatures: set[str],
) -> None:
    """Drop tracker entries whose position is no longer open.

    Mutates ``tracker`` in place. The runner calls this each cycle so the
    tracker doesn't grow unbounded as positions close.
    """
    for key in list(tracker.keys()):
        if key not in active_signatures:
            tracker.pop(key, None)


def partial_close_guard(
    *,
    tracker_state: dict[str, Any] | None,
    loop_ts: float,
    settings: Any,
) -> tuple[bool, str, float]:
    """Decide whether another partial close is permitted right now.

    Returns ``(allowed, reason, cooldown_remaining_secs)``. Blocks when
    either the per-position partial count cap is hit or the configured
    cooldown window has not yet elapsed since the last partial close.
    """
    state = dict(tracker_state or {})
    max_partials = max(0, int(getattr(settings, "max_partial_closes_per_position", 0) or 0))
    partial_count = max(0, int(state.get("count", 0) or 0))
    if max_partials > 0 and partial_count >= max_partials:
        return False, "partial_tp_limit_reached", 0.0

    cooldown_secs = max(0.0, safe_float(getattr(settings, "partial_close_cooldown_secs", 0.0), 0.0))
    last_partial_ts = safe_float(state.get("last_partial_ts", 0.0), 0.0)
    if cooldown_secs > 0.0 and last_partial_ts > 0.0:
        elapsed = max(0.0, float(loop_ts) - float(last_partial_ts))
        remaining = max(0.0, float(cooldown_secs) - float(elapsed))
        if remaining > 0.0:
            return False, "partial_tp_cooldown_active", float(remaining)

    return True, "", 0.0


__all__ = [
    "active_position_signatures",
    "partial_close_guard",
    "partial_close_plan",
    "position_side",
    "position_signature",
    "prune_partial_close_tracker",
    "round_lot_size",
]
