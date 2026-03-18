# Windows Launcher Stack

Production startup orchestration for the fxstack v2 runtime.

## Primary entrypoint

- `start.bat` (repo root): full staged-safe startup

## Modular scripts

- `00_preflight.bat`: environment and dependency checks
- `01_sync_python.bat`: `uv` sync for `fx-quant-stack/.venv`
- `02_sync_node.bat`: `pnpm` install + build
- `03_postgres_start.bat`: postgres service start + readiness
- `04_db_migrate.bat`: alembic migrate + verify
- `05_gpu_check.bat`: CUDA requirement validation
- `10_ingest_all.bat`: Dukascopy CSV ingestion for all pairs/timeframes
- `11_features_all.bat`: feature build
- `12_labels_all.bat`: label build
- `13_train_all.bat`: model training per pair
- `14_activate_models.bat`: activate model sets in DB + manifest
- `15_backtest_smoke.bat`: quick cost-aware smoke checks
- `16_train_swing_transformer.bat`: force train swing transformer for all pairs
- `17_train_intraday_tcn.bat`: force train intraday TCN for all pairs
- `18_train_deep_stale.bat`: retrain deep artifacts only when stale
- `20_start_bridge.bat`: bridge startup/readiness
- `21_start_runtime.bat`: runtime startup/readiness
- `22_start_dashboard.bat`: dashboard startup/readiness
- `23_start_monitor.bat`: confidence monitor
- `24_start_candidate_stack.bat`: optional candidate stack startup
- `30_fast_gate_15m.bat`: strict 15m gate
- `31_shadow_24h.bat`: 24h shadow gate
- `32_finalize_audit.bat`: finalize GO/HOLD audit outputs
- `40_full_scale_e2e_validation.bat`: full fail-fast E2E validation (training -> activation -> live -> gates -> finalization)
- `90_stop_all.bat`: stop known windows and ports

Data ingest defaults:

- `FXSTACK_DUKASCOPY_SOURCE_ROOT` (default: `fx-quant-stack/data/dukascopy`)
- `FXSTACK_DUKASCOPY_FILE_PATTERN` (default: `{pair}_{granularity}.csv`)

## Full-Scale E2E Profile

Use:

```bat
ops\windows\40_full_scale_e2e_validation.bat 10000
```

The profile enforces:

- `TRADER_BRIDGE_IMPL=fxstack`
- `TRADER_RUNTIME_IMPL=fxstack`
- `FXSTACK_REQUIRE_CUDA=0`
- 9-pair liquid universe
- baseline (`:58710`) + candidate (`:58711`) balanced gate flow
