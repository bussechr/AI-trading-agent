"""Concrete ``RiskKernelConfig.order_builder`` implementations.

``RiskKernelConfig.order_builder`` has been declared since the kernel was written
(``risk/kernel.py:48``) and never assigned, so the kernel's risk-percent sizing
branch is unreachable: supplying ``target_risk_pct`` without a builder rejects
with ``target_risk_pct_requires_custom_order_builder`` (``kernel.py:259-260``),
and every order ever sent carried ``risk_budget_pct = 0.0``. The one place in the
stack architected for risk-based sizing was dead code.

This module supplies the missing half. ``risk_based_order_builder`` derives lots
from an explicit risk fraction and the ACTUAL stop the order will carry, so:

  * money at risk per trade is a stated number rather than a side effect of the
    stop distance, which is the precondition for changing the bracket geometry
    (measured: the deployed ``max(1.2*ATR, 5 pip)`` stop with a 4R target is
    ~-0.105 R per trade on random entries, and a wider stop cuts that by ~40%
    -- but only if widening the stop does not also multiply risk); and
  * the sizing "decision" stops being pinned to the broker's 0.01 lot minimum.

Wiring is deliberately explicit and opt-in -- assign the builder onto the config
where the kernel is constructed. Nothing here changes behaviour until that
assignment happens, and the assignment is a live-sizing change that belongs to
whoever owns the account.
"""

from __future__ import annotations

from typing import Any, Callable

from fxstack.risk.sizing import STANDARD_LOT_UNITS, kelly_fraction, lots_for_risk

#: Conservative default risk per trade, as a fraction of equity.
DEFAULT_RISK_FRACTION = 0.005  # 0.5%


def _f(value: Any, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if out == out and abs(out) != float("inf") else default


def stop_distance_from_intent(intent: Any, market: Any) -> float:
    """Absolute stop distance in price units, from the order's own SL.

    Prefers an explicit ``stop_distance`` in metadata, then derives it from
    ``sl_price`` against the side-correct entry price. Returns 0.0 when it cannot
    be established -- callers must treat that as "cannot size", never as "no
    stop", because sizing without a stop distance is what produced the original
    problem.
    """

    meta = dict(getattr(intent, "metadata", {}) or {})
    explicit = abs(_f(meta.get("stop_distance")))
    if explicit > 0.0:
        return explicit

    sl = _f(meta.get("sl_price"))
    if sl <= 0.0:
        return 0.0

    side = str(getattr(intent, "side", "")).upper()
    bid = _f(getattr(market, "bid", 0.0)) or _f(meta.get("bid"))
    ask = _f(getattr(market, "ask", 0.0)) or _f(meta.get("ask"))
    entry = ask if side in {"BUY", "LONG"} else bid
    if entry <= 0.0:
        entry = _f(meta.get("entry_price"))
    if entry <= 0.0:
        return 0.0
    return abs(entry - sl)


def risk_based_order_builder(
    *,
    risk_fraction: float = DEFAULT_RISK_FRACTION,
    min_lots: float = 0.01,
    lot_step: float = 0.01,
    max_lots: float = 0.10,
    value_per_price_unit: float = STANDARD_LOT_UNITS,
    use_kelly: bool = False,
    fraction_of_kelly: float = 0.25,
) -> Callable[[Any, Any, Any], Any]:
    """Build a kernel ``order_builder`` that sizes by risk, not by lot arithmetic.

    Returns ``None`` (i.e. no approved order) whenever risk cannot be expressed
    honestly: no stop distance, no equity, or a budget too small for the broker's
    lot granularity. Returning ``None`` is a refusal, which the kernel already
    treats as "no order" -- strictly safer than emitting a size that does not
    match the stated risk.

    ``use_kelly`` scales the risk fraction by fractional Kelly using the intent's
    ``confidence`` as p and the order's own reward:risk. Left OFF by default: the
    deployed probability calibrators are fitted to a preliminary model and applied
    to a refit one, so ``confidence`` is not yet trustworthy enough to size on.
    """

    from fxstack.risk.contracts import ApprovedOrderIntent  # local: avoid cycles

    def _build(intent: Any, market: Any, portfolio: Any) -> Any:
        side_up = str(getattr(intent, "side", "")).upper()
        if side_up not in {"BUY", "SELL", "LONG", "SHORT"}:
            return None
        command = "BUY" if side_up in {"BUY", "LONG"} else "SELL"

        meta = dict(getattr(intent, "metadata", {}) or {})
        stop_distance = stop_distance_from_intent(intent, market)
        if stop_distance <= 0.0:
            return None

        equity = _f(getattr(portfolio, "equity", 0.0)) or _f(meta.get("equity"))
        if equity <= 0.0:
            return None

        frac = max(0.0, _f(risk_fraction, DEFAULT_RISK_FRACTION))
        if use_kelly:
            tp = _f(meta.get("tp_price"))
            entry = _f(meta.get("entry_price")) or (
                _f(getattr(market, "ask", 0.0)) if command == "BUY" else _f(getattr(market, "bid", 0.0))
            )
            reward_risk = 0.0
            if tp > 0.0 and entry > 0.0 and stop_distance > 0.0:
                reward_risk = abs(tp - entry) / stop_distance
            frac = kelly_fraction(
                win_probability=_f(getattr(intent, "confidence", 0.0)),
                reward_risk_ratio=reward_risk,
                fraction_of_kelly=fraction_of_kelly,
                max_fraction=frac,
            )
            if frac <= 0.0:
                return None  # no positive-expectancy size at this probability

        sized = lots_for_risk(
            equity=equity,
            risk_fraction=frac,
            stop_distance_price=stop_distance,
            value_per_price_unit=value_per_price_unit,
            min_lots=min_lots,
            lot_step=lot_step,
            max_lots=max_lots,
        )
        if not sized.ok:
            return None

        return ApprovedOrderIntent(
            command=command,
            symbol=str(getattr(intent, "pair", "")).upper(),
            lots=float(sized.lots),
            close_lots=0.0,
            side=side_up,
            intent=str(getattr(intent, "intent", "")).upper(),
            action=str(getattr(intent, "action", "") or "entry"),
            action_score=max(0.0, min(1.0, _f(getattr(intent, "action_score", 0.0)))),
            tp_price=meta.get("tp_price"),
            sl_price=meta.get("sl_price"),
            risk_budget_pct=float(frac),
            lifecycle_action="entry",
            metadata={
                **meta,
                "sizing_source": "risk_based_order_builder",
                "sizing_stop_distance": float(stop_distance),
                "sizing_money_at_risk": float(sized.money_at_risk),
                "sizing_risk_fraction": float(frac),
            },
        )

    return _build
