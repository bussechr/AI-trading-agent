# External Shadow Dual-Run Runbook

This runbook observes a baseline and the exact candidate runtime on an external isolated validation host or VM. It must not be run against production services or production URLs.

The validation environment must have copied immutable inputs and its own database, endpoints, credentials, logs, model registry, and rollback control. It must have no production database, bridge/API key, broker credential, registry-write access, or writable production mount. Candidate broker emission stays disabled and passing evidence requires zero emitted entry commands.

## Command

Run the direct tool from the isolated checkout, replacing every placeholder:

```bash
python tools/shadow_dual_run.py \
  --baseline-url <ISOLATED_BASELINE_URL> \
  --candidate-url <ISOLATED_CANDIDATE_URL> \
  --duration-secs 900 \
  --poll-secs 2 \
  --min-throughput-delta 0 \
  --max-timeout-rate 0.05 \
  --pair <PAIR> \
  --model-manifest <ISOLATED_MODEL_MANIFEST> \
  --out-dir <ISOLATED_EVIDENCE_DIR> \
  --prefix canary_shadow_fast15m
```

Repeat with `--duration-secs 86400`, the 24-hour threshold, and a distinct prefix for binding shadow evidence. Do not use `--require-nonzero-entries`: the tool observes genuine runtime behavior and must not manufacture trades to satisfy a gate.

If rollback execution is enabled, supply a command whose process, files, database, and endpoints are wholly inside the isolated trust domain. Never point it at production `launch_all.bat`, `90_stop_all.bat`, MT4, or production listeners.

## Evidence requirements

The JSON and Markdown reports bind the observation window, baseline/candidate results, exact pair and active model identity, runtime/feature readiness, command-window summary, risk state, and gate decision. Promotion evidence must also prove:

- the candidate is the actual installed runtime under evaluation;
- manifest, database, and loaded-runtime identities agree;
- required exit and reversal lifecycle models are loaded;
- one continuous runtime boot covers the required window;
- broker emission is disabled and entry-command emissions are zero.

Exit code `0` means the configured gates passed, `2` means one or more gates failed, and `3` means a gate and the explicitly scoped rollback command both failed.

The production-host scripts `24_start_candidate_stack.bat`, `30_fast_gate_15m.bat`, `31_shadow_24h.bat`, and `40_full_scale_e2e_validation.bat` are deliberate nonzero quarantine stubs.
