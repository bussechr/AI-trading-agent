"""Scalper sizing: the ONLY sizing authority, built on the verified FX sizer.

Reuses risk/sizing.py's fail-closed money math (rounds down, refuses sub-min-
lot, refuses to guess conversions). Crypto CFDs are honestly UNSIZEABLE here:
IG's crypto contract sizes are per-symbol (1 BTC per lot, not 100k units) and
the FX contract assumption would over-size by orders of magnitude -- so crypto
intents carry lots=0 with reason ``contract_size_unknown`` and shadow PnL is
tracked in R-units instead. Wiring MODE_LOTSIZE from the EA lifts this later.
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

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["intent"] = self.intent.to_dict()
        return payload


def size_intent(
    *,
    intent: ScalpIntent,
    equity: float,
    config: ScalpConfig,
    account_currency: str = "USD",
    quote_rates: dict[str, float] | None = None,
) -> SizedIntent:
    frac = float(config.risk_fraction)
    if intent.symbol in CRYPTO_SYMBOLS:
        # Fail closed rather than mis-size: FX contract math does not apply.
        return SizedIntent(
            intent=intent,
            lots=0.0,
            risk_fraction=frac,
            money_at_risk=0.0,
            sizeable=False,
            reason="contract_size_unknown",
        )
    if equity <= 0.0:
        return SizedIntent(
            intent=intent,
            lots=0.0,
            risk_fraction=frac,
            money_at_risk=0.0,
            sizeable=False,
            reason="equity_unattested",
        )
    vpu = account_value_per_price_unit(
        pair=intent.symbol,
        rates=dict(quote_rates or {}),
        account_currency=account_currency,
    )
    if vpu <= 0.0:
        return SizedIntent(
            intent=intent,
            lots=0.0,
            risk_fraction=frac,
            money_at_risk=0.0,
            sizeable=False,
            reason="conversion_unresolvable",
        )
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
        return SizedIntent(
            intent=intent,
            lots=0.0,
            risk_fraction=frac,
            money_at_risk=0.0,
            sizeable=False,
            reason=str(sized.reason or "unsizeable"),
        )
    return SizedIntent(
        intent=intent,
        lots=float(sized.lots),
        risk_fraction=frac,
        money_at_risk=float(sized.money_at_risk),
        sizeable=True,
    )
