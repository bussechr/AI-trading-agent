# Carry enablement — the last untested alpha

Carry (the interest-rate differential earned or paid for holding a position) is
the best-documented durable effect in FX and the only signal family in this stack
that is **structurally independent** of the price-momentum features, which were
measured to carry no gross edge at any horizon (M5 −4.05%, M15 −14.98%,
H1 −17.38%, H4 −11.41%, all before costs).

It is not blocked by data acquisition. Most of the plumbing already exists.

## What already works

| Piece | Location | State |
|---|---|---|
| EA reads realized swap | `MQL4/Experts/BridgeEA.mq4` — `double swap = OrderSwap();` | **done** |
| EA reports it | same file — `,"swap":` in the report JSON | **done** |
| API receives it | `fx-quant-stack/src/fxstack/api/app.py` — `payload.get("swap")` | **done** |
| API stores it | same file — `"swap": swap` in the stored payload | **done** |
| Schema declares it | `fx-quant-stack/src/fxstack/api/schemas.py` | **done** |
| Cost model accepts financing | `fxstack/backtest/costs.py::all_in_cost_bps` (required arg) | **done** |
| Reported swap → bps | `costs.py::financing_bps_from_reported_swap` | **done** |
| Financing over a holding period | `costs.py::financing_bps_for_holding` | **done** |

The field arrives, is validated, is persisted, and until now nothing charged it.

## Why it matters (measured)

The corrected M5 strategy holds **298.6 position-days** and nets **+1.02%** after
spread. Breakeven financing is **0.340 bps/day**. Typical retail EURUSD swap is
**0.5–2 bps/day** — so the only positive result this stack produced is
**negative once carry is charged**. Locked by
`fx-quant-stack/tests/test_backtest_costs.py::test_breakeven_financing_arithmetic_matches_the_measurement`.

## What is missing: the forward carry RATE

`OrderSwap()` is *accrued* swap on an already-open position — realized cost, not a
forecast. A carry **signal** needs the per-pair, per-side rate, which MT4 exposes
but the EA does not poll.

### EA change (needs a MetaTrader compile + demo smoke test)

Add to the symbol/handshake reporting block in `BridgeEA.mq4`:

```mql4
// Forward carry rate, per side, in account currency per lot per day.
// MODE_SWAPLONG/MODE_SWAPSHORT are point-denominated or percentage-denominated
// depending on SYMBOL_SWAP_MODE -- report the mode too so Python can normalise
// rather than guess.
double swapLong  = MarketInfo(brokerSym, MODE_SWAPLONG);
double swapShort = MarketInfo(brokerSym, MODE_SWAPSHORT);
int    swapMode  = (int)MarketInfo(brokerSym, MODE_SWAPTYPE);
// append to the existing symbol payload:
//   ,"swap_long":  DoubleToString(swapLong, 4)
//   ,"swap_short": DoubleToString(swapShort, 4)
//   ,"swap_mode":  IntegerToString(swapMode)
```

**Do not ship this without compiling and smoke-testing on the demo account.** It
touches the file that places orders. The Python side is additive and safe; the EA
side is not.

### Then, Python side

1. Accept `swap_long` / `swap_short` / `swap_mode` in `api/schemas.py` and store
   them alongside the existing `swap` field.
2. Normalise to bps/day per side (branch on `swap_mode`: points vs percent vs
   account-currency) — this is the one genuinely fiddly step, and it must be
   tested per mode, because the sign convention differs.

   **Sign convention — read this before writing the normaliser.** `costs.py` now
   states it once, as `FINANCING_SIGN_CONVENTION = "cost_positive"`:

   | | positive means | negative means |
   |---|---|---|
   | Broker (`OrderSwap`, `MODE_SWAPLONG/SHORT`) | credited to you | debited from you |
   | `costs.py` `financing_bps` | you PAY it | you EARN it |

   They are **opposites**, so anything crossing the boundary must be negated
   exactly once. `financing_bps_from_reported_swap` does that and is the only
   place it should happen — do not negate again downstream.

   This was a live bug, fixed 2026-07-30: `financing_bps_from_reported_swap`
   preserved the broker sign while `all_in_cost_bps` treated positive as a cost,
   so a swap-PAYING position reduced measured cost and a swap-EARNING one
   increased it. Both functions passed their own tests; the inversion only
   existed in the composition. Since carry is the entire point of this document,
   that would have produced a confidently backwards carry strategy. The seam is
   now covered by `test_paying_swap_increases_all_in_cost`,
   `test_earning_swap_decreases_all_in_cost` and
   `test_carry_direction_survives_a_multi_day_hold`.
3. Build the signal: **carry** = rate differential, and **carry-trend** = carry
   conditioned on trend agreement, which is the documented interaction and is
   more robust than carry alone.
4. Run it through the existing gate: `tools/certify_models.py` already applies
   MCPT, block bootstrap, PBO, deflated Sharpe and cost stress against a fixed
   acceptance bar. No new validation machinery is required.

## Honest expectation

Carry at retail spreads on a demo account is not a large effect, and it is
strongest at longer holding horizons where financing accrues in your favour
rather than against you. The reason to test it is not that it is likely to be
big — it is that it is the only remaining hypothesis whose failure would be
*informative*, because every price-derived family in this stack has already been
measured flat.

Test it the same way as everything else: pre-declare the parameter grid, charge
full costs including financing, and let the acceptance gate decide.
