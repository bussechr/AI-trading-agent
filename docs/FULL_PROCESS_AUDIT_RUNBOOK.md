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

Fast gate (`15m`, strict):

```bash
python -m src.trader.cli scenario shadow-run -- \
  --baseline-url http://127.0.0.1:58710 \
  --candidate-url http://127.0.0.1:58711 \
  --duration-secs 900 \
  --poll-secs 2 \
  --min-throughput-delta 1 \
  --max-timeout-rate 0.05 \
  --require-nonzero-entries \
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
  --min-throughput-delta 1 \
  --max-timeout-rate 0.01 \
  --require-nonzero-entries \
  --out-dir docs \
  --prefix canary_shadow_24h
```

## 3) Finalize GO/HOLD

```bash
python -m src.trader.cli audit finalize-build -- \
  --evidence-root docs/audit \
  --fast-gate-artifact docs/canary_shadow_fast15m_<timestamp>.json \
  --shadow-artifact docs/canary_shadow_24h_<timestamp>.json \
  --rollback-validated
```

## 4) Runtime Policy

- Runtime and bridge are v2-only (`TRADER_BRIDGE_IMPL=fxstack`, `TRADER_RUNTIME_IMPL=fxstack`).
- Rollback uses prior v2 artifacts/configuration, not legacy executables.
