# Ops Entrypoints

## Primary Production Files

- [_env.bat](../../ops/windows/_env.bat)
- [00_preflight.bat](../../ops/windows/00_preflight.bat)
- [01_sync_python.bat](../../ops/windows/01_sync_python.bat)
- [validate_runtime_risk_limits.ps1](../../ops/windows/validate_runtime_risk_limits.ps1)
- [19_start_mt4.ps1](../../ops/windows/19_start_mt4.ps1)
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
- [stop_owned_stack_processes.ps1](../../ops/windows/stop_owned_stack_processes.ps1)
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
- `20_start_bridge.bat`: run `python -I -m uvicorn fxstack.api.app:app --loop asyncio:SelectorEventLoop` and wait for `/v2/ready`; the selector loop avoids Windows Proactor reset-callback trace churn from the EA's short-lived HTTP sockets
- `19_start_mt4.ps1`: reuse or visibly launch the configured/IG MT4 terminal before runtime admission; it never stops the terminal
- `21_start_runtime.bat`: run `python -I -m fxstack.runtime.runner` with a startup phase watchdog bound to the newly observed boot ID and spawned runtime PID
- `python -I -m fxstack.runtime.model_manifest_preflight`: read-only manifest, feature-contract, registry-provenance, and local-payload gate before runtime reset/spawn
- `22_start_dashboard.bat`: Next.js production server
- `23_start_monitor.bat`: run the installed `fxstack.runtime.monitor` module under Python isolated mode
- `24_start_feature_push_worker.bat`: run the installed `fxstack.runtime.feature_push_worker` module to drain runtime feature-push intents into Feast
- `26_operator_plane.bat`: describe or attach an explicitly enabled read-only stdio MCP server
- `90_stop_all.bat`: durably disable execution egress, revoke release authority, and quarantine queued commands before invoking `stop_owned_stack_processes.ps1`; shutdown uses process-tree semantics and must prove the configured bridge/dashboard listeners are gone before PID markers are removed, and it never stops MT4

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
- Validate and activate the candidate registry externally with `FXSTACK_ACTIVATE_REGISTRY_ROOT` and `FXSTACK_ACTIVATE_MANIFEST`; activation rejects every registry whose promotion status is not `eligible`. Keep the candidate manifest outside `fx-quant-stack/artifacts/active_models.json` until validation passes.
- Canary monitoring computes entry evidence from accepted queue records, not submission attempts: accepted means newly `queued` or a duplicate already `queued`/`delivered`. Zero approved and zero accepted entries is `insufficient_evidence`, not a passing ratio. Evidence carries its observation timestamp and stage index/percentage; advancement clears it, and the next stage requires a new runtime observation after the ramp. `advance_canary_stage` therefore cannot recycle one passing sample across exposure levels.
- Phase 5 pre-canary evidence is bound to one exact pair, release bundle, model set, active-manifest hash, and artifact-set hash. Advisory causal-research output is rejected. Run an external harness with `python -m fxstack.backtest.harness.lean` or `python -m fxstack.backtest.harness.nautilus` plus `--execute --model-manifest ... --economic-report ... --manifest-output ...`, then normalize it with `tools/assemble_phase5_economic_evidence.py`; planned or hand-weakened manifests are rejected. Runtime authority requires the actual candidate for at least 24 hours in broker-emission-disabled shadow posture with loaded lifecycle models, healthy samples, and zero emitted entry commands. Finalization separately requires distinct 15-minute and 24-hour artifacts/windows. Gate reload and finalization rehash the referenced files and derive gate state rather than trusting stored pass booleans.
- Before installer assembly, use `tools/stage_candidate_registry.py` to copy only validated immutable artifacts into an ignored repository-relative `fx-quant-stack/artifacts_shadow/...` handoff and rebase their registry references. Rerun candidate activation and read-only preflight against that staged registry before replacing the active manifest.
- Assemble the installer only after activation has produced the immutable active manifest, registry provenance, and content-digested artifacts consumed by production preflight. The deployed package can validate and load that handoff but cannot activate or rewrite it.
- End-to-end release proof runs the exact candidate on the external validation host or VM in broker-emission-disabled shadow posture. Its database, bridge, credentials, feature/raw roots, registry, logs, and rollback control stay outside the production trust domain. Production receives only the externally signed, content-addressed evidence bundle through the quarantine import workflow.
- On production, `models release-request-export --pair ... --bundle-run-id ... --manifest ... --output ...` imports the finalized Phase-5 supports into a contained content-addressed tree and exports the exact unsigned claims. It cannot sign. A physically separate witness signs those claims with the OS-pinned Ed25519 identity; `models canary-start` requires both `--release-request` and `--external-witness`, and the booted runtime must independently ACK the same package/config/model/evidence generation before egress can enable.
- The installed package is measured as a complete link-free relative-file/SHA-256 inventory. Signed build provenance must match that measured Merkle digest and the package root actually executing. The signed runtime-config digest classifies every public setting exactly once; endpoint, secret, machine path, training, and observability exclusions are explicit rather than substring-based.

