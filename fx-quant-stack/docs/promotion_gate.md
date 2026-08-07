# External Promotion Gate

Candidate promotion evidence is produced only by the exact installed runtime on an external isolated host or VM. The production host runs one baseline stack and its candidate/fast-gate/24-hour/full-validation scripts are deliberate nonzero quarantine stubs.

The isolated environment must have its own database, endpoints, credentials, registry, logs, model roots, and rollback control; broker emission remains disabled. It must not have production credentials, broker access, registry-write authority, or writable production mounts.

## Required evidence

1. Contract and identity parity: manifest, database, loaded runtime, package, pair, and model-set identities agree.
2. Operability: runtime and feature readiness remain healthy under one continuous boot.
3. Reliability and risk: command lifecycle, timeout, drawdown, breaker, and governance gates pass.
4. Broker boundary: zero emitted entry commands while broker emission is disabled.
5. Distinct windows: at least 900 seconds for the fast artifact and 86,400 seconds for binding shadow evidence.
6. Rollback: an independently identified rollback drill passes entirely inside the isolated trust domain.

Use the direct observer described in [External Shadow Dual-Run](../../docs/SHADOW_DUAL_RUN_RUNBOOK.md). Do not use `--require-nonzero-entries`; genuine zero-entry market windows are valid observations, and validation must never manufacture trades.

Import only a signed, content-addressed evidence bundle into the production quarantine root. Finalize with `tools/finalize_build.py`; missing, short, duplicated, mutable, or identity-mismatched evidence yields HOLD.
