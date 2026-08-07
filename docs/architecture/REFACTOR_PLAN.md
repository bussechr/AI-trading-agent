# Architecture Refactor Plan

Status: in progress. Baseline pinned green at 1,525 tests / 0 failures before
any structural change (18 pre-existing failures triaged and fixed first).

## Why

The stack works, but its shape fights it:

| Problem | Evidence |
|---|---|
| One god-module owns the whole decision path | `runtime/runner.py` is 14,070 lines, ~200 private functions, and `run_loop` alone is 2,536 lines |
| Handoffs are untyped dicts | Every stage passes `dict[str, Any]`; a typo in a key is a silent no-op, not an error |
| Money management is half-wired | `risk/sizing.py` implements Kelly, EWMA vol targeting and drawdown scaling; the runner imports only 2 of 6 functions |
| Strategy overlap is unmeasured | Four playbooks run concurrently with no test that they are actually complementary |
| Rescue paths duplicate the decision rule | "aggressive fallback" re-decides what the utility comparison already decided |
| Errors are swallowed into wrong codes | A broad `except Exception` in `service.py` turned an `AssertionError` into a misleading 503 `reconciliation_check_failed` |

## Target shape

A trading cycle is a pipeline. Make that literal.

```
core/contracts.py        pure types, no I/O          CycleContext, PairSnapshot, Signal,
                                                     EntryIntent, SizedOrder, RiskVerdict
core/reasons.py          one reason-code registry    no ad-hoc block strings

runtime/pipeline/
  marketdata.py          ticks + bars   -> MarketSnapshot
  features.py            feature tail   -> scoring frames
  scoring.py             models         -> Signal per pair
  belief.py              cross-pair     -> BeliefOverlay
  lifecycle.py           open positions -> management intents
  ranking.py             utility        -> enter vs abstain, ranked
  sizing.py              capital        -> lots            (full risk/sizing.py)
  risk.py                risk kernel    -> RiskVerdict
  submission.py          final admission + egress

runtime/runner.py        bootstrap + loop only, composing the above
```

Supporting extractions out of `runner.py`, by cohesion:

| New module | Absorbs | ~lines |
|---|---|---|
| `runtime/orchestration_capture.py` | `_orchestration_*`, `_capture_orchestration_cycle`, `_governed_command_payload_for_mode` | 1,600 |
| `runtime/models_loader.py` | `_PolicyModelRouter`, `_load_model_sets`, `_activation_consistency` | 1,500 |
| `runtime/managed_state.py` | `_serialize/_restore_managed_position_state`, `_hydrate_*`, `_reconcile_*` | 1,100 |
| `runtime/authority.py` | `_arm_production_runtime_authority`, `_synchronize_release_authority` | 540 |
| `runtime/telemetry/feature_serving.py` | `_feature_service_*`, `_pair_readiness_summary` | 320 |
| `runtime/diagnostics/summaries.py` | `_risk_cycle_summary`, `_adaptive_overlay_summary`, `_rollout_policy_summary` | 340 |

## Quant reevaluation — "only complementing strategies"

The four playbooks form a clean 2x2 over (trend vs range) x (continuation vs reversal):

| Playbook | Fires on | Volatility state |
|---|---|---|
| `trend_pullback` | `+trend_persistence`, `+pullback_quality` | normal |
| `breakout_expansion` | `+expansion` after `+precompression` | rising |
| `range_mean_reversion` | `-trend_persistence`, `+depth` | low (`+(1-vol_term)`) |
| `failed_breakout_reversal` | `+extension_penalty`, `+failure_confirmation` | high, post-impulse |

The taxonomy is complementary **on paper**. Two problems:

1. `range_mean_reversion` and `failed_breakout_reversal` are both fades and share
   `trigger_flip_score` and a `(1 - trend_persistence)` loading. They are separated
   only by volatility state — which nothing enforces or measures.
2. Empirically only mean-reversion showed timing skill on real EURUSD, and cost
   removed it (PBO 0.433, DSR 0.147). Three of four sleeves have no demonstrated edge.

So complementarity is currently an **assumption**. This refactor makes it a **check**:

- `strategy/complementarity.py` measures realized pairwise correlation between sleeve
  signal series and refuses to co-admit sleeves above a threshold. Redundant sleeves
  are demoted, not silently stacked.
- Sleeve admission is gated on a statistical warrant (`validation/activation_gate.py`),
  so a sleeve with no evidence cannot size up next to one that has it.

Removed rather than kept:

- The **aggressive-fallback rescue path**. It is a second decision rule layered on the
  utility comparison that already decides enter-vs-abstain. Verified dead on its own
  regression inputs: every case it used to "rescue" is now either admitted on the
  primary path or correctly refused by the evidence margin.

## Entry admission — conjunctive, with cost as a gate

Replaced the compensatory weighted sum (2026-07-31). The old rule averaged
channels, so a 0.95 setup could carry models at 0.18, and the free terms
(execution quality, crowding, reliability) donated their full 0.16 to ENTER
whenever nothing was *wrong* — a zero-information signal scored 0.58 and traded.

