# Ops Entrypoints

## Primary Production Files

- [_env.bat](../../ops/windows/_env.bat)
- [00_preflight.bat](../../ops/windows/00_preflight.bat)
- [01_sync_python.bat](../../ops/windows/01_sync_python.bat)
- [validate_runtime_risk_limits.ps1](../../ops/windows/validate_runtime_risk_limits.ps1)
- [20_start_bridge.bat](../../ops/windows/20_start_bridge.bat)
- [21_start_runtime.bat](../../ops/windows/21_start_runtime.bat)
- [22_start_dashboard.bat](../../ops/windows/22_start_dashboard.bat)
- [23_start_monitor.bat](../../ops/windows/23_start_monitor.bat)
- [24_start_feature_push_worker.bat](../../ops/windows/24_start_feature_push_worker.bat)
- [26_operator_plane.bat](../../ops/windows/26_operator_plane.bat)
- [package_preflight.py](../../fx-quant-stack/src/fxstack/runtime/package_preflight.py)
- [model_manifest_preflight.py](../../fx-quant-stack/src/fxstack/runtime/model_manifest_preflight.py)
- [feature_push_worker.py](../../fx-quant-stack/src/fxstack/runtime/feature_push_worker.py)
- [monitor.py](../../fx-quant-stack/src/fxstack/runtime/monitor.py)
- [setup.py](../../fx-quant-stack/setup.py)
- [build_windows_installer.py](../../tools/build_windows_installer.py)
- [find_owned_instance_processes.ps1](../../ops/windows/find_owned_instance_processes.ps1)
- [90_stop_all.bat](../../ops/windows/90_stop_all.bat)

## External Pre-Deployment Files

- [13_train_all.bat](../../ops/windows/13_train_all.bat)
- [14_activate_models.bat](../../ops/windows/14_activate_models.bat)
- [preflight_active_models.py](../../tools/preflight_active_models.py)
- [25_monitor_everything.ps1](../../ops/windows/25_monitor_everything.ps1)

