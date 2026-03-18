# Runbooks

## Full Process Audit

Bootstrap evidence and run static checks:

```bash
python -m src.trader.cli audit full-process -- \
  --evidence-root docs/audit \
  --runtime-db data/state/runtime_v2.db \
  --audit-dir data/state/audit
```

Finalize build signoff after fast-gate + 24h shadow artifacts are available:

```bash
python -m src.trader.cli audit finalize-build -- \
  --evidence-root docs/audit \
  --fast-gate-artifact docs/canary_shadow_fast15m_<timestamp>.json \
  --shadow-artifact docs/canary_shadow_24h_<timestamp>.json \
  --rollback-validated
```

## Baseline Training

1. Place CSV files under `fx-quant-stack/data/dukascopy/{PAIR}_{TIMEFRAME}.csv`.
2. Ingest Dukascopy CSV data.
3. Build features.
4. Build labels.
5. Train regime, swing, intraday, and meta models.
6. Calibrate probabilities.

Example ingest:

```bash
python -m src.trader.cli data ingest --pair EURUSD --granularity M5 --source-root fx-quant-stack/data/dukascopy
```

## One-time Provider Partition Migration

If legacy parquet data exists under `provider=oanda`, migrate to `provider=dukascopy`:

```bash
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/raw --apply
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/features --apply
python -m src.trader.cli data migrate-provider --store-root fx-quant-stack/data/labels --apply
```

Use `--dry-run` (default) to preview migration counts before writing.

## Baseline Freeze

Capture legacy baseline artifacts before cutover:

```bash
python scripts/freeze_baseline.py --runtime-db data/state/runtime_v2.db --out-dir docs
```

## Live Runtime

1. Start Postgres.
2. Run schema migration + table verification:

```bash
python -m src.trader.cli db migrate
python -m src.trader.cli db verify
```

3. Start FastAPI runtime.
4. Point MT4 EA to `/v2/*` API.
5. Monitor command lifecycle and governance events.

## Fast Promotion Gate

Evaluate candidate vs baseline runtime:

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

Exit code `0` means pass, `2` means fail, `3` means fail + rollback command failed.

## Evidence Outputs

The audit pipeline creates:

- `docs/audit/<date>_full_process/master_report.md`
- `docs/audit/<date>_full_process/blockers.json`
- `docs/audit/<date>_full_process/gate_summary.json`
- `docs/audit/<date>_full_process/go_no_go.json`