| Gate | Rule | Reason code |
|---|---|---|
| model | `reliable_model >= 0.52` | `model_conviction_below_floor` |
| setup | `reliable_setup >= 0.52` | `setup_quality_below_floor` |
| cost | `edge >= max(min_edge, 2.0 x spread)` | `edge_below_cost_floor` |
| margin | informative mean − 0.5 >= 0.02 | `insufficient_evidence_margin` |

All four are conjunctive — excellence in one buys nothing in another — and all
bind in `_ADAPTIVE_HARD_ENTRY_BLOCK_REASONS`, so the strict path cannot rescue
them. The three informative channels are now **equally weighted**; the hand-set
0.30/0.16/0.22/0.16 were never fitted to anything. Execution quality and
crowding became a penalty that only subtracts.

Cost as a gate is the load-bearing change: the old `edge_support` term saturated
at ~0.88 for any edge exceeding spread, so it was a constant. The gate
self-tightens exactly when it should — spreads widen in thin liquidity, so the
bar rises automatically at the times you least want to pay it.

## Money and risk management

`risk/sizing.py` already implements the correct spine. Wire all of it:

| Function | Wired today | After |
|---|---|---|
| `account_value_per_price_unit` | yes | yes |
| `drawdown_scaled_fraction` | yes | yes |
| `kelly_fraction` | **no** | conviction multiplier, 0.25x-1.0x |
| `volatility_targeted_fraction` | **no** | stable realized risk across vol regimes |
| `ewma_volatility` | **no** | feeds vol targeting |
| `risk_based_order_builder` | **no** | one owner for stop -> lots |

Kelly was wired as a *ceiling* first, and that made it inert: capped at the 0.5%
base, quarter-Kelly exceeds the cap for any edge better than roughly p=0.51 at
1:1, so a 0.53 setup and a 0.75 setup were funded identically. It is now a ratio
against a reference trade (p=0.55 at 1.5:1, quarter-Kelly = 0.0625), which
restores the gradient across the range that actually occurs. The upper bound
stays at 1.0x until the probabilities are calibrated out-of-sample —
`FXSTACK_MAX_CONVICTION_SIZE_SCALE` releases it.

A fifth multiplier now sits alongside: **realized sleeve expectancy**
(`sleeve_expectancy_allocation_scale`), ramping 0.25x-1.0x on USD per closed
trade and inert below 8 trades. This is the piece that lets *outcomes* govern
capital rather than the model's opinion of its own setups.

Composition rule: the multipliers are orthogonal by construction —
Kelly reads *edge*, vol targeting reads *instrument*, drawdown scaling reads
*account*. They multiply without double-counting, and the product is floored and
capped by the risk kernel, which stays the final authority.

## Sequencing

Each step ends with the full suite green before the next begins.

| # | Step | State |
|---|---|---|
| 1 | Green baseline | **done** — 18 fixed, 0 failing |
| 2 | Extract `runtime/managed_state.py` | **done** — 1,112 lines |
| 3 | Extract `runtime/orchestration_bridge.py` | **done** — 1,590 lines |
| 4 | Money-management wiring (Kelly + vol targeting) | **done** |
| 5 | Complementarity gate, binding on entries | **done** |
| 6 | Dead aggressive-fallback parameter removed | **done** |
| 7 | Nav-graph + `system-map.yaml` update | **done** |
| 8 | Extract `runtime/models_loader.py` (~1,500) | next |
| 9 | Extract `runtime/authority.py` (~540) | next |
| 10 | `core/contracts.py` + `core/reasons.py` | next |
| 11 | `api/app.py` (4,605) and `postgres_store.py` (4,047) | next |

`runner.py`: 14,070 -> 11,494 lines.

### Extraction recipe that worked

1. Pick a contiguous block that is cohesive on **one** axis.
2. Static-check the block for names it uses but does not define (walk the AST,
   diff `Load` names against `Store`/`arg`/`import`/`def` names). This catches
   almost everything in one pass.
3. Two traps that check does **not** catch, both hit during this work:
   - A module-level singleton mutated via `global` inside the block reads as
     "defined" and gets left behind in `runner.py`.
   - Renaming a private import to a public name can collide with a *local
     variable* of that name inside a function (`position_signature`). Alias the
     import instead.
4. Re-import into `runner.py` under the original underscored aliases with
   `# noqa: E402`, so the ~200 internal call sites and every test that reached
   into `runner` keep working untouched.
5. Full suite green before the next extraction.

## Invariants that must not move

- The runner stays the single production decision engine; research stays advisory.
- Fail-closed stays fail-closed: artifact contracts, activation gates, egress fencing.
- `submit_approved_command` remains the only path to broker egress.
- No behaviour change is bundled into a mechanical move. Moves and semantic changes
  land as separate steps so a regression has one obvious cause.
