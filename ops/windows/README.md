# Windows Launcher Stack

Production startup orchestration for the fxstack v2 runtime.

## Primary entrypoint

- `launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]` (repo root): full staged-safe startup with validated endpoint selection

When a default port is occupied or reserved by Windows, the launcher selects the next bindable loopback port and stores it in `logs/active_stack_env.bat`. Active endpoint state overrides installed defaults until `90_stop_all.bat` removes it. Explicit port arguments are strict. Bridge auth is enabled by default; an operator-supplied `FXSTACK_BRIDGE_API_KEY` is honored, otherwise `_env.bat` creates and reuses an ignored local key.

Use `launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]` to resolve and display the endpoint contract without starting any service.

For an isolated shadow audit, set `FXSTACK_SKIP_INSTALLED_ENV=1` together with the process-level shadow/SQLite/feature-push-off values. `_env.bat` will not read `installed_env.bat`, but it will still load active endpoints and fill any unset safe defaults.

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
- `24_start_feature_push_worker.bat`: instance-isolated Feast outbox worker
- `25_monitor_everything.bat`: auth-aware aggregate monitor using the active endpoint state
- `26_operator_plane.bat`: describe or attach explicitly enabled read-only stdio MCP services
- `30_fast_gate_15m.bat`: strict 15m gate
- `31_shadow_24h.bat`: 24h shadow gate
- `32_finalize_audit.bat`: finalize GO/HOLD audit outputs
- `40_full_scale_e2e_validation.bat`: full fail-fast E2E validation (training -> activation -> live -> gates -> finalization)
- `90_stop_all.bat`: stop repo-owned Windows workers and selected ports

Baseline runtime calls default to instance ID `baseline`; candidate startup uses `candidate`. Each instance has its own runtime/feature-worker PID and log files, command-line marker, and feature worker ID. Restarts use `find_owned_instance_processes.ps1` to select only matching repo-owned processes, so starting or restarting the candidate does not terminate the baseline. Unmarked pre-upgrade processes are eligible only for baseline cleanup. `90_stop_all.bat` still stops every repo-owned instance by design.

Data ingest defaults:

- `FXSTACK_DUKASCOPY_SOURCE_ROOT` (default: `fx-quant-stack/data/dukascopy`)
- `FXSTACK_DUKASCOPY_FILE_PATTERN` (default: `{pair}_{granularity}.csv`)

## Dashboard Contract

- `22_start_dashboard.bat` is the authoritative launcher for `%TRADER_DASHBOARD_URL%`.
- Production build preparation happens in `02_sync_node.bat`.
- `22_start_dashboard.bat` runs the production Next server on `%TRADER_DASHBOARD_HOST%:%TRADER_DASHBOARD_PORT%` against an existing `.next/BUILD_ID`.
- `pnpm dev` is not part of normal ops and should be used only for developer preview on `http://127.0.0.1:3001`.

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
- validated baseline + candidate endpoint balanced gate flow
