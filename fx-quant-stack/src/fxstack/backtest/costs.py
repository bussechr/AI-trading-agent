"""All-in trading costs, including the one that was missing.

``all_in_cost_bps`` previously summed spread and slippage only. Financing (swap /
rollover) appeared NOWHERE in this repository -- a repo-wide grep for
``swap|financing|overnight_cost|rollover_cost`` returned zero hits across
backtest/, risk/ and tools/ -- despite the EA already reporting ``OrderSwap()``
(MQL4/Experts/BridgeEA.mq4) and the API already storing it
(fxstack/api/app.py). The field arrived, was validated, was persisted, and was
never charged.

That omission is not cosmetic. Measured on the corrected M5 strategy: 298.6
position-days of exposure and a breakeven financing threshold of **0.340
bps/day**, against typical retail EURUSD swap of ~0.5-2 bps/day. The strategy's
only positive result (+1.02% net of spread) turns NEGATIVE once carry is charged.
An uncharged holding cost was the difference between a profitable-looking and an
unprofitable strategy.

``financing_bps`` is therefore a required argument on the all-in helper. It has no
default on purpose: a default of 0.0 is exactly how this cost went unnoticed, and
a caller that genuinely holds nothing overnight can pass 0.0 explicitly and say so.
"""

from __future__ import annotations


#: THE SIGN CONVENTION, stated once so the seam cannot drift again.
#:
#: Every ``*_bps`` financing value in this module is a COST:
#:     positive => you PAY it, negative => you EARN it.
#:
#: The broker uses the OPPOSITE convention. ``OrderSwap()`` and
#: ``MODE_SWAPLONG/MODE_SWAPSHORT`` report a credit: negative means it was
#: debited from the account, positive means it was paid to you. Anything crossing
#: that boundary must be negated exactly once, which is what
#: ``financing_bps_from_reported_swap`` exists to do.
#:
#: This mattered: the two helpers below originally disagreed, and because each
#: was tested in isolation every test passed while the COMPOSITION inverted
#: carry -- a swap-paying position reduced measured cost and a swap-earning one
#: increased it. Carry is the one alpha family in this stack still untested, so
#: the failure mode was a confidently backwards carry strategy.
FINANCING_SIGN_CONVENTION = "cost_positive"


def financing_bps_for_holding(
    *,
    cost_bps_per_day: float,
    holding_days: float,
) -> float:
    """Financing accrued over a holding period, in basis points, as a COST.

    ``cost_bps_per_day`` follows the module convention: POSITIVE means the
    position pays financing, negative means it earns it. If you are starting from
    a broker-reported swap rate, negate it first (or route it through
    ``financing_bps_from_reported_swap``) -- the broker's sign is a credit, not a
    cost.

    Use the broker's actual per-pair, per-side rate -- MT4 exposes it via
    ``MarketInfo(symbol, MODE_SWAPLONG/MODE_SWAPSHORT)`` -- rather than an assumed
    constant, because the sign flips by side and by pair.
    """

    days = max(0.0, float(holding_days))
    return float(cost_bps_per_day) * days


def financing_bps_from_reported_swap(
    *,
    swap_amount: float,
    notional: float,
) -> float:
    """Convert the broker's REPORTED swap into a financing COST in bps.

    ``OrderSwap()`` is reported by the EA and stored by the API
    (``fxstack/api/app.py``) in account currency, in BROKER sign: negative was
    debited (you paid), positive was credited (you earned).

    This module charges costs as positive, so the sign is INVERTED here -- exactly
    once, at the boundary. A -$20 swap on $100k notional is +2.0 bps of cost; a
    +$15 swap is -1.5 bps, which correctly reduces the all-in cost.

    Returns 0.0 for a non-positive notional rather than dividing by zero.
    """

    size = abs(float(notional))
    if size <= 0.0:
        return 0.0
    # Negate: broker credit -> our cost.
    return -float(swap_amount) / size * 1e4


def all_in_cost_bps(
    *,
    spread_bps: float,
    slippage_bps: float,
    financing_bps: float,
) -> float:
    """Total round-trip cost in basis points.

    Spread and slippage are clamped non-negative (they are always paid).
    Financing is NOT clamped: positive carry is real income and clamping it away
    would understate a carry strategy exactly as omitting it overstated this one.
    """

    return float(
        max(0.0, float(spread_bps))
        + max(0.0, float(slippage_bps))
        + float(financing_bps)
    )
