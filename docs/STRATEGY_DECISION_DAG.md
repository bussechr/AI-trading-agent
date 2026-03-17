# Strategy Decision DAG And Cancellation Taxonomy

## Decision DAG
1. `act()` cycle starts.
2. Universe build + volatility snapshot.
3. Pull open positions.
4. `decisions()` per symbol:
- Fresh tick gate.
- `score_symbol()` component stack:
  - EL momentum ensemble.
  - Regime probability and adaptive beta.
  - Hawkes/OFI micro component.
  - LPPLS damping (buy-side).
  - Session multiplier.
  - Direction calibration.
  - AI indicator blend.
- Regime-aware thresholds.
- Score/sharpe/cost/spread/hawkes/heston/utility blockers.
- Confidence and execution-readiness scoring.
- Candidate ranking.
5. Split fresh-entry vs held-symbol decisions.
6. Exit manager:
- Risk-manager exits.
- Reversal exits.
- Command-budget + bridge close command.
7. Governance scaling/pause.
8. Entry execution:
- Execution quality gate.
- Sizing and leverage caps.
- Margin-floor + min-lot floor.
- Cost gate finalization.
- Portfolio/cluster risk requantization.
- Command-budget check.
- Send order + pending-entry mark.
9. Dashboard + diagnostics post.

## Cancellation Taxonomy
- `signal-level`: model components offset each other before gate evaluation.
- `gate-level`: soft blockers strongly damp score/confidence into non-action.
- `execution-level`: execution gate vetoes candidates already softly blocked on same axis.
- `risk-budget-level`: lot/risk floors and risk caps conflict causing late-stage rejection.
- `transport-level`: dual command budgets (agent + bridge) suppress valid intents.
- `replay-artifact`: static-bar replay misses live-only freshness fields, causing false starvation.

## Required Trace Fields
Each lifecycle row must include:
- `cycle_id`, `symbol`, `score_raw`, `score_effective`, `gate_penalty`
- `blockers`, `entry_ready`, `exec_quality_ready`, `execution_ready`
- `lot_pre_floor`, `lot_post_floor`, `rejection_reason`, `outcome`
