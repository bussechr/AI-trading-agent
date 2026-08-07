# Full Process Audit Runbook

This flow bootstraps static evidence locally, imports externally produced runtime evidence through the operator quarantine workflow, and produces a GO/HOLD decision. It does not start a production candidate.

## 1. Bootstrap audit evidence

```bash
python tools/full_process_audit.py \
  --evidence-root docs/audit
```

The dated evidence directory contains read-only repository/toolchain metadata, static-check results, blockers, gate summary, GO/HOLD state, and operator checklists. It does not read a runtime database or contact a bridge. Environment metadata records matching variable names plus presence/nonempty state only; it never copies environment values or credentials. The generated shadow commands are templates for an external isolated validation host, not production commands.

## 2. Produce external runtime evidence

Follow [External Shadow Dual-Run](SHADOW_DUAL_RUN_RUNBOOK.md). Use the exact pair and isolated active-model manifest for both distinct observation windows:

- fast gate: at least 900 seconds;
- binding shadow gate: at least 86,400 seconds under one continuous boot.

The production-host candidate and validation launchers are nonzero quarantine stubs. The external environment must not receive production database, endpoint, credential, broker, registry-write, rollback, or writable-mount authority.

## 3. Produce rollback evidence

Run `python tools/run_release_rollback_drill.py --help` inside the isolated environment and supply the exact release, controller, process, port, database, snapshot, manifest, and command identities. Do not substitute an incomplete example or a production stop command.

## 4. Import and finalize GO/HOLD

After the signed, content-addressed evidence bundle has passed the explicit quarantine import workflow:

```bash
python tools/finalize_build.py \
  --evidence-root docs/audit \
  --fast-gate-artifact <IMPORTED_FAST_GATE_JSON> \
  --shadow-artifact <IMPORTED_24H_SHADOW_JSON> \
  --rollback-evidence <IMPORTED_ROLLBACK_EVIDENCE_JSON> \
  --pair <PAIR> \
  --model-manifest <QUARANTINED_ACTIVE_MODEL_MANIFEST>
```

GO requires zero open critical/high blockers plus exact-identity, distinct, valid fast, 24-hour, and rollback artifacts. HOLD is the safe result whenever evidence is missing, mutable, stale, short, duplicated, mismatched, or failed.

## Runtime policy

- Runtime and bridge execution are `fxstack` only; `src.trader` is a repository-only isolated-research facade with no runtime or operator verbs.
- The production host runs one `baseline` stack only.
- Rollback selects a previously verified `fxstack` release; it does not revive legacy executables.
- Offline research is advisory and cannot grant runtime, broker, or activation authority.
