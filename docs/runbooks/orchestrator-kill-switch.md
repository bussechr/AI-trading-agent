# Orchestrator Kill Switch

## Purpose
This runbook restores the baseline runtime path by disabling all future orchestration and external-tool surfaces.

## Disable Switches
Set or confirm the following values before restarting the stack:

```bash
FXSTACK_AGENT_MODE=off
FXSTACK_AGENT_ALLOW_REMOTE_LLM=false
FXSTACK_AGENT_ALLOW_EXTERNAL_TOOLS=false
FXSTACK_MCP_ENABLED=false
FXSTACK_OPENCLAW_ENABLED=false
```

## Owner And On-Call Procedure
1. Runtime owner or incident commander declares orchestration disable.
2. Apply the disable switches in the active environment file or process environment.
3. Restart the runtime and any bridge-facing process that reads settings at startup.
4. Verify the baseline path with the checks below.
5. Record the incident and rollback reason in the operational log.

## Rollback Target
The rollback target is the current baseline runtime:

- `fxstack/runtime/runner.py`
- `fxstack/live/scorer.py`
- `fxstack/live/policy.py`
- `/v2/*` bridge and API routes
- existing command queue and risk kernel path

Phase 0 does not replace any of those components.

## Post-Kill Validation
After disabling orchestration features, confirm:

### Bridge
- `/v2/health` returns `status=ok`
- `/v2/ready` remains reachable
- command queue polling and ACK still work

### Runtime
- runtime enters its normal startup and main-loop phases
- no orchestration-only setting is required for boot
- decision snapshots continue to write

### Dashboard
- `/api/trading/state` continues to normalize bridge state
- no new required field is missing

## Evidence To Record
- timestamp of disable
- operator or on-call owner
- environment diff
- validation results for `/v2/health`, `/v2/ready`, `/v2/state`, and `/v2/decision-snapshots`

## Sign-Off
- Runtime owner: __________________
- Security owner: __________________
- Date: __________________
