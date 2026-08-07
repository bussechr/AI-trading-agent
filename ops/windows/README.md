# Windows Launcher Stack

Production startup orchestration for the fxstack v2 runtime.

## Primary entrypoint

- `launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]` (repo root): validated endpoint startup routed by strategy family. Every live path, including exact-22 IG-DEMO scalp, authenticates the selected signed release before sync/stop and revalidates its exact binding before runtime spawn.
- The root launcher requires an explicit action. Running or double-clicking `launch_all.bat` without arguments prints usage and starts nothing; broker-connected startup requires the literal `live` action.

When a default port is occupied or reserved by Windows, the launcher selects the next bindable loopback port and stores it in `logs/active_stack_env.bat`. Active endpoint state overrides installed defaults until `90_stop_all.bat` removes it. Explicit port arguments are strict. Bridge auth is enabled by default; an operator-supplied `FXSTACK_BRIDGE_API_KEY` is honored, otherwise `_env.bat` creates and reuses an ignored local key.

Use `launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]` to resolve and display the endpoint contract without starting any service.

For an isolated shadow audit, set `FXSTACK_SKIP_INSTALLED_ENV=1` together with the process-level shadow/SQLite/feature-push-off values. `_env.bat` will not read `installed_env.bat`, but it will still load active endpoints and fill any unset safe defaults.

## Modular scripts

