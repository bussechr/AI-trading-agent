# Agent Docs Index

## Primary Files
- [system-map.yaml](system-map.yaml)
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Upstream
- [../../AGENTS.md](../../AGENTS.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Subsystems
- Runtime loop: [runtime-loop.md](runtime-loop.md)
- Bridge and API: [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- Dashboard dataflow: [dashboard-dataflow.md](dashboard-dataflow.md)
- Model and feature stack: [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- Twin vs prod parity: [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
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
- Need adaptive parity: [twin-vs-prod-parity.md](twin-vs-prod-parity.md) -> [adaptive_policy.py](../../fx-quant-stack/src/fxstack/backtest/adaptive_policy.py)
- Need start/stop order: [ops-entrypoints.md](ops-entrypoints.md) -> [21_start_runtime.bat](../../ops/windows/21_start_runtime.bat)

## Conventions
- File headers declare ownership, callers, side effects, and next docs.
- Inline comments only mark boundaries, handshakes, parity seams, and hot paths.
- `system-map.yaml` is the authoritative machine-readable map.

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
