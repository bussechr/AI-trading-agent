# Operator Plane

## Role
The operator plane is the optional supervisory boundary added in Phase 5. It is separate from the live runtime, bridge command queue, and execution provider stack.

## Active Paths
- `services/operator_plane/openclaw`
- `services/operator_plane/mcp_runtime_state`
- `services/operator_plane/mcp_twin_artefacts`
- `services/operator_plane/mcp_release_registry`

## Responsibilities
- replay orchestration windows through the existing tooling
- inspect runtime state and orchestration traces through read-only seams
- inspect replay artefacts and release-manifest state
- draft experiment and approval packs in a staging workspace

## Hard Boundaries
- no `/v2/commands`
- no command queue writes
- no broker credentials
- no execution-provider imports
- no live runtime startup dependency

## Primary Inputs
- `/v2/ready`
- `/v2/state`
- `/v2/decision-snapshots`
- `/v2/orchestration/runs`
- `/v2/orchestration/traces`
- `artifacts/orchestration/`
- `fx-quant-stack/artifacts/active_models.json`
- `fx-quant-stack/artifacts/registry`
- `fx-quant-stack/artifacts/releases`

## Related Docs
- [system-map.yaml](system-map.yaml)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [../security/agent-trust-boundary.md](../security/agent-trust-boundary.md)
