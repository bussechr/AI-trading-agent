# Active Runbooks

The public runtime and bridge live under `fxstack`. Root `src.trader` commands are legacy compatibility shims and are not operator entrypoints.

## Windows production operations

Use the repository-root launchers:

```bat
ops\windows\00_preflight.bat
launch_all.bat endpoints
launch_all.bat live 10000
launch_all.bat status
launch_all.bat stop
```

`launch_all.bat live` still requires valid signed release authority plus explicit live posture, arming, scopes, account-mode expectation, and broker attestation before any live entry path can exist. The production host owns one baseline stack only.

See [Ops Entrypoints](../../docs/agents/ops-entrypoints.md) for process ownership, endpoint persistence, training/activation isolation, and shutdown behavior.

## Training and activation

Candidate training and activation are external pre-deployment operations. Set distinct candidate artifact, registry, and activation roots before using:

```bat
ops\windows\13_train_all.bat
ops\windows\14_activate_models.bat
```

Do not train or activate into production-owned roots. The exact environment contract is documented in [Ops Entrypoints](../../docs/agents/ops-entrypoints.md#isolated-training-and-activation).

## Full process audit

Bootstrap static evidence directly:

```bash
python tools/full_process_audit.py \
  --evidence-root docs/audit \
  --runtime-db data/state/runtime_v2.db \
  --audit-dir data/state/audit
```

Runtime assurance comes from the exact installed candidate on an external isolated host or VM. Follow the [Full Process Audit Runbook](../../docs/FULL_PROCESS_AUDIT_RUNBOOK.md), [External Shadow Dual-Run](../../docs/SHADOW_DUAL_RUN_RUNBOOK.md), and [Causal Research and Runtime Validation](../../docs/agents/causal-research-and-runtime-validation.md).

## Data coverage and causal research

Use `tools/dukascopy_coverage_gate.py` for source coverage and `tools/run_causal_walk_forward.py` for physically isolated point-in-time research. Research inputs and outputs must stay outside the production trust boundary and cannot grant runtime, activation, broker, or release authority.

## Bridge/MT4 stale triage

1. Run `launch_all.bat status` and the authenticated monitor (`ops\windows\23_start_monitor.bat --run`).
2. Confirm the resolved endpoint with `launch_all.bat endpoints`; do not assume the preferred port.
3. Inspect MT4 **Experts** and **Journal** for BridgeEA authentication, WebRequest, or DLL errors.
4. Confirm the BridgeEA is attached to the intended chart and the visible account/server/Magic identity matches operator intent.
5. Restart through `launch_all.bat stop` followed by `launch_all.bat live <EQUITY>` only after current signed authority is valid. Shutdown intentionally leaves MT4 visible.

If heartbeat or ticks exceed their freshness SLA, the dashboard must remain stale/disconnected rather than presenting cached state as live.

## One-time runtime state remediation

Preview before applying:

```bash
python fx-quant-stack/scripts/remediate_state_snapshot.py
python fx-quant-stack/scripts/remediate_state_snapshot.py --apply
```

Optional decision-cache cleanup:

```bash
python fx-quant-stack/scripts/remediate_state_snapshot.py --apply --clear-decisions
```
