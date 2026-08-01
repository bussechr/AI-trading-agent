"""Scalper sizing: the ONLY sizing authority, built on the verified FX sizer.

Two paths, strictly ordered by evidence quality:

1. BROKER SPECS (preferred): when the EA has published this symbol's
   MarketInfo contract (lot_size, min/step/max lot, stop level, margin), size
   from that truth. This makes crypto CFDs sizeable (1-unit contracts, not
   100k), enforces the broker's real minimum stop distance, and caps lots by
   margin feasibility so approved orders cannot silently bounce (error 134).
2. FX ASSUMPTION (fallback): the legacy 100k-contract math from
   risk/sizing.py -- valid ONLY for FX majors; crypto without specs stays
   honestly unsizeable (``contract_size_unknown``).

Both paths fail closed: rounding is always down, missing conversions refuse,
and every refusal carries a reason the ledger records.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from fxstack.risk.sizing import account_value_per_price_unit, lots_for_risk
from fxstack.scalp.config import ScalpConfig
from fxstack.scalp.gates import CRYPTO_SYMBOLS
from fxstack.scalp.signals import ScalpIntent


@dataclass(slots=True)
class SizedIntent:
    intent: ScalpIntent
    lots: float
    risk_fraction: float
    money_at_risk: float
    sizeable: bool
    reason: str = ""
    margin_capped: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intent"] = self.intent.to_dict()
        return payload


def _refuse(intent: ScalpIntent, frac: float, reason: str) -> SizedIntent:
    return SizedIntent(
        intent=intent,
        lots=0.0,
        risk_fraction=frac,
        money_at_risk=0.0,
        sizeable=False,
        reason=reason,
    )


def size_intent(
    *,
    intent: ScalpIntent,
    equity: float,
    config: ScalpConfig,
    account_currency: str = "USD",
    quote_rates: dict[str, float] | None = None,
    specs: dict[str, dict[str, float]] | None = None,
) -> SizedIntent:
    frac = float(config.risk_fraction)
    if equity <= 0.0:
        return _refuse(intent, frac, "equity_unattested")

    spec = dict((specs or {}).get(intent.symbol) or {})
    if spec:
        return _size_from_broker_spec(
            intent=intent,
            equity=float(equity),
            frac=frac,
            spec=spec,
            config=config,
            account_currency=account_currency,
            quote_rates=dict(quote_rates or {}),
        )

    if intent.symbol in CRYPTO_SYMBOLS:
        # Fail closed rather than mis-size: FX contract math does not apply,
        # and no broker spec has been observed for this symbol.
        return _refuse(intent, frac, "contract_size_unknown")

    vpu = account_value_per_price_unit(
        pair=intent.symbol,
        rates=dict(quote_rates or {}),
        account_currency=account_currency,
    )
    if vpu <= 0.0:
        return _refuse(intent, frac, "conversion_unresolvable")
    stop_distance = abs(intent.entry_price - intent.sl_price)
    sized = lots_for_risk(
        equity=float(equity),
        risk_fraction=frac,
        stop_distance_price=stop_distance,
        value_per_price_unit=vpu,
        min_lots=0.01,
        lot_step=0.01,
        max_lots=0.0,
    )
    if sized.lots <= 0.0:
        return _refuse(intent, frac, str(sized.reason or "unsizeable"))
    return SizedIntent(
        intent=intent,
        lots=float(sized.lots),
        risk_fraction=frac,
        money_at_risk=float(sized.money_at_risk),
        sizeable=True,
    )


def _size_from_broker_spec(
    *,
    intent: ScalpIntent,
    equity: float,
    frac: float,
    spec: dict[str, float],
    config: ScalpConfig,
    account_currency: str,
    quote_rates: dict[str, float],
) -> SizedIntent:
    lot_size = float(spec.get("lot_size") or 0.0)
    if lot_size <= 0.0:
        return _refuse(intent, frac, "contract_size_unknown")

    # Broker minimum stop distance is a hard geometry veto, not a clamp:
    # widening the stop behind the signal's back would change its risk math.
    point = float(spec.get("point") or 0.0)
    stop_level_points = float(spec.get("stop_level_points") or 0.0)
    stop_distance = abs(intent.entry_price - intent.sl_price)
    if point > 0.0 and stop_level_points > 0.0:
        min_stop_px = stop_level_points * point
        if stop_distance < min_stop_px:
            return _refuse(intent, frac, "stop_below_broker_minimum")

    # Quote->account conversion via the verified sizer, with the broker's REAL
    # contract size instead of the 100k FX assumption.
    value_per_price_unit = account_value_per_price_unit(
        pair=intent.symbol,
        rates=quote_rates,
        account_currency=account_currency,
        contract_units=lot_size,
    )
    if value_per_price_unit <= 0.0:
        return _refuse(intent, frac, "conversion_unresolvable")

    min_lot = float(spec.get("min_lot") or 0.01)
    lot_step = float(spec.get("lot_step") or 0.01)
    max_lot = float(spec.get("max_lot") or 0.0)
    sized = lots_for_risk(
        equity=equity,
        risk_fraction=frac,
        stop_distance_price=stop_distance,
        value_per_price_unit=value_per_price_unit,
        min_lots=min_lot if min_lot > 0.0 else 0.01,
        lot_step=lot_step if lot_step > 0.0 else 0.01,
        max_lots=max_lot,
    )
    if sized.lots <= 0.0:
        return _refuse(intent, frac, str(sized.reason or "unsizeable"))

    lots = float(sized.lots)
    money_at_risk = float(sized.money_at_risk)
    margin_capped = False

    # Margin feasibility: an order the broker would bounce with error 134 must
    # be resized here, visibly, not discovered live. margin_required is the
    # broker's account-currency margin per lot at current leverage.
    margin_required = float(spec.get("margin_required") or 0.0)
    if margin_required > 0.0:
        cap = float(config.margin_utilization_cap)
        max_margin_lots = (equity * cap) / margin_required
        if max_margin_lots < lots:
            step = lot_step if lot_step > 0.0 else 0.01
            stepped = int(max_margin_lots / step) * step
            if stepped < (min_lot if min_lot > 0.0 else 0.01):
                return _refuse(intent, frac, "margin_infeasible")
            scale = stepped / lots
            lots = stepped
            money_at_risk *= scale
            margin_capped = True

    return SizedIntent(
        intent=intent,
        lots=lots,
        risk_fraction=frac,
        money_at_risk=money_at_risk,
        sizeable=True,
        margin_capped=margin_capped,
    )
