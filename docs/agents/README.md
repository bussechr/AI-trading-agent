# Agent Docs Index

## Primary Files
- [system-map.yaml](system-map.yaml)
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [operator-plane.md](operator-plane.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Upstream
- [../../AGENTS.md](../../AGENTS.md)
- [../../.codex/skills/navigate-trading-stack/SKILL.md](../../.codex/skills/navigate-trading-stack/SKILL.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [operator-plane.md](operator-plane.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Subsystems
- Runtime loop: [runtime-loop.md](runtime-loop.md)
- Bridge and API: [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- Dashboard dataflow: [dashboard-dataflow.md](dashboard-dataflow.md)
- Model and feature stack: [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- Providers: [system-map.yaml](system-map.yaml)
- Portfolio intelligence: [system-map.yaml](system-map.yaml)
- Strategy allocator and campaigns: [system-map.yaml](system-map.yaml)
- Belief engine (directional-belief v2): [system-map.yaml](system-map.yaml)
- Self-improvement loop (LLM proposes, code disposes): [system-map.yaml](system-map.yaml)
- Offline security and egress: [system-map.yaml](system-map.yaml)
- Twin vs prod parity: [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- Operator plane: [operator-plane.md](operator-plane.md)
- Ops entrypoints: [ops-entrypoints.md](ops-entrypoints.md)
- Registry: [system-map.yaml](system-map.yaml)

## Search Markers
- `AGENT: ROLE`
- `AGENT FLOW`
- `AGENT HANDSHAKE`
- `AGENT STATE`
- `AGENT PARITY`
- `AGENT HOT PATH`

## Pointer Table
- Need live startup or main loop: [runtime-loop.md](runtime-loop.md) -> [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- Need bridge contract or command ACK path: [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md) -> [app.py](../../fx-quant-stack/src/fxstack/api/app.py)
- Need dashboard state shape: [dashboard-dataflow.md](dashboard-dataflow.md) -> [route.ts](../../app/api/trading/state/route.ts)
- Need feature or gate inputs: [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md) -> [scorer.py](../../fx-quant-stack/src/fxstack/live/scorer.py)
- Need raw snapshot, cache, session, or artifact integrity: [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md) -> [parquet_store.py](../../fx-quant-stack/src/fxstack/io/parquet_store.py), [multi_tf_contract.py](../../fx-quant-stack/src/fxstack/features/multi_tf_contract.py), [session_contract.py](../../fx-quant-stack/src/fxstack/features/session_contract.py), and [artifact_contract.py](../../fx-quant-stack/src/fxstack/models/artifact_contract.py)
- Need registry identity or portfolio-RL checkpoint integrity: [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md) -> [activation.py](../../fx-quant-stack/src/fxstack/training/activation.py), [trainer.py](../../fx-quant-stack/src/fxstack/rl/trainer.py), [proposal.py](../../fx-quant-stack/src/fxstack/rl/proposal.py), and [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- Need provider normalization, source roles, or execution adapter seams: [system-map.yaml](system-map.yaml) -> [providers](../../fx-quant-stack/src/fxstack/providers), especially [registry.py](../../fx-quant-stack/src/fxstack/providers/registry.py), [history/](../../fx-quant-stack/src/fxstack/providers/history), [market/](../../fx-quant-stack/src/fxstack/providers/market), [execution/](../../fx-quant-stack/src/fxstack/providers/execution), and the runtime-facing dispatch helpers in [live_quotes.py](../../fx-quant-stack/src/fxstack/data/live_quotes.py)
- Need portfolio exposure, budgeting, or concentration seams: [system-map.yaml](system-map.yaml) -> [portfolio](../../fx-quant-stack/src/fxstack/portfolio), especially [book.py](../../fx-quant-stack/src/fxstack/portfolio/book.py), [allocator.py](../../fx-quant-stack/src/fxstack/portfolio/allocator.py), [correlation.py](../../fx-quant-stack/src/fxstack/portfolio/correlation.py), and [stress.py](../../fx-quant-stack/src/fxstack/portfolio/stress.py)
- Need adaptive parity: [twin-vs-prod-parity.md](twin-vs-prod-parity.md) -> [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py)
- Need blind walk-forward replay: [twin-vs-prod-parity.md](twin-vs-prod-parity.md) -> [run_causal_walk_forward.py](../../tools/run_causal_walk_forward.py) -> [build_walk_forward_snapshot.py](../../tools/build_walk_forward_snapshot.py) -> [fxstack_digital_twin_backtest.py](../../tools/fxstack_digital_twin_backtest.py)
- Need operator-plane boundaries or supervisory flows: [operator-plane.md](operator-plane.md) -> [services/operator_plane](../../services/operator_plane)
- Need start/stop order: [ops-entrypoints.md](ops-entrypoints.md) -> [21_start_runtime.bat](../../ops/windows/21_start_runtime.bat)
- Need isolated retraining or candidate activation: [ops-entrypoints.md](ops-entrypoints.md#isolated-training-and-activation) -> [13_train_all.bat](../../ops/windows/13_train_all.bat) -> [14_activate_models.bat](../../ops/windows/14_activate_models.bat)
- Need an end-to-end smoke: [ops-entrypoints.md](ops-entrypoints.md) -> [launch_all.bat](../../launch_all.bat), using isolated shadow settings and repository-owned process selectors

## Conventions
- File headers declare ownership, callers, side effects, and next docs.
- Inline comments only mark boundaries, handshakes, parity seams, and hot paths.
- `system-map.yaml` is the authoritative machine-readable map.
- Use the smallest regression that proves a changed contract; reserve broad suites for explicit requests or irreducible boundary risk.
- Provider normalization sits beneath parquet, Feast, runtime, and API consumers; portfolio allocation sits between policy output and the risk kernel.

## Handshakes
- navigation entrypoint: [../../AGENTS.md](../../AGENTS.md) -> [system-map.yaml](system-map.yaml)
- runtime/API handshake details live in [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- dashboard contract handshake details live in [dashboard-dataflow.md](dashboard-dataflow.md)

## Related Docs
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
- [../STRATEGY_DECISION_DAG.md](../STRATEGY_DECISION_DAG.md)
- [../PY_MT4_EFFICIENCY_DAG.md](../PY_MT4_EFFICIENCY_DAG.md)
- [../FULL_SCALE_E2E_RUNBOOK.md](../FULL_SCALE_E2E_RUNBOOK.md)
