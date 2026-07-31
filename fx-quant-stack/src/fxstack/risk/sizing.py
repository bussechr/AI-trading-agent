"""Risk-based position sizing: one equation, in money, with one owner.

The incumbent path is ``_entry_order_lots`` in ``runtime/runner.py``:
``lots = equity * equity_lots_per_usd`` (1e-5), floored to the 0.01 lot step and
clamped to 0.10. It has three properties that make the rest of the stack
unmeasurable:

  * It does not know the stop distance, so money at risk per trade is whatever
    the stop happens to be. Widening the stop silently multiplies risk.
  * It does not respond to edge or volatility, so conviction is inexpressible.
  * At realistic equity the result lands within one or two lot steps of the
    0.01 minimum, so the ~15-term sizing computation downstream has an effective
    output alphabet of one or two values -- every sizing "decision" is below the
    broker's quantisation floor.

This module computes lots from an explicit risk fraction and the ACTUAL stop
distance the order will carry:

    lots = (equity * risk_fraction) / (stop_distance_price * value_per_price_unit)

That single change makes stop width risk-neutral (a wider stop buys a smaller
position for the same money), which is the precondition for changing the bracket
geometry at all.

``kelly_fraction`` is offered for the conviction term, deliberately fractional
and capped: full Kelly on a mis-estimated probability is ruinous, and the
probabilities in this stack are not yet trustworthy (the deployed calibrators are
fitted to a preliminary model and applied to a refit one). Default 0.25 of Kelly
with a hard ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

#: FX contract size for a standard lot (100k units of base currency).
STANDARD_LOT_UNITS = 100_000.0


@dataclass(frozen=True)
class SizingResult:
    lots: float
    risk_fraction_used: float
    money_at_risk: float
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.lots > 0.0 and not self.reason


def _finite(value: object, default: float = 0.0) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def quote_value_per_price_unit(*, lots: float = 1.0, contract_units: float = STANDARD_LOT_UNITS) -> float:
    """Account-currency value of a 1.0 move in price, per ``lots``.

    For a quote-currency-denominated account (USD account trading EURUSD) this is
    exactly ``lots * contract_units``: 1.00 of price movement on 1 lot is
    100,000 units of quote currency. Crosses need an FX conversion the caller
    must supply via ``value_per_price_unit`` instead of using this helper.
    """

    return max(0.0, _finite(lots)) * max(0.0, _finite(contract_units))


def usd_per_quote_unit(
    *,
    pair: str,
    rates: "dict[str, float] | Any",
    account_currency: str = "USD",
) -> float:
    """Account-currency value of ONE unit of ``pair``'s quote currency.

    ``lots_for_risk`` needs the contract value in ACCOUNT currency, and the
    100,000 default is only right when the quote currency IS the account
    currency. Of the 18 pairs configured in ``ops/windows/_env.bat`` exactly 4
    satisfy that (EURUSD, GBPUSD, AUDUSD, NZDUSD). For the rest the default is
    wrong, and NOT in a uniformly safe direction -- measured at a 30-pip stop on
    $10k at 0.5%:

        EURGBP  +22% over budget      USDCHF  +7% over budget
        USDCAD  -29% under budget     JPY pairs REFUSED entirely
                                      (~0.0017 lots, below the 0.01 minimum)

    So six JPY pairs could never take a risk-sized entry at all, and two pairs
    silently exceeded the risk budget.

    Resolution order, from whatever live rates are available:
      * quote == account currency          -> 1.0
      * direct pair  ``<account><quote>``  -> 1 / rate   (USDJPY: USD per JPY)
      * inverse pair ``<quote><account>``  -> rate       (GBPUSD: USD per GBP)

    Returns 0.0 when it cannot be resolved. That is deliberate: the caller must
    then decline to risk-size rather than fall back to a default that is known to
    be wrong, because the failure is silent and can over-risk.
    """

    sym = str(pair or "").strip().upper()
    acct = str(account_currency or "USD").strip().upper()
    if len(sym) < 6 or len(acct) != 3:
        return 0.0
    quote = sym[3:6]
    if quote == acct:
        return 1.0
    table = {str(k).strip().upper(): _finite(v) for k, v in dict(rates or {}).items()}
    direct = table.get(f"{acct}{quote}", 0.0)
    if direct > 0.0:
        return float(1.0 / direct)
    inverse = table.get(f"{quote}{acct}", 0.0)
    if inverse > 0.0:
        return float(inverse)
    return 0.0


def account_value_per_price_unit(
    *,
    pair: str,
    rates: "dict[str, float] | Any",
    account_currency: str = "USD",
    contract_units: float = STANDARD_LOT_UNITS,
) -> float:
    """Contract value per 1.0 of price move, per lot, in ACCOUNT currency.

    Feed this to ``lots_for_risk(value_per_price_unit=...)``. Returns 0.0 when the
    conversion rate is unavailable, which ``lots_for_risk`` already treats as
    ``non_positive_contract_value`` and refuses -- fail closed, never guess.
    """

    unit = usd_per_quote_unit(pair=pair, rates=rates, account_currency=account_currency)
    if unit <= 0.0:
        return 0.0
    return float(max(0.0, _finite(contract_units)) * unit)


def kelly_fraction(
    *,
    win_probability: float,
    reward_risk_ratio: float,
    fraction_of_kelly: float = 0.25,
    max_fraction: float = 0.02,
) -> float:
    """Fractional-Kelly risk fraction for a fixed-bracket bet.

    For a bet that wins ``b`` units per unit risked with probability ``p``:
    ``f* = (p*(b+1) - 1) / b``. A non-positive ``f*`` means the bet has no
    positive expectancy at that probability, and the correct size is zero.

    The result is scaled by ``fraction_of_kelly`` and hard-capped by
    ``max_fraction``, because Kelly is extremely sensitive to error in ``p`` and
    these probabilities are not yet calibrated out-of-sample.
    """

    p = _finite(win_probability)
    b = _finite(reward_risk_ratio)
    if not (0.0 < p < 1.0) or b <= 0.0:
        return 0.0
    edge = (p * (b + 1.0)) - 1.0
    if edge <= 0.0:
        return 0.0
    f_star = edge / b
    scaled = f_star * max(0.0, _finite(fraction_of_kelly))
    return float(min(max(scaled, 0.0), max(0.0, _finite(max_fraction))))


def ewma_volatility(returns: list[float] | Any, *, halflife: int = 20) -> float:
    """EWMA volatility forecast from recent returns.

    Exponential weighting, not a flat window: volatility clusters, so the most
    recent observations carry the most information about the next period. A flat
    rolling window responds to a shock and to its expiry equally hard, which
    makes position size jump for a reason that has nothing to do with the market.
    """

    values = [float(x) for x in (returns or []) if _finite(x, float("nan")) == _finite(x, float("nan"))]
    values = [x for x in values if math.isfinite(x)]
    if len(values) < 2:
        return 0.0
    lam = math.exp(math.log(0.5) / max(1, int(halflife)))
    weight, total_w, total_v = 1.0, 0.0, 0.0
    for x in reversed(values):
        total_w += weight
        total_v += weight * (x * x)
        weight *= lam
    if total_w <= 0.0:
        return 0.0
    var = total_v / total_w
    return float(math.sqrt(var)) if var > 0.0 else 0.0


def volatility_targeted_fraction(
    *,
    base_fraction: float,
    forecast_volatility: float,
    target_volatility: float,
    max_scale: float = 3.0,
    min_scale: float = 0.25,
) -> float:
    """Scale risk-per-trade so realized risk is stable across volatility regimes.

    ``f = base * (target_vol / forecast_vol)``, clamped.

    This is the cheapest genuine improvement in the whole risk stack because it
    requires NO forecast of direction. Holding notional constant while volatility
    doubles doubles the risk actually taken; holding *risk* constant instead
    raises the geometric mean by cutting volatility drag, which is why a
    vol-targeted version of an unchanged signal generally beats the raw one.

    Clamps matter as much as the formula: an unclamped ratio explodes when a
    quiet window produces a near-zero forecast, which would size a position on a
    measurement artifact. ``min_scale`` keeps the strategy alive in turbulence
    rather than switching it off entirely at the worst moment.
    """

    base = max(0.0, _finite(base_fraction))
    target = max(0.0, _finite(target_volatility))
    forecast = _finite(forecast_volatility)
    if base <= 0.0 or target <= 0.0:
        return 0.0
    if not math.isfinite(forecast) or forecast <= 0.0:
        # No usable volatility estimate: do NOT scale up on an unknown. Fall back
        # to the unscaled base, which is the conservative reading.
        return float(base)
    scale = target / forecast
    scale = min(max(scale, max(0.0, _finite(min_scale))), max(0.0, _finite(max_scale, 1.0)))
    return float(base * scale)


def drawdown_scaled_fraction(
    *,
    base_fraction: float,
    drawdown_pct: float,
    max_drawdown_pct: float,
    min_scale: float = 0.25,
    exponent: float = 1.0,
) -> float:
    """Shrink risk-per-trade as the account draws down. Anti-martingale.

    The incumbent behaviour is a CLIFF: ``risk/kernel.py`` compares
    ``drawdown_pct <= max_drawdown_pct`` and blocks entries outright at the
    limit, so risk-per-trade is a flat 0.5% of equity all the way to the wall
    and then zero. That is the worst of both shapes -- it takes full size
    through the whole losing run, then switches the strategy off at precisely
    the point where the remaining capital most needs to be able to recover.

    This replaces the step with a ramp: ``f = base * (1 - dd/limit)^exponent``,
    floored at ``min_scale`` so a strategy in drawdown keeps a working position
    size rather than being throttled to nothing before the hard limit is even
    reached. The hard limit in the kernel is unchanged and still binds -- this
    only governs the approach to it.

    Note this is ORTHOGONAL to the volatility normalisation already present.
    ``lots_for_risk`` divides by an ATR-scaled stop, which holds money-at-risk
    constant across INSTRUMENT volatility regimes. This responds to ACCOUNT
    state instead, so the two compose without double-counting the same signal.

    ``exponent`` > 1.0 de-risks more slowly at first and then sharply; 1.0 is a
    straight line and is the default because a curve implies a confidence about
    drawdown dynamics this stack has not earned.

    MEASURED (63,901 real EURUSD M15 bars 2024-01..2026-07, 0.474 pip mean
    spread, full costs; 200 independent 1,500-trade sequences per regime, ramp
    vs flat 0.5% risk over IDENTICAL trades so only sizing differs).

    At the DEPLOYED ``risk_max_drawdown_pct = 5.0``:

        edge regime      baseline    max drawdown        return
        no edge           -26.5%   -20.15pp (200/200)  +18.32pp (t=+36.8)
        slight edge       -18.2%   -15.88pp (200/200)  +12.54pp (t=+21.9)
        solid (~breakeven) -1.3%    -9.37pp (200/200)   +1.09pp (t=+1.69, ns)
        strong edge       +28.8%    -4.43pp (200/200)  -13.79pp (t=-28.4)

    Read that honestly: the ramp ALWAYS reduces drawdown, but its return effect
    CHANGES SIGN at roughly breakeven. On a losing strategy it is free money in
    both directions. On a genuinely profitable one it costs return -- you are
    smallest exactly while recovering -- buying ~4.4pp less drawdown for ~13.8pp
    of return. That is a steep price, and it is a direct consequence of the
    5% limit: the ramp hits ``min_scale`` at only 3.75% drawdown.

    The same test at a 20% limit gives -14.29pp / +12.95pp (no edge) and
    -1.54pp / -4.26pp (strong edge) -- same shape, roughly a third the
    magnitude. So the tightness of ``risk_max_drawdown_pct`` governs how
    aggressive this is; the two parameters are not independent.

    This stack currently measures negative-edge (every price-derived family flat
    gross at every horizon), which is squarely in the region where the ramp is
    unambiguously correct. REVISIT BOTH ``min_scale`` AND
    ``risk_max_drawdown_pct`` if a model ever passes the validation gate: at that
    point the tradeoff inverts and costs ~14pp of return at the deployed limit.
    Deliberately parameters, not constants.
    """

    base = max(0.0, _finite(base_fraction))
    limit = _finite(max_drawdown_pct)
    dd = _finite(drawdown_pct)
    floor = min(max(0.0, _finite(min_scale)), 1.0)
    if base <= 0.0:
        return 0.0
    # No configured limit, or no drawdown: nothing to scale against. Never scale
    # UP on a missing/absurd input -- an unknown is not good news.
    if limit <= 0.0 or dd <= 0.0:
        return float(base)
    if dd >= limit:
        return float(base * floor)
    headroom = 1.0 - (dd / limit)
    exp = _finite(exponent, 1.0)
    if exp <= 0.0:
        exp = 1.0
    scale = headroom**exp
    scale = min(max(scale, floor), 1.0)
    return float(base * scale)


def lots_for_risk(
    *,
    equity: float,
    risk_fraction: float,
    stop_distance_price: float,
    value_per_price_unit: float = STANDARD_LOT_UNITS,
    min_lots: float = 0.01,
    lot_step: float = 0.01,
    max_lots: float = 0.10,
) -> SizingResult:
    """Lots such that a stop-out costs ``equity * risk_fraction``.

    Rounds DOWN to ``lot_step`` so realized risk never exceeds the budget, then
    refuses rather than silently trading a larger-than-budgeted size: if the
    rounded result is below ``min_lots`` the honest answer is "this risk budget
    cannot be expressed at this broker's granularity", not "round up".
    """

    eq = _finite(equity)
    frac = _finite(risk_fraction)
    stop = _finite(stop_distance_price)
    vpu = _finite(value_per_price_unit)
    step = _finite(lot_step, 0.01)
    lo = max(0.0, _finite(min_lots))
    hi = _finite(max_lots)

    if eq <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, "non_positive_equity")
    if frac <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, "non_positive_risk_fraction")
    if stop <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, "non_positive_stop_distance")
    if vpu <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, "non_positive_contract_value")
    if step <= 0.0:
        return SizingResult(0.0, 0.0, 0.0, "invalid_lot_step")

    budget = eq * frac
    raw_lots = budget / (stop * vpu)

    # Round DOWN so realized risk <= budget.
    stepped = math.floor((raw_lots + step * 1e-9) / step) * step
    if hi > 0.0:
        stepped = min(stepped, hi)
    if stepped + (step * 1e-9) < lo:
        return SizingResult(
            0.0,
            float(frac),
            0.0,
            f"risk_budget_below_min_lot:{raw_lots:.6f}<{lo}",
        )

    lots = round(float(stepped), 8)
    money_at_risk = lots * stop * vpu
    return SizingResult(lots, float(frac), float(money_at_risk), "")


def risk_fraction_for_lots(
    *,
    equity: float,
    lots: float,
    stop_distance_price: float,
    value_per_price_unit: float = STANDARD_LOT_UNITS,
) -> float:
    """Inverse of ``lots_for_risk`` -- what fraction of equity is actually at risk.

    Use this to audit the incumbent lot-arithmetic path: it converts an opaque
    ``equity * 1e-5`` lot count into the number that matters, and makes a silent
    risk increase from a wider stop immediately visible.
    """

    eq = _finite(equity)
    stop = _finite(stop_distance_price)
    vpu = _finite(value_per_price_unit)
    lt = _finite(lots)
    if eq <= 0.0 or stop <= 0.0 or vpu <= 0.0 or lt <= 0.0:
        return 0.0
    return float((lt * stop * vpu) / eq)
