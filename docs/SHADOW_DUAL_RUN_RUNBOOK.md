# Shadow Dual-Run Runbook

## Goal
Run baseline and candidate runtimes side-by-side for a fixed window and evaluate canary gates:
- throughput (acked entry delta)
- reliability (ACK timeout rate)
- risk (no hard drawdown / daily breaker breach)
- operability (telemetry available)

## Command

```bash
python -m src.trader.cli scenario shadow-run -- \
  --baseline-url http://127.0.0.1:58710 \
  --candidate-url http://127.0.0.1:58711 \
  --duration-secs 900 \
  --poll-secs 2 \
  --min-throughput-delta 1 \
  --max-timeout-rate 0.05 \
  --require-nonzero-entries \
  --rollback-on-fail \
  --rollback-cmd "start /b run_bridge.bat" \
  --rollback-timeout-secs 60 \
  --out-dir docs \
  --prefix canary_shadow
```

Windows wrapper:

```bat
run_canary_shadow.bat http://127.0.0.1:58710 http://127.0.0.1:58711 900 docs
```

## Output
Two files are written under `docs/`:
- `canary_shadow_<timestamp>.json`
- `canary_shadow_<timestamp>.md`

The report includes:
- baseline vs candidate command outcomes
- governance/risk events in the window
- pass/fail gate decision
- rollback trigger reasons when failed
- rollback command execution result (if enabled)

## Rollback Trigger Conditions
- Throughput gate failed
- Reliability gate failed
- Risk gate failed
- Operability gate failed

## Notes
- Both runtimes must expose v2 endpoints.
- Use `run_bridge.bat` (or `trader bridge serve`) for candidate/cutover runs.
- Candidate should run with the same market feed as baseline.
- Keep MT4 execution path unchanged while evaluating protocol/runtime deltas.
- Exit code `0` = all gates pass.
- Exit code `2` = one or more gates failed.
- Exit code `3` = gate failed and rollback command execution failed.