## Endpoint And Auth Contract

- `launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]` selects both endpoints once before startup.
- `launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]` performs only the bind checks and prints/persists the resolved URLs; it starts no service.
- `resolve_stack_endpoints.ps1` verifies actual loopback binds, so active listeners and Windows excluded TCP ranges are both rejected. Omitted ports may move upward to the first bindable port; explicit command-line ports are strict.
- The selected ports are persisted in ignored `logs/active_stack_env.bat`. Active values override installed defaults, so `_env.bat`, status, monitor, stop, bridge, runtime, and dashboard consume the same endpoints on later invocations. `_env.bat` never reads the retired `active_candidate_env.bat` artifact. `90_stop_all.bat` removes both active endpoint state and any stale candidate artifact after cleanup.
- Live startup persists endpoint state after any side-by-side Python environment switch, because that switch stops the previous owned stack and clears stale endpoint state.
- `_env.bat` derives `MT4_BRIDGE_URL`, `TRADER_BRIDGE_URL`, and `TRADER_DASHBOARD_URL` from those endpoints unless an operator supplied an explicit URL.
- Bridge auth stays enabled by default. If no key was supplied, a 256-bit local key is generated once in ignored `logs/bridge_api_key.txt` and reused by all children. `FXSTACK_BRIDGE_API_KEY` remains the authoritative external override.
- Runtime launch posture is resolved by `21_start_runtime.bat --validate`. `launch_all.bat live` uses `--validate-models` to run posture plus active-model checks before stack cleanup, endpoint persistence, or any bridge/runtime spawn, and the `--run` / `--background` paths repeat both gates before resetting a runtime process.
- `00_preflight.bat` package mode runs the installed `fxstack.runtime.package_preflight` module. It rejects missing required dependencies, invalid settings/database posture, MLflow, or any forbidden training/research/write-side module visible inside the isolated interpreter.
- `21_start_runtime.bat --validate-models`, `--run`, and `--background` run the read-only active-model preflight from the non-editable, research-pruned installed package under Python isolated mode before any runtime process reset or spawn. The gate requires every configured pair in `FXSTACK_PAIRS`, `promotion_status=eligible`, the current feature contract, matching local registry provenance where applicable, and valid local artifact payload digests. A `pair=GLOBAL` scope is accepted only for the cross-pair directional-belief component; it never relaxes contract or payload checks. The preflight never downloads, deserializes, trains, promotes, activates, writes the manifest, or touches the runtime database.
- Direct `python -I -m fxstack.runtime.runner` invocation cannot bypass startup admission: Python accepts only the literal `--instance-id baseline`, repeats settings validation, profile/mode/live arming and explicit-scope checks, requires binding structure-timing/chase, uncertainty, belief, campaign, and capital-governance settings, proves the complete forbidden-module set is absent, and runs the same read-only model preflight before bridge checks or `RuntimeService` access. The feature-push worker enforces the same baseline-only identity. The production settings and runner contain no adaptive observation-twin switch or baseline-ranking comparator.
- Live startup additionally reads trust policy only from the fixed OS path and checks the runtime SID/principal and pinned files. Policy booleans cannot prove isolation: without independently observed least-privilege DB grants, singleton bridge consumer, terminal-wide EA lease, poll/ACK token, credential rotation, and research-credential absence, startup reports `physical_boundary_unproven`. Staged-safe startup does not require those live-only capabilities.
- With neither value supplied, `FXSTACK_START_PROFILE=staged_safe` deterministically resolves to `FXSTACK_AGENT_MODE=shadow`. The production runtime rejects `FXSTACK_START_PROFILE=paper` because the simulated execution adapter is physically absent. Any explicit profile/mode mismatch fails before spawn.
- Live posture is never inferred: it requires `FXSTACK_START_PROFILE=live`, an explicitly matching `FXSTACK_AGENT_MODE=live`, `FXSTACK_LIVE_ARMED=1`, an explicit `FXSTACK_LIVE_EXPECTED_ACCOUNT_MODE=demo` or `real`, and explicit non-empty `FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST`, `FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST`, and `FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST` values. It also requires structure/chase, uncertainty, belief hard-gate, campaign, and capital-governance producers to be binding. `FXSTACK_ADAPTIVE_EXECUTION_ENABLED` is the sole adaptive runtime switch; when enabled it directly owns adaptive history, evaluation, ranking, and sleeve governance. The former `FXSTACK_ADAPTIVE_SHADOW_ENABLED` and baseline comparator are absent. `_env.bat` never chooses the expected broker mode or live scopes; any missing or disabling override makes Python startup fail closed.
- After model load, Python live startup also requires every configured pair to be explicitly live-scoped and backed by a configured, active, pair-allowlisted `canary` rollout with positive budget. The intent allowlist must contain only supported intents and include `enter`, plus `exit` and `reduce` when lifecycle actions are enabled and `tighten_stop` when adjustment actions are enabled; otherwise startup raises `live_command_admission_blocked` before trading begins.
- Entry-canary checks apply only to new `enter` authority. Protective lifecycle actions do not depend on entry-canary budget, but they still require an externally signed protective scope for the exact account/pair/generation/boot and remain subject to runtime/queue kills, governance, risk, reconciliation, and broker validation. `CLOSE_ALL` requires a separately signed emergency-flatten capability.
- Every broker verb—including protective actions and `INFO`—is fenced at both database enqueue and MQ4 poll against the acknowledged release generation. Missing, pending, rejected, expired, superseded, or drifted authority returns an empty/quarantined poll result; there is no status-command or old-process bypass.
- Live `enter` also requires a fresh MQ4 heartbeat attesting a known `demo` or `real` account mode and a non-empty account scope derived from account number, server, and Magic. `contest`, `unknown`, missing, or stale attestation blocks the entry; `FinalEntryApproval` carries the mode/scope and service enqueue rereads state to reject identity drift. This entry-only attestation gate does not suppress protective commands.
- Live validation also requires finite positive sizing and hard-risk limits before any process reset or spawn. The shipped low-risk defaults scale at `0.00001` lots/USD, cap each order at `0.10` lots, stop new entries at `5%` drawdown, and cap gross/net lot exposure at `0.30`/`0.20`; zero is never interpreted as “unbounded” in an execution posture. `validate_runtime_risk_limits.ps1` rejects disabled, non-finite, or internally inconsistent installed-env overrides, and Python startup repeats the contract through `Settings.validate_for_startup()`.
- MCP, OpenClaw, remote LLM, and external tools remain disabled until explicitly enabled.
- Production operations expose no offline self-correction launcher. The former continuous supervisor is removed; self-improvement evidence is produced only in a physically isolated research environment and has no runtime-database registration path.
- For isolated audits, set `FXSTACK_SKIP_INSTALLED_ENV=1` before calling any Windows entrypoint. `_env.bat` then skips the optional credential-bearing `installed_env.bat` while still loading active endpoint state and process-supplied/default settings. This lets a guarded caller enforce shadow + SQLite + feature-push-off values without machine overrides.