- `00_dev_setup.bat`: idempotent developer-checkout bootstrap for the authoritative Python and dashboard workspaces; it does not launch services
- `00_preflight.bat`: environment and dependency checks
- `01_sync_python.bat`: `uv` sync for `fx-quant-stack/.venv`
- `02_sync_node.bat`: frozen `pnpm` install + dashboard doctor + lint/type-enforcing production build
- `03_postgres_start.bat`: postgres service start + readiness
- `04_db_migrate.bat`: alembic migrate + verify
- `05_gpu_check.bat`: direct focused CUDA requirement validation
- `10_ingest_all.bat`: direct focused Dukascopy CSV ingestion for all pairs/timeframes
- `11_features_all.bat`: direct focused feature build
- `12_labels_all.bat`: direct focused label build
- `13_train_all.bat`: direct focused model training per pair; isolated runs set all `FXSTACK_TRAIN_{RAW,FEATURE,LABEL,ARTIFACT,REGISTRY}_ROOT` values and `FXSTACK_TRAIN_ALLOW_INGEST=0`; set `FXSTACK_FORCE_RETRAIN=1` after feature-contract changes. The global belief bundle trains in the first successful pair job only and is reused thereafter; set `FXSTACK_TRAIN_WITH_BELIEF=0` to reuse an already validated bundle.
- `14_activate_models.bat`: direct focused activation of model sets into the DB and manifest
- `15_backtest_smoke.bat`: direct focused cost-aware smoke checks using the same policy edge/spread normalization as the compatibility facade
- `16_train_swing_transformer.bat`: force train swing transformer for all pairs, honoring `FXSTACK_TRAIN_*` roots
- `17_train_intraday_tcn.bat`: force train intraday TCN for all pairs, honoring `FXSTACK_TRAIN_*` roots
- `18_train_deep_stale.bat`: retrain deep artifacts only when stale, honoring `FXSTACK_TRAIN_*` roots
- `20_start_bridge.bat`: bridge startup/readiness; `--background-if-absent` is the collection-only no-reset mode and refuses any existing listener or matching bridge process
- `21_start_runtime.bat`: runtime startup/readiness. Every live launch validates signed release authority before reset; MTVCLC repeats the same public release verification inside runner startup.
- `21_start_scalp_runtime.bat`: scope-v3 exact-22 IG MT4 demo/real wrapper. It pins the strategy family, expected broker mode, exact pair/sleeve/intent scopes, hash-pinned runtime-native costs, and one-second cadence. Qualified entries execute immediate market `BUY` or `SELL` with broker SL/TP, pending orders forbidden, and maximum stop risk `0.5%` of current equity. Generic lot ceilings do not suppress the largest broker-valid lot within cash risk, margin, broker maximum, position-count, and drawdown controls.
- `21_run_scalp_runtime_task.ps1`: narrow hidden Scheduled Task trampoline that invokes only `21_start_scalp_runtime.bat --run` and propagates its exit code.
- `22_manage_scalp_runtime_task.ps1`: repo-owned persistent scalp definition for `TradingAgentScalpRuntime`. `Register` creates or repairs and enables the proven current-user Limited/InteractiveToken task; `Start` enables if needed and starts it. `Stop`, `Disable`, and `Unregister` remain exact-owner mutations, and `Stop` waits for Scheduler to report the instance stopped. The action runs `21_run_scalp_runtime_task.ps1` through Windows PowerShell with `-WindowStyle Hidden`; the previous exact `cmd.exe` action remains recognized only for safe stop, disable, unregister, or repair. The registrar has no MT4 or credential surface. It is included in the production package so uninstall can disarm it before the egress-safe stack stop and remove only the exact owned definition afterward.
- `22_start_dashboard.bat`: dashboard startup/readiness
- `23_start_monitor.bat`: confidence monitor
- `24_start_candidate_stack.bat`: nonzero quarantine stub; candidates run only on an external isolated host or VM
- `24_start_feature_push_worker.bat`: baseline-only Feast outbox worker
- `25_monitor_everything.bat`: auth-aware aggregate monitor using the active endpoint state
- `27_guard_mtvclc_collector_resilient_v3.ps1`: active gap-v5 collection-only `Health`, `AdoptRunning`, and full-lifetime `StartOrResume` supervisor; it pins the v4 collector adapter, v3 collector template, v3 continuity inspector and reusable pinned core, producer software, preregistration, and output identity and has no signal/performance/runtime/trade capability
- `30_checkpoint_ig_tick_microstructure_candidate.ps1` and `30_manage_ig_tick_microstructure_continuity_task.ps1`: authenticated read-only readiness plus append-only exact-22 source/counter continuity checkpoints for the sealed two-cell prospective tick window. The registrar defaults to non-mutating preview; explicit installation creates a hidden reversible hourly task that pins every invoked source, stores only credential paths, uses `IgnoreNew`, and has no signal, performance, runtime, command, or trading surface.
- `28_ensure_mtvclc_collection_dependencies.ps1`: explicit operator-invoked check that idempotently proves or restores only the exact repository bridge on `127.0.0.1:58710` and the configured visible IG terminal, then requires authenticated IG-DEMO exact-22 readiness; it refuses foreign or ambiguous ownership and never starts the runtime, stops an existing process, or installs persistence
- `29_ensure_mtvclc_collector_resilient_v3.ps1`: active hash-pinned full-lifetime task action; after exact zero-writer/unlocked/open-window health it invokes the identical gap-v5 guard synchronously, preserves task-action-to-writer ancestry, supports only one exact orphan-writer adoption when the lock is available, and never kills a process
- All recurring collection/preservation task registrars default to non-mutating preview; installation is always explicit.
- `29_register_mtvclc_collector_resilient_watchdog_v3.ps1`: active reversible preview/installer/starter/remover for `TradingAgentMtvclcV5Collector`; `Start` proves the unchanged task remains `Running`, binds the new Scheduler instance and EnginePID to the exact action/guard/writer ancestry, and requires two separated healthy locked-writer samples
- `29_preserve_mtvclc_capture.ps1`: operator-invoked collection-only durability monitor and append-only replica for one exact resilient preregistration/capture/guard tuple; requires an explicit existing backup root on a different volume, warns below 500 GB capture-volume free space, refuses below 311 GB or on stale manifest/journal metadata, copies only stability-proven closed chunks plus content-addressed manifest snapshots, never copies the active journal, and never deletes or overwrites backup data
- `29_register_mtvclc_capture_preservation_task.ps1`: reversible identity-safe registrar for the exact preservation tuple; preview is mutation-free, the default task is current-user `AtLogOn` plus hourly repetition, optional `AtStartup` mutation is administrator-only, and the action pins the preservation-script hash, exact preregistration/capture/backup paths, expected identity hashes, thresholds, and freshness policy. The task uses `IgnoreNew` and `StartWhenAvailable`, never starts during registration, has no credential/key or evidence-evaluation surface, and overwrite/removal requires the complete owned definition
- The unversioned and v2 collector guard/watchdog families remain only as historical compatibility and pinned templates; they are not active v5 operator entrypoints
- The active external evidence chain is `seal_mt4_tick_volume_preregistration_resilient_v5.py` -> `capture_ig_mt4_m1_activity_resilient_v4.py` -> `verify_mt4_tick_volume_capture_handoff_v5.py` -> `evaluate_mt4_tick_volume_post_window_v5.py` -> authority-free `mtvclc_validation_release_v5.py` evidence-v3 -> `mtvclc_runtime_release.py` outer-v2. It runs only on the physically isolated research/release host and none of these tools starts the production runtime or MT4 trading
- `30_fast_gate_15m.bat`: nonzero quarantine stub; the 15-minute gate runs externally
- `31_shadow_24h.bat`: nonzero quarantine stub; the 24-hour shadow gate runs externally
- `32_finalize_audit.bat`: invoke `tools/finalize_build.py` directly to finalize GO/HOLD audit outputs
- `40_full_scale_e2e_validation.bat`: nonzero quarantine stub; full validation runs externally
- `stop_owned_stack_processes.ps1`: kill only repo-owned or fresh PID-marker-bound worker trees and verify configured listeners are gone
- `90_stop_all.bat`: durably revoke execution egress and quarantine commands, then invoke verified repo-owned process-tree/listener shutdown; MT4 remains running

