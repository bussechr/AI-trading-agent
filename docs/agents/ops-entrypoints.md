# Ops Entrypoints

## Primary Files
- [_env.bat](../../ops/windows/_env.bat)
- [20_start_bridge.bat](../../ops/windows/20_start_bridge.bat)
- [21_start_runtime.bat](../../ops/windows/21_start_runtime.bat)
- [22_start_dashboard.bat](../../ops/windows/22_start_dashboard.bat)
- [23_start_monitor.bat](../../ops/windows/23_start_monitor.bat)
- [24_start_feature_push_worker.bat](../../ops/windows/24_start_feature_push_worker.bat)
- [25_monitor_everything.ps1](../../ops/windows/25_monitor_everything.ps1)
- [26_operator_plane.bat](../../ops/windows/26_operator_plane.bat)
- [13_train_all.bat](../../ops/windows/13_train_all.bat)
- [14_activate_models.bat](../../ops/windows/14_activate_models.bat)
- [find_owned_instance_processes.ps1](../../ops/windows/find_owned_instance_processes.ps1)
- [90_stop_all.bat](../../ops/windows/90_stop_all.bat)

## Upstream
- [AGENTS.md](../../AGENTS.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Start Order
- `_env.bat`: shared env and interpreter resolution
- `20_start_bridge.bat`: bridge + readiness on `/v2/ready`
- `21_start_runtime.bat`: runtime + startup phase watchdog
- `22_start_dashboard.bat`: Next.js production server
- `23_start_monitor.bat`: monitor confidence loop
- `24_start_feature_push_worker.bat`: drains runtime feature-push intents into the Feast online store
- `25_monitor_everything.ps1`: consolidated watch of training, bridge, dashboard, runtime
- `26_operator_plane.bat`: describe or attach an explicitly enabled read-only stdio MCP server
- `90_stop_all.bat`: repo-scoped Windows shutdown plus runtime snapshot clear

## Isolated Training And Activation

- Keep a candidate run out of the active artifact tree with `FXSTACK_TRAIN_ARTIFACT_ROOT` and `FXSTACK_TRAIN_REGISTRY_ROOT`. The shorter `FXSTACK_ARTIFACT_ROOT` / `FXSTACK_REGISTRY_ROOT` names are not launcher inputs.
- Use `FXSTACK_FORCE_RETRAIN=1` after feature-contract or numerical-integrity changes. Set `FXSTACK_TRAIN_WITH_BELIEF=0` for pair batches after training the single cross-pair belief bundle once.
- Before leaving a long batch unattended, inspect the spawned `src.trader.cli train all` command and confirm both isolated paths appear.
- Validate and activate the candidate registry with `FXSTACK_ACTIVATE_REGISTRY_ROOT` and `FXSTACK_ACTIVATE_MANIFEST`; keep the manifest outside `fx-quant-stack/artifacts/active_models.json` until smoke checks pass.
- Activation changes a manifest, not broker execution authority. End-to-end proof remains shadow-only and requires repository-owned listeners plus explicit heartbeat/tick freshness evidence.

## Endpoint And Auth Contract

- `launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]` selects both endpoints once before startup.
- `launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]` performs only the bind checks and prints/persists the resolved URLs; it starts no service.
- `resolve_stack_endpoints.ps1` verifies actual loopback binds, so active listeners and Windows excluded TCP ranges are both rejected. Omitted ports may move upward to the first bindable port; explicit command-line ports are strict.
- The selected ports are persisted in ignored `logs/active_stack_env.bat`. Active values override installed defaults, so `_env.bat`, status, monitor, stop, bridge, runtime, and dashboard consume the same endpoints on later invocations. `90_stop_all.bat` removes the active files after cleanup.
- `_env.bat` derives `MT4_BRIDGE_URL`, `TRADER_BRIDGE_URL`, and `TRADER_DASHBOARD_URL` from those endpoints unless an operator supplied an explicit URL.
- Bridge auth stays enabled by default. If no key was supplied, a 256-bit local key is generated once in ignored `logs/bridge_api_key.txt` and reused by all children. `FXSTACK_BRIDGE_API_KEY` remains the authoritative external override.
- Normal startup remains `FXSTACK_AGENT_MODE=shadow`; MCP, OpenClaw, remote LLM, and external tools remain disabled until explicitly enabled.
- For isolated audits, set `FXSTACK_SKIP_INSTALLED_ENV=1` before calling any Windows entrypoint. `_env.bat` then skips the optional credential-bearing `installed_env.bat` while still loading active endpoint state and process-supplied/default settings. This lets a guarded caller enforce shadow + SQLite + feature-push-off values without machine overrides.

## Baseline And Candidate Coexistence

- `21_start_runtime.bat` accepts an optional fourth `INSTANCE_ID`; ordinary calls default to `baseline`, while `24_start_candidate_stack.bat` passes `candidate` (or the validated `FXSTACK_CANDIDATE_INSTANCE_ID`).
- Runtime and feature-push child command lines carry `--instance-id`, allowing a restart to select only the same repository **and** the same instance. Legacy unmarked processes are treated as baseline only.
- Candidate runtime logs/PIDs use `runtime_candidate_<port>.*`; candidate feature-push state uses `feature_push_worker_candidate.*`. Baseline filenames remain backward-compatible.
- Feature-push database/outbox consumers also receive an instance-specific worker ID, preventing candidate claims from impersonating the baseline worker.
- `find_owned_instance_processes.ps1` is read-only and centralizes repository, role, and instance matching. `90_stop_all.bat` intentionally remains the all-instances shutdown entrypoint.

## Handshakes
- bridge readiness -> `/v2/ready`
- dashboard readiness -> HTTP `GET /`
- runtime readiness -> `/v2/ready` with startup phase fields
- feature push worker -> runtime outbox to Feast online store
- env propagation -> Windows batch exports mirrored into Python and Node child processes
- training isolation -> `13_train_all.bat` exact candidate roots -> `14_activate_models.bat` exact candidate manifest
- protected probes -> `X-API-Key` inherited from `_env.bat`

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