## Single Production Instance

- The production host admits exactly one runtime/bridge/feature-worker stack with instance ID `baseline`. Batch launchers and both installed Python CLIs reject every other identity before database or process mutation.
- `24_start_candidate_stack.bat`, `30_fast_gate_15m.bat`, `31_shadow_24h.bat`, and `40_full_scale_e2e_validation.bat` are nonzero quarantine stubs. They cannot start, observe, roll back, or stop a same-host candidate.
- `find_owned_instance_processes.ps1` remains a read-only ownership selector so shutdown can recognize and remove a stale pre-quarantine repo-owned process. It does not authorize starting another instance.
- `90_stop_all.bat` first runs `python -I -m fxstack.runtime.execution_egress_control --reason operator_stop_all`. No taskkill occurs unless that command confirms egress disabled, release authority inactive/revoked, and the command quarantine committed. `stop_owned_stack_processes.ps1` then admits only root-owned workers or workers bound to a fresh launcher PID marker, kills their full process trees, and waits until no admitted worker or configured listener remains. Stale PID reuse is rejected by comparing process creation time to marker write time. Process cleanup is repository-scoped; the global `python.exe` kill escape hatch is absent.

## One-Time Retired Candidate Cleanup

Perform this once on every former dual-stack workstation before treating it as a production host:

