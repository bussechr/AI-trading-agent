# Agent Orchestration Risk Register

## Phase 0 Minimum Risks

### Hot-path latency
- Risk: orchestration or remote dependencies slow the live decision cadence.
- Control: Phase 0 keeps `FXSTACK_AGENT_MODE=off` and forbids remote tools in the live loop.

### Snapshot compatibility
- Risk: `/v2/decision-snapshots` breaks runtime-audit or offline-export consumers.
- Control: additive-only contract rule plus compatibility tests.

### Duplicate-command risk
- Risk: future orchestration retries create duplicate broker commands.
- Control: governor-only trade authority and existing bridge command queue idempotency remain unchanged in Phase 0.

### External-tool risk
- Risk: model-controlled tools read or mutate unsafe resources.
- Control: implemented remote-LLM and external-tool flags default to off; the removed MCP/OpenClaw operator plane has no launcher or runtime setting.

### Operator-plane trust boundary
- Risk: operator workflows bypass runtime safety rules.
- Control: bridge remains the only execution boundary, the repository-hosted operator plane is absent, and the kill-switch runbook restores baseline mode.
