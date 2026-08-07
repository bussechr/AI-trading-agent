# External Full-Scale Validation Runbook

Full-scale candidate validation does not run on the production host. The production host owns one baseline stack only, and `ops/windows/40_full_scale_e2e_validation.bat` is a deliberate nonzero quarantine stub. It starts nothing.

Use an external isolated build/research host or VM with immutable copied inputs and no production database, API key, bridge, MT4, broker credential, registry-write access, or writable production mount. Transfer only signed, content-addressed evidence into the production quarantine root through an explicit operator workflow.

## Validation phases

1. Record source, dependency, configuration, data, and active-model identities.
2. Run static checks and the data-coverage gate.
3. Build features, labels, and candidate artifacts in isolated roots.
4. Run causal offline evaluation with point-in-time inputs and delayed fills.
5. Install the exact candidate package in the validation environment.
6. Start isolated baseline and candidate runtimes with broker emission disabled.
7. Run the live-stack readiness check without issuing trade mutations.
8. Observe a 15-minute fast gate and a continuous 24-hour shadow gate.
9. Execute an isolated, identity-bound rollback drill.
10. Import the signed evidence bundle and finalize GO/HOLD without starting a candidate on production.

## Direct tools

Validate source coverage without the legacy CLI facade:

```bash
python tools/dukascopy_coverage_gate.py \
  --source-root <ISOLATED_DUKASCOPY_ROOT> \
  --pairs EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD \
  --timeframes M1,M5,M15,H4,D \
  --out <ISOLATED_EVIDENCE_DIR>/dukascopy_coverage.json
```

For runtime readiness, use `python tools/live_stack_check.py --help` and target only the isolated endpoints. Its optional ACK lifecycle probe is locked to non-trading `INFO`; BUY, SELL, CLOSE, and other execution commands are rejected at both parser and runtime boundaries. Do not request an ACKed mutation merely to make validation pass. Use [External Shadow Dual-Run](SHADOW_DUAL_RUN_RUNBOOK.md) for the exact candidate observation command and [Full Process Audit](FULL_PROCESS_AUDIT_RUNBOOK.md) for evidence bootstrap/finalization.

The external environment's rollback command must be scoped to that environment. It must never call the production `90_stop_all.bat`.

## Failure policy

On failure, stop only the isolated environment, preserve the evidence and logs, diagnose the root cause, rebuild a new immutable candidate, and restart the validation sequence. A partial or failed run grants no production authority.