1. Identify the retired candidate terminal by its exact terminal data path, broker account/server, Magic number, and bridge URL. Reconcile open positions and pending orders for that exact account before changing the terminal.
2. In that identified terminal only, disable AutoTrading, detach/disable the candidate BridgeEA, and remove any candidate auto-start shortcut, Scheduled Task, or service. Do not use `taskkill /im terminal.exe`; the Windows scripts intentionally never kill arbitrary MT4 terminals.
3. Revoke the retired candidate bridge credential at its consumer, rotate the production bridge API key, update the production EA and `installed_env.bat` together, and verify old-key requests are rejected before re-enabling the production EA.
4. Run `90_stop_all.bat` to durably revoke production execution egress and remove repo-owned processes plus stale `logs/active_candidate_env.bat`. Archive the retired candidate database/data/log roots offline; do not mount them into production.
5. Restart only `launch_all.bat live` in staged-safe posture and verify one baseline runner, one bridge, one feature worker (when enabled), the intended MT4 account/server/Magic heartbeat, and no traffic from the retired candidate terminal. Live arming still requires a separately authorized external release.

## Handshakes
- bridge readiness -> `/v2/ready`
- dashboard readiness -> HTTP `GET /`
- runtime readiness -> `/v2/ready` with startup phase, boot ID, and runtime PID fields; the launcher ignores persisted status from an earlier boot generation
- feature push worker -> runtime outbox to Feast online store
- env propagation -> Windows batch exports mirrored into Python and Node child processes
- hard-risk launch gate -> live sizing, drawdown, gross, and net caps validated before runtime mutation or spawn; paper is rejected at posture admission
- Python startup admission -> literal baseline-only instance -> settings/posture, physical absence of the adaptive observation twin and research package, and read-only active-manifest identity before bridge/service access
- shutdown safety -> durable execution-egress disable + release revocation + queue quarantine confirmation -> repo-owned process kill -> runtime snapshot clear; MT4 remains running
- live command admission -> loaded model rollout + explicit pair/sleeve/intent scopes -> active positive-budget canary for every configured pair, with enabled protective intents present
- MQ4 account attestation -> authoritative heartbeat mode/scope/Magic -> fresh known demo/real live-entry admission -> identity-bound service enqueue
- canary advancement -> post-pack monitor check -> evaluable approved/accepted queue-record evidence bound to the current stage and observed after the previous ramp; attempts do not count, 0/0 remains `insufficient_evidence`, and advancement clears the sample
- activation identity -> preflight manifest SHA-256 + required-pair manifest/DB/loaded model identity consistency, with no activation capability in production
- pre-deployment artifact handoff -> external `13_train_all.bat` exact candidate roots -> external `14_activate_models.bat` immutable active manifest/artifacts -> installer copies only exact manifest-named local payloads -> installed read-only preflight
- package boundary -> filtered site-packages distribution -> direct `python -I -m` bridge/runner/worker/monitor/preflight entrypoints
- protected probes -> `X-API-Key` inherited from `_env.bat`

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
