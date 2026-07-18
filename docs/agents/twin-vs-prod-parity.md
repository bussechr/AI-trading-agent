# Twin Vs Prod Parity

## Primary Files
- [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py)
- [fxstack_digital_twin_backtest.py](../../tools/fxstack_digital_twin_backtest.py)
- [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)

## Upstream
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [runtime-loop.md](runtime-loop.md)

## Downstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)

## Shared Logic
- environment classification
- playbook routing
- location scoring
- trigger scoring
- adaptive entry quality
- bounded causal cross-sectional normalization and pair-strength scaling
- re-entry cooldown
- tempo-gap logic
- replacement keep score
- adaptive lifecycle decision
- the same `hierarchical_v2` batch/latest multi-timeframe join, availability/freshness diagnostics, and stale-context rejection

## Twin-Only Surface
- full portfolio replay
- artifact emission and guardrail reports
- same-window strict baseline comparisons
- validation against `/v2/decision-snapshots`
- mandatory delayed execution: a decision built from closed bar `t` fills no earlier than `t + FXSTACK_TWIN_FILL_DELAY_BARS` (default one bar), and replay artifacts record both timelines

## Prod-Only Surface
- live bridge freshness gates
- broker command submission and ACK handling
- runtime state patching for dashboard and ops
- MT4 tick / bar refresh and heartbeat integration

## Validation Path
- run repeatable windows through `tools/run_causal_walk_forward.py --window NAME,TRAIN_END,TEST_START,TEST_END`; each window owns isolated training data, artifacts, registry, research manifest, replay raw data, and audit output
- physically truncate training raw/features by bar-close knowledge time and labels by outcome-horizon knowledge time; a row timestamp alone is not proof that its target was known
- train with the snapshot's raw root and ingestion disabled, then build a `research_only` manifest without updating the runtime model store
- physically truncate the replay raw tree at `TEST_END`; a date filter over the full project raw tree is not sufficient isolation
- use strict twin for replay baseline
- retain at least `adaptive_shadow_history_bars` common observations before a requested adaptive replay start, including across market closures
- compare adaptive twin against strict twin for quality and aggressiveness
- compare prod adaptive diagnostics against twin on overlapping windows
- compare `<tf>_available`, `<tf>_fresh`, and `<tf>_age_secs` for M15/H1/H4/D on the common latest row; neither path may score a stale context row
- require `causal_replay.enabled=true`, `future_data_access=forbidden`, and `fill_delay_bars>=1` in every promotion replay; the clock must satisfy decision bar open < decision availability at bar close < delayed execution fill for every emitted decision
- require each window's `point_in_time_audit.json` and the run-level `causal_walk_forward_summary.json` to pass before interpreting trade or PnL results

## Handshakes
- shared import boundary: [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py) -> [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- live validation source: [app.py](../../fx-quant-stack/src/fxstack/api/app.py) `/v2/decision-snapshots`
- promotion yardstick: twin strict/adaptive comparison artifacts from [fxstack_digital_twin_backtest.py](../../tools/fxstack_digital_twin_backtest.py)

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/promotion_gate.md](../../fx-quant-stack/docs/promotion_gate.md)
