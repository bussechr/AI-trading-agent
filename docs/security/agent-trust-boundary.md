# Agent Trust Boundary

## Scope
This document defines the trust boundary for the orchestration and operator-plane stack while the live execution plane remains isolated.

## Hard Rules
- No external system may send broker-facing commands directly.
- The bridge remains the only execution ingress and egress boundary.
- The live loop must not depend on remote LLMs or external tool calls.
- Internal persistence remains canonical; telemetry exports are secondary.

## MCP Position
- The repository-hosted MCP operator plane was removed and has no runtime setting or launcher.
- Any future connector requires a new reviewed implementation outside the live trading hot path.
- No MCP connector receives venue authority, broker credentials, or write access to runtime state.

## OpenClaw Position
- The repository contains no OpenClaw service, launcher, or runtime setting.
- Any future integration belongs on a separate OS-user or host boundary and requires explicit security review.
- It may not originate venue commands.

## External Tool Policy
- `FXSTACK_AGENT_ALLOW_REMOTE_LLM=false`
- `FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS=false`

Those defaults keep the implemented external-tool paths inert; absent integrations are not represented by no-op environment flags.

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
