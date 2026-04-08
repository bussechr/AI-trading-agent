# ADR-00X: Agent Orchestration Phase 0 Freeze

## Status
Accepted for Phase 0 implementation.

## Context
The active FX stack already has a clean execution seam:

- `fxstack/runtime/runner.py` owns the live loop.
- `fxstack/live/scorer.py` emits the live signal contract.
- `fxstack/live/policy.py` owns gating.
- The bridge under `/v2/*` owns state, snapshots, and command queue handshakes.
- The dashboard and twin consume decision artefacts from those contracts.

Phase 0 freezes the orchestration boundary without changing live trading behavior.

## Decision
Phase 0 adopts the following non-negotiables:

1. The governor is the sole trade authority.
   Only the governor may create a broker-facing command, even when later orchestration layers emit proposals or approvals.

2. The bridge is the only execution ingress and egress boundary.
   Runtime, dashboard, twin, and future orchestration code must continue to treat the bridge contract as the canonical execution seam.

3. `/v2/decision-snapshots` is additive-only.
   Existing fields consumed by the twin and dashboard must remain backward compatible.

4. The hot path stays deterministic and local.
   No remote LLM calls, MCP tool calls, or operator-plane orchestration may enter the live decision cadence through Phase 6B.

5. Every run must be replayable.
   Persisted decision context, additive snapshots, idempotent side effects, and stable version bundles are required for later orchestration phases.

6. Internal persistence is canonical and OpenTelemetry is export-only.
   OTEL may be used for correlation and observability, but repository-owned storage remains the audit source of truth.

7. External tools stay behind a strict trust boundary.
   MCP is read-only by default, OpenClaw remains operator-plane only, and no external tool receives venue authority.

8. Every persisted orchestration object carries a version bundle.
   At minimum: `schema_version`, `policy_version`, `model_bundle_version`, and `orchestrator_version`.

## Consequences
- Phase 0 may add docs, schemas, capture tooling, model freeze tooling, and inert settings.
- Phase 0 must not change the runtime loop, bridge command path, dashboard behavior, or twin parity semantics.
- Future phases may build on these contracts, but they must preserve the rules above unless a later ADR supersedes this one.

## References
- [Agent Docs Index](../agents/README.md)
- [Runtime Loop](../agents/runtime-loop.md)
- [Bridge And API Handshakes](../agents/bridge-and-api-handshakes.md)
- [Twin Vs Prod Parity](../agents/twin-vs-prod-parity.md)
- [Agent Trust Boundary](../security/agent-trust-boundary.md)
- [Orchestrator Kill Switch](../runbooks/orchestrator-kill-switch.md)