## Upstream
- [AGENTS.md](../../AGENTS.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Start Order

- `_env.bat`: shared environment and bundled-interpreter resolution
- `01_sync_python.bat`: on a build/development host, install the filtered non-editable runtime distribution; in package mode, verify the bundled interpreter without importing repository source
- `00_preflight.bat`: in package mode, run `python -I -m fxstack.runtime.package_preflight`
- `20_start_bridge.bat`: run `python -I -m uvicorn fxstack.api.app:app` and wait for `/v2/ready`
- `21_start_runtime.bat`: run `python -I -m fxstack.runtime.runner` with the startup phase watchdog
- `python -I -m fxstack.runtime.model_manifest_preflight`: read-only manifest, feature-contract, registry-provenance, and local-payload gate before runtime reset/spawn
- `22_start_dashboard.bat`: Next.js production server
- `23_start_monitor.bat`: run the installed `fxstack.runtime.monitor` module under Python isolated mode
- `24_start_feature_push_worker.bat`: run the installed `fxstack.runtime.feature_push_worker` module to drain runtime feature-push intents into Feast
- `26_operator_plane.bat`: describe or attach an explicitly enabled read-only stdio MCP server
- `90_stop_all.bat`: repo-scoped Windows shutdown plus runtime snapshot clear

`tools/preflight_active_models.py` is an external developer/build-host adapter for the same read-only validator. Production launchers never invoke it.

## Production Package Boundary

- The deployed Python surface is the filtered, non-editable `fx-quant-stack` distribution in bundled `site-packages`. The installer does not ship the raw `fx-quant-stack/src` tree, a repository CLI shim, or a repository feature-worker helper.
- Bridge, runner, feature-push worker, monitor, package preflight, and model-manifest preflight all execute from that installed distribution under `python -I`; there is no repository-source production fallback.
- The runtime distribution contains inference, policy, risk, portfolio, persistence, API, monitoring, and read-only artifact-validation code. Runtime callers use `fxstack.mlops.local_artifact`, which can only resolve already-present payloads; the MLflow client and the remote-capable `fxstack.mlops.model_uri` module are forbidden. Training activation, registry mutation, research/backtest, replay/experiment, LLM/improvement, RL training environments, and the other write-side modules checked by `runtime_physical_isolation_errors()` are physically absent.
- `build_windows_installer.py` independently reruns the current physical-isolation probe, packages the filtered site-packages runtime, an explicit production-only Windows launcher allowlist, and only the exact local artifact and registry paths named by the active manifest. It ships neither raw artifact-run roots, `fx-quant-stack/scripts`, the training aggregate monitor, nor training, activation, backtest, candidate-stack, or full-validation launchers; packaged `launch_all.bat full` fails closed.
- `FXSTACK_AGENT_MODE=shadow` is a safe execution posture of the same installed runner and model set, not a separate process or alternate model-loading path.

## Isolated Training And Activation

- Training, research, promotion, and activation run on an external build/research host before deployment. `13_train_all.bat`, `14_activate_models.bat`, and their Python dependencies are not production runtime entrypoints.
- MLflow and deep training/inference dependencies are opt-in through the external `external_mlops` and `deep_inference` extras; neither is a core runtime dependency. The packaged xgb-only environment installs neither extra.
- `25_monitor_everything.ps1` is an external build/research-host aggregate view because it inspects training and candidate state; the packaged monitor helper calls the installed `fxstack.runtime.monitor` instead.
- Keep a candidate run out of the active data and artifact trees with `FXSTACK_TRAIN_RAW_ROOT`, `FXSTACK_TRAIN_FEATURE_ROOT`, `FXSTACK_TRAIN_LABEL_ROOT`, `FXSTACK_TRAIN_ARTIFACT_ROOT`, and `FXSTACK_TRAIN_REGISTRY_ROOT`. The shorter `FXSTACK_ARTIFACT_ROOT` / `FXSTACK_REGISTRY_ROOT` names are not launcher inputs.
- Use `FXSTACK_TRAIN_PAIRS` to select the pair jobs for a candidate batch without narrowing `FXSTACK_PAIRS`, which remains the feature and directional-belief context universe. When omitted, the training selector defaults to the complete configured universe.
- Set `FXSTACK_TRAIN_ALLOW_INGEST=0` for point-in-time or otherwise isolated training. This makes missing snapshot inputs fail closed instead of rebuilding them from the project-wide raw tree.
- Deep-model launchers `16_train_swing_transformer.bat`, `17_train_intraday_tcn.bat`, and `18_train_deep_stale.bat` consume the same artifact, feature, and label roots.
- Use `FXSTACK_FORCE_RETRAIN=1` after feature-contract or numerical-integrity changes. Set `FXSTACK_TRAIN_WITH_BELIEF=0` for pair batches after training the single cross-pair belief bundle once.
- Before leaving a long batch unattended, inspect the external training process generated by `13_train_all.bat` and confirm every isolated root plus disabled ingest appears.
- Validate and activate the candidate registry externally with `FXSTACK_ACTIVATE_REGISTRY_ROOT` and `FXSTACK_ACTIVATE_MANIFEST`; keep the candidate manifest outside `fx-quant-stack/artifacts/active_models.json` until validation passes.
- Assemble the installer only after activation has produced the immutable active manifest, registry provenance, and content-digested artifacts consumed by production preflight. The deployed package can validate and load that handoff but cannot activate or rewrite it.
- End-to-end proof uses the single installed runner in shadow posture and requires repository-owned listeners plus explicit heartbeat/tick freshness evidence.

## Endpoint And Auth Contract

- `launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]` selects both endpoints once before startup.
- `launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]` performs only the bind checks and prints/persists the resolved URLs; it starts no service.
- `resolve_stack_endpoints.ps1` verifies actual loopback binds, so active listeners and Windows excluded TCP ranges are both rejected. Omitted ports may move upward to the first bindable port; explicit command-line ports are strict.
- The selected ports are persisted in ignored `logs/active_stack_env.bat`. Active values override installed defaults, so `_env.bat`, status, monitor, stop, bridge, runtime, and dashboard consume the same endpoints on later invocations. `90_stop_all.bat` removes the active files after cleanup.
- `_env.bat` derives `MT4_BRIDGE_URL`, `TRADER_BRIDGE_URL`, and `TRADER_DASHBOARD_URL` from those endpoints unless an operator supplied an explicit URL.
- Bridge auth stays enabled by default. If no key was supplied, a 256-bit local key is generated once in ignored `logs/bridge_api_key.txt` and reused by all children. `FXSTACK_BRIDGE_API_KEY` remains the authoritative external override.
- Runtime launch posture is resolved by `21_start_runtime.bat --validate`. `launch_all.bat live` uses `--validate-models` to run posture plus active-model checks before stack cleanup, endpoint persistence, or any bridge/runtime spawn, and the `--run` / `--background` paths repeat both gates before resetting a runtime process.
- `00_preflight.bat` package mode runs the installed `fxstack.runtime.package_preflight` module. It rejects missing required dependencies, invalid settings/database posture, MLflow, or any forbidden training/research/write-side module visible inside the isolated interpreter.
- `21_start_runtime.bat --validate-models`, `--run`, and `--background` run the read-only active-model preflight from the non-editable, research-pruned installed package under Python isolated mode before any runtime process reset or spawn. The gate requires every configured pair in `FXSTACK_PAIRS`, the current feature contract, matching local registry provenance where applicable, and valid local artifact payload digests; it never downloads, deserializes, trains, promotes, activates, writes the manifest, or touches the runtime database.
- Direct `python -I -m fxstack.runtime.runner` invocation cannot bypass startup admission: Python repeats settings validation, profile/mode/live arming and explicit-scope checks, requires binding desk-overlay, structure-timing/chase, uncertainty, belief, campaign, and capital-governance settings, proves the complete forbidden-module set is absent, and runs the same read-only model preflight before bridge checks or `RuntimeService` access.
- With neither value supplied, `FXSTACK_START_PROFILE=staged_safe` deterministically resolves to `FXSTACK_AGENT_MODE=shadow`. The production runtime rejects `FXSTACK_START_PROFILE=paper` because the simulated execution adapter is physically absent. Any explicit profile/mode mismatch fails before spawn.
- Live posture is never inferred: it requires `FXSTACK_START_PROFILE=live`, an explicitly matching `FXSTACK_AGENT_MODE=live`, `FXSTACK_LIVE_ARMED=1`, and explicit non-empty `FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST`, `FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST`, and `FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST` values. It also requires adaptive-overlay, structure/chase, uncertainty, belief hard-gate, campaign, and capital-governance producers to be binding. `_env.bat` exports those safe producer defaults explicitly; any installed override that disables one makes Python startup fail closed. `FXSTACK_ADAPTIVE_EXECUTION_ENABLED` remains independent. The shared environment supplies no live allowlist defaults.
- Live validation also requires finite positive sizing and hard-risk limits before any process reset or spawn. The shipped low-risk defaults scale at `0.00001` lots/USD, cap each order at `0.10` lots, stop new entries at `5%` drawdown, and cap gross/net lot exposure at `0.30`/`0.20`; zero is never interpreted as “unbounded” in an execution posture. `validate_runtime_risk_limits.ps1` rejects disabled, non-finite, or internally inconsistent installed-env overrides, and Python startup repeats the contract through `Settings.validate_for_startup()`.
- MCP, OpenClaw, remote LLM, and external tools remain disabled until explicitly enabled.
- Production operations expose no offline self-correction launcher. The former continuous supervisor is removed; self-improvement evidence is produced only in a physically isolated research environment and has no runtime-database registration path.
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
- hard-risk launch gate -> live sizing, drawdown, gross, and net caps validated before runtime mutation or spawn; paper is rejected at posture admission
- Python startup admission -> settings/posture + physical research-package exclusion + read-only active-manifest identity before bridge/service access
- activation identity -> preflight manifest SHA-256 + required-pair manifest/DB/loaded model identity consistency, with no activation capability in production
- pre-deployment artifact handoff -> external `13_train_all.bat` exact candidate roots -> external `14_activate_models.bat` immutable active manifest/artifacts -> installer copies only exact manifest-named local payloads -> installed read-only preflight
- package boundary -> filtered site-packages distribution -> direct `python -I -m` bridge/runner/worker/monitor/preflight entrypoints
- protected probes -> `X-API-Key` inherited from `_env.bat`

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
