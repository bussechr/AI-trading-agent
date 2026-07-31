# Edge Audit — EURUSD M15, 2026-07-31

Measured answer to: **does anything in this stack predict EURUSD forward returns?**

Six independent probes over real history (n up to 123,160 bars), each positive
claim then adversarially verified by an agent that re-derived the pipeline from
source rather than trusting reported numbers.

**Result: zero signals survived verification.**

---

## 1. The timeframe is not the problem

Cost is 3–9% of the typical forward move; 91–99% of M15 bars move more than the
0.52 bps spread. M15 EURUSD is tradeable in principle.

Viability requires a sustained out-of-sample Spearman IC of roughly **0.06–0.08**
(a 53–55% directional hit rate). Nothing measured here reaches it. Changing
timeframe does not address the problem.

## 2. The causal plumbing is sound

This is worth stating because it was checked hard and it held. The verifier
attempted to break lookahead and could not:

- `add_fx_lifecycle_features` uses only trailing rolling / `shift(+k)` / expanding
  windows. `mae_proxy_12` / `mfe_proxy_12` are backward excursions.
- `multi_tf_contract._merge_context_asof` is a backward `merge_asof` on
  `anchor_close_ts >= {tf}_close_ts`, with stale rows dropped.
- `resample_bars` uses `label="left", closed="left"`, so bucket boundaries are
  exact and no partial future bar leaks in.
- `_causal_cross_section_stats` reads endpoint indices only; `precompression_avg_12`
  is `.shift(1).rolling(12)`; `_bars_since_impulse` never looks past `i`.

An independent reimplementation matched production to **max abs diff 0.000e+00**
on 800 probes. The data engineering is not where the problem is.

## 3. The models are memorisation, and cannot currently be evaluated honestly

| measurement | value |
|---|---|
| in-sample intraday P(up) vs +2h return | rho **+0.410** |
| in-sample meta vs side-adjusted return | rho **+0.458** |
| best \|rho\| of any *input feature* vs forward return | **0.037** |
| honest walk-forward refit, all directional | **+0.02**, net P&L negative at every horizon |

No input carries more than 0.037, so a model reporting 0.41 is remembering, not
predicting.

> **Contamination warning.** Artifact training windows end 2026-07-17; on-disk
> data ends 2026-07-20/21. **Any backtest in this repo scoring these artifacts
> over this window is reading its own training labels.** This invalidates prior
> backtest results over the window. Every honest number above came from
> refitting the model *spec* walk-forward — never from the shipped weights,
> which cannot be evaluated until more data accumulates.

## 4. The playbook scores are a re-encoding of trailing return

The headline directional finding (trend_pullback rho −0.0250, p=1.6e-18,
monotone deciles) reproduced exactly — and then failed on every other axis:

- **Collinearity.** `dir(ret_60)` alone gives rho −0.0377 at h=8, *stronger than
  any playbook*. Partialling out `{ret_1, ret_5, ret_20, ret_60}`:
  trend_pullback −0.0232 → **−0.0035 (p=0.22)**; range_mean_reversion
  +0.0185 → **−0.0024, sign flips**. The four scores inter-correlate 0.57–0.77.
  They are one collinear factor, not four sleeves.
- **Instability.** 2026Q1 rho +0.0060 (p=0.52); 2026Q2 +0.0025 (p=0.78).
- **Economics.** The 0.53/0.51 bps decile spread is a long-short construct
  costing 2 × 0.52 = 1.04 bps. Every decile of both scores is net-negative.

### Do not trust small p-values from this pipeline

`p = 1.6e-18` was inflated by roughly **14 orders of magnitude**:

- **Pseudo-replication** — each bar counted twice for long/short; the two scores
  correlate −0.87 and their returns −1.00.
- **8× overlapping forward windows** at h=8.

Honest non-overlapping median p: **0.060** at h=8, **0.32** at h=24, **0.47** at
h=96. Always de-overlap windows and de-duplicate sides before believing a
p-value produced here.

## 5. The one durable observation

Not a tradeable inversion — a diagnostic:

> `trend_pullback`-selected bars are gross-negative on **both** sides at all four
> horizons (long −0.12 / −0.23 / −0.50 / −0.08; short −0.20 / −0.40 / −0.81 /
> −1.79 bps) and **lose to the engine's own `no_trade` rows.**

The playbook reliably selects bad bars. It cannot be profitably inverted — the
effect is sub-cost either way — but it is measurably worse than the engine doing
nothing. It was also the only sleeve enabled by `ops/windows/_env.bat` default.

## 6. Open lead — under verification

`meta_prob` vs `|forward return|`: rho **+0.12**, p<1e-30 — "predicts *whether*
EURUSD moves, never *which way*".

This sat inside a probe whose overall verdict was `no_signal`, so the first
audit's verification phase never examined it. It is the most promising remaining
lead **and it has not faced the scrutiny that destroyed the directional claims**.
It is being attacked separately on four axes: collinearity with trailing
volatility, overlap/replication inflation, in-sample contamination, and whether a
volatility edge can be monetised at all on a spot MT4 account with no options.

Treat as unconfirmed until that lands.

---

## What the evidence supports

1. **Kill `trend_pullback`** — it selects bars worse than not trading.
2. **Retire the directional models** as overfit; do not cite their in-sample
   numbers.
3. **Verify the volatility lead before building on it.** If it holds, route it to
   position sizing and bracket geometry — `risk/sizing.py` already implements
   vol targeting and is currently fed nothing — not to directional entries.
4. **Stop adding decision layers.** Every gate only subtracts trades; none
   creates edge.
