# Agent Entry

Use the active system first. Ignore legacy paths unless the active docs point there.

## Start Here
- Index: [docs/agents/README.md](docs/agents/README.md)
- Machine map: [docs/agents/system-map.yaml](docs/agents/system-map.yaml)

## Common Tasks
- Runtime loop and live decision flow: [docs/agents/runtime-loop.md](docs/agents/runtime-loop.md)
- Bridge, API, and command handshakes: [docs/agents/bridge-and-api-handshakes.md](docs/agents/bridge-and-api-handshakes.md)
- Dashboard state normalization and consumers: [docs/agents/dashboard-dataflow.md](docs/agents/dashboard-dataflow.md)
- Model stack, features, and policy gates: [docs/agents/model-stack-and-feature-flow.md](docs/agents/model-stack-and-feature-flow.md)
- Twin vs prod adaptive parity: [docs/agents/twin-vs-prod-parity.md](docs/agents/twin-vs-prod-parity.md)
- Operator plane, MCP, and supervisory flows: [docs/agents/operator-plane.md](docs/agents/operator-plane.md)
- Windows ops entrypoints: [docs/agents/ops-entrypoints.md](docs/agents/ops-entrypoints.md)

## Active Paths First
- Backend: `fx-quant-stack/src/fxstack/runtime`, `live`, `api`, `backtest`
- Dashboard: `app/api/trading`, `components`, `lib/hooks`, `lib/server/bridge.ts`
- Ops: `ops/windows`
- Tooling: `tools/fxstack_digital_twin_backtest.py`, `tools/live_stack_check.py`, `tools/shadow_dual_run.py`

## Search Conventions
- `AGENT: ROLE` for ownership
- `AGENT FLOW` for orchestration boundaries
- `AGENT HANDSHAKE` for cross-system contracts
- `AGENT PARITY` for twin/prod overlap and divergence
- `AGENT HOT PATH` for latency-sensitive code
