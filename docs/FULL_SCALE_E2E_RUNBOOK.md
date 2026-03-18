# Full-Scale E2E Validation Runbook (v2 Required)

This runbook executes a fail-fast validation from AI training to MT4 trade execution for the `fxstack` v2 path.

## Profile

- Venue: MT4 Demo
- Universe: 9 liquid pairs (`EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD`)
- Runtime target: `fxstack` only (legacy blocked)
- Compute mode: CPU validation (`FXSTACK_REQUIRE_CUDA=0`)
- Canary topology: dual MT4 terminals
  - Baseline: `http://127.0.0.1:58710`
  - Candidate: `http://127.0.0.1:58711`

## One-Command Execution (Windows)

```bat
ops\windows\40_full_scale_e2e_validation.bat 10000
```

Compatibility wrapper:

```bat
run_full_scale_e2e.bat 10000
```

## Phase Coverage

The orchestrator runs:

1. Stop all + preflight (v2 contract, CPU profile)
2. Python/node sync, Postgres start, DB migrate/verify
3. Dukascopy coverage gate (`45/45` files + minimum row thresholds)
4. Ingest/features/labels
5. Train + deep-stale + model activation
6. Backtest smoke + targeted pytest suites
7. Baseline stack startup + live stack check
8. Candidate stack startup + live stack check
9. 15m fast gate + 24h shadow gate
10. Full-process audit refresh + GO/HOLD finalization

## Data Gate

The data availability gate is enforced via:

```bash
python -m src.trader.cli audit dukascopy-gate -- \
  --source-root fx-quant-stack/data/dukascopy \
  --pairs EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD \
  --timeframes M1,M5,M15,H4,D \
  --file-pattern {pair}_{granularity}.csv \
  --min-rows-m1 20000 \
  --min-rows-m5 10000 \
  --min-rows-m15 4000 \
  --min-rows-h4 1000 \
  --min-rows-d 400
```

## Live Stack Check

Bridge/runtime lifecycle checks are enforced via:

```bash
python -m src.trader.cli audit live-stack-check -- \
  --base-url http://127.0.0.1:58710 \
  --timeout-secs 2100 \
  --min-observation-secs 1800 \
  --min-heartbeat-advances 20 \
  --require-ticks \
  --require-acked-command \
  --command CLOSE_ALL \
  --symbol EURUSD
```

Run for candidate URL `:58711` as well.

Fast and shadow gates run with rollback-on-fail enabled using `ops\windows\90_stop_all.bat` as default rollback command.

## Evidence

- E2E run artifacts: `docs/e2e/<timestamp>/`
- Gate artifacts: `docs/canary_shadow_fast15m_*.json`, `docs/canary_shadow_24h_*.json`
- Final audit: `docs/audit/<date>_full_process/`
- Final decision: `docs/audit/<date>_full_process/go_no_go.json`

## Failure Policy

On any phase failure:

1. `ops/windows/90_stop_all.bat`
2. Keep artifacts/logs for diagnosis
3. Fix root cause and rerun from the beginning
