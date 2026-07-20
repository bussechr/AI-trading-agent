# Operator Plane

## Role
The operator plane is the optional supervisory boundary added in Phase 5. It is separate from the live runtime, bridge command queue, and execution provider stack.

## Active Paths
- `services/operator_plane/openclaw`
- `services/operator_plane/mcp_runtime_state`
- `services/operator_plane/mcp_release_registry`

## Responsibilities
- inspect runtime state and orchestration traces through read-only seams
- inspect release-manifest state
- draft experiment and approval packs in a staging workspace

## Hard Boundaries
- no `/v2/commands`
- no command queue writes
- no broker credentials
- no execution-provider imports
- no causal-research execution or research-root access
- no self-improvement or autonomous self-correction launcher
- no live runtime startup dependency
- disabled MCP servers reject resource, prompt, and tool requests and do not enter their stdio loop
- disabled OpenClaw construction/`--describe` does not create a state directory
- MCP transport is stdio only; there is no automatic/background operator-plane startup
- OpenClaw requires sandboxing and stays disabled in the Windows stack defaults

## Windows Entry

`ops\windows\26_operator_plane.bat describe` is a read-only capability check. The `runtime-mcp` and `release-mcp` actions run attached to stdio only after `FXSTACK_MCP_ENABLED=1` and `FXSTACK_MCP_TRANSPORT=stdio` are set explicitly. The launcher does not expose OpenClaw write flows or causal-research tooling.

## Primary Inputs
- `/v2/ready`
- `/v2/state`
- `/v2/decision-snapshots`
- `/v2/orchestration/runs`
- `/v2/orchestration/traces`
- `fx-quant-stack/artifacts/active_models.json`
- `fx-quant-stack/artifacts/registry`
- `fx-quant-stack/artifacts/releases`

## Related Docs
- [system-map.yaml](system-map.yaml)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../security/agent-trust-boundary.md](../security/agent-trust-boundary.md)
