# Full Process Audit Runbook

This runbook defines the operator flow for the production cutover-ready audit profile.

## 1) Bootstrap Audit Evidence

```bash
python -m src.trader.cli audit full-process -- \
  --evidence-root docs/audit \
  --runtime-db data/state/runtime_v2.db \
  --audit-dir data/state/audit
```

Expected artifacts under `docs/audit/<date>_full_process/`:

- `metadata.json`
- `phase1_static_checks.json`
- `master_report.md`
- `blockers.json`
- `gate_summary.json`
- `go_no_go.json`

## 2) Run Live Assurance

Run this section only inside the external isolated validation host or VM. The production-host scripts `24_start_candidate_stack.bat`, `30_fast_gate_15m.bat`, `31_shadow_24h.bat`, and `40_full_scale_e2e_validation.bat` are quarantine stubs and intentionally return nonzero. The external environment must have no production database, bridge/API key, MT4/broker credential, registry-write access, or writable production mount. Its rollback command must be scoped to that external environment and must never call the production `90_stop_all.bat`.

Fast gate (`15m`, strict):

```bash
python -m src.trader.cli scenario shadow-run -- \
  --baseline-url http://127.0.0.1:58710 \
  --candidate-url http://127.0.0.1:58711 \
  --duration-secs 900 \
  --poll-secs 2 \
  --min-throughput-delta 0 \
  --max-timeout-rate 0.05 \
  --pair EURUSD \
  --model-manifest /isolated/candidate/active_models.json \
  --out-dir docs \
  --prefix canary_shadow_fast15m
```

Shadow window (`24h`):

```bash
python -m src.trader.cli scenario shadow-run -- \
  --baseline-url http://127.0.0.1:58710 \
  --candidate-url http://127.0.0.1:58711 \
  --duration-secs 86400 \
  --poll-secs 2 \
  --min-throughput-delta 0 \
  --max-timeout-rate 0.01 \
  --pair EURUSD \
  --model-manifest /isolated/candidate/active_models.json \
  --out-dir docs \
  --prefix canary_shadow_24h
```

## 3) Finalize GO/HOLD

Transfer the externally signed, content-addressed evidence bundle into the production quarantine root through the explicit operator import workflow. Never point finalization at mutable external paths or live candidate processes.

```bash
python -m src.trader.cli audit finalize-build -- \
  --evidence-root docs/audit \
  --fast-gate-artifact docs/canary_shadow_fast15m_<timestamp>.json \
  --shadow-artifact docs/canary_shadow_24h_<timestamp>.json \
  --rollback-evidence docs/rollback_drill_evidence_<timestamp>.json \
  --pair EURUSD \
  --model-manifest fx-quant-stack/artifacts/active_models.json
```

## 4) Runtime Policy

- Runtime and bridge are v2-only (`TRADER_BRIDGE_IMPL=fxstack`, `TRADER_RUNTIME_IMPL=fxstack`).
- Rollback uses prior v2 artifacts/configuration, not legacy executables.
- The production host runs one `baseline` stack only. Follow the [one-time retired-candidate cleanup](agents/ops-entrypoints.md#one-time-retired-candidate-cleanup) before the first post-migration restart.
