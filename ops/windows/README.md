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
- `13_train_all.bat`: model training per pair; isolated runs set all `FXSTACK_TRAIN_{RAW,FEATURE,LABEL,ARTIFACT,REGISTRY}_ROOT` values and `FXSTACK_TRAIN_ALLOW_INGEST=0`; set `FXSTACK_FORCE_RETRAIN=1` after feature-contract changes, and `FXSTACK_TRAIN_WITH_BELIEF=0` when the global belief bundle is trained once via `trader train belief`
- `14_activate_models.bat`: activate model sets in DB + manifest
- `15_backtest_smoke.bat`: quick cost-aware smoke checks
- `16_train_swing_transformer.bat`: force train swing transformer for all pairs, honoring `FXSTACK_TRAIN_*` roots
- `17_train_intraday_tcn.bat`: force train intraday TCN for all pairs, honoring `FXSTACK_TRAIN_*` roots
- `18_train_deep_stale.bat`: retrain deep artifacts only when stale, honoring `FXSTACK_TRAIN_*` roots
- `20_start_bridge.bat`: bridge startup/readiness
- `21_start_runtime.bat`: runtime startup/readiness
- `22_start_dashboard.bat`: dashboard startup/readiness
- `23_start_monitor.bat`: confidence monitor
- `24_start_candidate_stack.bat`: nonzero quarantine stub; candidates run only on an external isolated host or VM
- `24_start_feature_push_worker.bat`: baseline-only Feast outbox worker
- `25_monitor_everything.bat`: auth-aware aggregate monitor using the active endpoint state
- `26_operator_plane.bat`: describe or attach explicitly enabled read-only stdio MCP services
- `30_fast_gate_15m.bat`: nonzero quarantine stub; the 15-minute gate runs externally
- `31_shadow_24h.bat`: nonzero quarantine stub; the 24-hour shadow gate runs externally
- `32_finalize_audit.bat`: finalize GO/HOLD audit outputs
- `40_full_scale_e2e_validation.bat`: nonzero quarantine stub; full validation runs externally
- `stop_owned_stack_processes.ps1`: kill only repo-owned or fresh PID-marker-bound worker trees and verify configured listeners are gone
- `90_stop_all.bat`: durably revoke execution egress and quarantine commands, then invoke verified repo-owned process-tree/listener shutdown; MT4 remains running

The production host admits only instance ID `baseline`. The batch launchers and installed runtime/feature-worker CLIs reject every other identity before database or process mutation. `find_owned_instance_processes.ps1` remains only so shutdown can recognize stale repo-owned processes from before this quarantine; it does not authorize candidate coexistence.

Data ingest defaults:

- `FXSTACK_DUKASCOPY_SOURCE_ROOT` (default: `fx-quant-stack/data/dukascopy`)
- `FXSTACK_DUKASCOPY_FILE_PATTERN` (default: `{pair}_{granularity}.csv`)

## Dashboard Contract

- `22_start_dashboard.bat` is the authoritative launcher for `%TRADER_DASHBOARD_URL%`.
- Production build preparation happens in `02_sync_node.bat`.
- `22_start_dashboard.bat` runs the production Next server on `%TRADER_DASHBOARD_HOST%:%TRADER_DASHBOARD_PORT%` against an existing `.next/BUILD_ID`.
- `pnpm dev` is not part of normal ops and should be used only for developer preview on `http://127.0.0.1:3001`.

## External Full-Scale E2E Profile

`40_full_scale_e2e_validation.bat` intentionally returns nonzero on the production host. Run training and exact-candidate validation on a separate host or VM with no production database, API key, bridge, MT4/broker access, registry-write access, or writable production mounts. Import only externally signed, content-addressed evidence through the release quarantine workflow.

Before first production restart after removing a former same-host candidate, follow the one-time retired-candidate cleanup in [the ops entrypoint runbook](../../docs/agents/ops-entrypoints.md#one-time-retired-candidate-cleanup). It covers exact terminal/account/Magic identification, candidate EA removal, bridge-key rotation, stale candidate artifact cleanup, and why arbitrary `terminal.exe` processes must never be killed.

The external validation environment must enforce:

- `TRADER_BRIDGE_IMPL=fxstack`
- `TRADER_RUNTIME_IMPL=fxstack`
- `FXSTACK_REQUIRE_CUDA=0`
- 9-pair liquid universe
- an exact candidate runtime in broker-emission-disabled posture, with rollback scoped only to that isolated trust domain
