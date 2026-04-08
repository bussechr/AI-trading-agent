# Agent Trust Boundary

## Scope
This document defines the trust boundary for the orchestration and operator-plane stack while the live execution plane remains isolated.

## Hard Rules
- No external system may send broker-facing commands directly.
- The bridge remains the only execution ingress and egress boundary.
- The live loop must not depend on remote LLMs or external tool calls.
- Internal persistence remains canonical; telemetry exports are secondary.

## MCP Position
- MCP is reserved for later phases as a read-only connector layer.
- Allowed early uses are state inspection, release artefact lookup, and operator-facing context collection.
- MCP tools are model-controlled, so they must remain outside the live trading hot path.
- No MCP connector receives venue authority, broker credentials, or write access to runtime state in Phase 5.
- The implemented operator-plane MCP services live under `services/operator_plane/` and remain `stdio` read-only servers in Phase 5.

## OpenClaw Position
- OpenClaw belongs in the operator plane only.
- Skills are treated as untrusted until explicitly reviewed.
- Sandboxing is mandatory when OpenClaw is enabled in later phases.
- OpenClaw may coordinate operator workflows, but it may not originate venue commands.
- The implemented OpenClaw supervisory bindings live under `services/operator_plane/openclaw` and are intended for a separate OS-user or host boundary.

## External Tool Policy
- `FXSTACK_AGENT_ALLOW_REMOTE_LLM=false`
- `FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS=false`
- `FXSTACK_MCP_ENABLED=false`
- `FXSTACK_OPENCLAW_ENABLED=false`

Those defaults remain locked in Phase 0 and keep all external-tool paths inert.

## Secrets And Data Handling
- Broker secrets stay inside the existing runtime and bridge boundary.
- External tools must never inherit broker or execution credentials.
- Model traces, prompts, and tool-call metadata are future audit artefacts and must not become the only source of truth for a decision.

## Operator Boundary
- Human operators may review, approve, or disable orchestration features.
- They may not bypass the governor or bridge command boundary.
- Emergency rollback must disable orchestration mode first, then validate the baseline runtime path.
- Operator services may read runtime state, replay artefacts, and release metadata, but they may not place, amend, or cancel trades through any path.

## Required Reviews
- Security review before any non-read-only external connector is enabled.
- Model risk review before any remote model enters a decision-support workflow.
- Explicit approval before any operator-plane agent can mutate repository-owned state.