### Persistent IG-DEMO scalp runtime

Run the task owner from the repository root. With no `-Action`, it performs the same mutation-free status read as `Status`:

```powershell
powershell.exe -NoProfile -File ops\windows\22_manage_scalp_runtime_task.ps1
powershell.exe -NoProfile -File ops\windows\22_manage_scalp_runtime_task.ps1 -Action Register -Confirm:$false
powershell.exe -NoProfile -File ops\windows\22_manage_scalp_runtime_task.ps1 -Action Stop -Confirm:$false
powershell.exe -NoProfile -File ops\windows\22_manage_scalp_runtime_task.ps1 -Action Disable -Confirm:$false
powershell.exe -NoProfile -File ops\windows\22_manage_scalp_runtime_task.ps1 -Action Unregister -Confirm:$false
```

`Register` enables the exact owned definition and `Start` starts it. `Disable` prevents later on-demand/logon starts without terminating a running instance. Stop a running instance explicitly before `Unregister`. Every mutation refuses a same-named task whose owner marker, exact command, repository working directory, or current-user principal does not prove repository ownership.

Task start still requires deployment-owned live/live/armed, both shadow controls off, and expected account mode `demo`. The launcher validates the signed IG-DEMO release before reset and runner repeats that verification before authority activation; durable queue admission still requires fresh matching broker attestation. Registration never starts MT4.

The production host admits only instance ID `baseline`. The batch launchers and installed runtime/feature-worker CLIs reject every other identity before database or process mutation. `find_owned_instance_processes.ps1` remains only so shutdown can recognize stale repo-owned processes from before this quarantine; it does not authorize candidate coexistence.

Data ingest defaults:

- `FXSTACK_DUKASCOPY_SOURCE_ROOT` (default: `fx-quant-stack/data/dukascopy`)
- `FXSTACK_DUKASCOPY_FILE_PATTERN` (default: `{pair}_{granularity}.csv`)

## Dashboard Contract

- `22_start_dashboard.bat` is the authoritative launcher for `%TRADER_DASHBOARD_URL%`.
- Production build preparation happens in `02_sync_node.bat`; the doctor rejects disabled type/lint gates and `next build` executes both checks.
- `22_start_dashboard.bat` runs the production Next server on `%TRADER_DASHBOARD_HOST%:%TRADER_DASHBOARD_PORT%` against an existing `.next/BUILD_ID`.
- `pnpm dev` is not part of normal ops and should be used only for developer preview on `http://127.0.0.1:3001`.

## External Full-Scale E2E Profile

`40_full_scale_e2e_validation.bat` intentionally returns nonzero on the production host. Run training and exact-candidate validation on a separate host or VM with no production database, API key, bridge, MT4/broker access, registry-write access, or writable production mounts. Import externally signed, content-addressed evidence only through the release quarantine workflow. Authenticated signed evidence is mandatory for every live path, including exact-22 IG-DEMO scalp.

Before first production restart after removing a former same-host candidate, follow the one-time retired-candidate cleanup in [the ops entrypoint runbook](../../docs/agents/ops-entrypoints.md#one-time-retired-candidate-cleanup). It covers exact terminal/account/Magic identification, candidate EA removal, bridge-key rotation, stale candidate artifact cleanup, and why arbitrary `terminal.exe` processes must never be killed.

The former weekly auto-retrain/auto-activate Scheduled Task surface is retired. No Windows entrypoint installs or launches it; training and activation remain explicit external pre-deployment operations. The uninstaller only removes an exact historical task whose action, description, and weekly trigger all match.

The production package also includes `ops\windows\provision_live_db_boundary.sql` for explicit administrator execution; neither install nor runtime executes privilege changes automatically. Installed Monitor shortcuts call the packaged `monitor_trading_agent.bat` helper, not the external aggregate monitor. A generated `.fxstack-install-root` identity prevents recursive install/uninstall against an unrelated target; upgrades mirror package-owned files while preserving local `logs` and merging `fx-quant-stack\data` without purging the runtime database, keys, endpoint state, or operator snapshots.

The external validation environment must enforce:

- `FXSTACK_REQUIRE_CUDA=0`
- 9-pair liquid universe
- an exact candidate runtime in broker-emission-disabled posture, with rollback scoped only to that isolated trust domain
