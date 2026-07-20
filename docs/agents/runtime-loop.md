# Runtime Loop

## Primary Files

- [package_preflight.py](../../fx-quant-stack/src/fxstack/runtime/package_preflight.py)
- [startup_preflight.py](../../fx-quant-stack/src/fxstack/runtime/startup_preflight.py)
- [model_manifest_preflight.py](../../fx-quant-stack/src/fxstack/runtime/model_manifest_preflight.py)
- [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- [service.py](../../fx-quant-stack/src/fxstack/runtime/service.py)
- [postgres_store.py](../../fx-quant-stack/src/fxstack/runtime/postgres_store.py)

## Upstream
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Startup Phases

- installed-package admission -> validate settings/dependencies/database posture -> prove the complete forbidden training, activation, registry-write, research, replay, improvement, and RL-training module set is absent
- runner admission -> validate profile/mode/live arming and explicit scopes -> reject the adaptive observation twin in live posture -> require binding structure-timing/chase, uncertainty, belief, campaign, and capital-governance producers -> read-only active-manifest preflight
- bridge checks -> boot -> patch boot state -> purge pending commands
- hash-anchored manifest seed -> required-pair seed gate -> model load -> live command admission -> live feature refresh
- startup inference dry run -> pre-deployed manifest/model consistency -> readying state
- main loop -> per-pair scoring -> lifecycle -> submissions -> state patch

## Single Runtime Boundary

- Production launches only the installed `fxstack.runtime.runner` module under `python -I`. The bridge, feature-push worker, monitor, and preflights are sibling entrypoints from the same filtered distribution.
- No production process imports repository source, a repository CLI shim, training activation, mutable MLflow/registry code, causal research, or a repository worker helper.
- There is one model-loading and decision path. Alternate bundle loading is absent; `FXSTACK_AGENT_MODE=shadow` only changes execution authority for the same runner and deployed model set.
- Training, research, promotion, and activation finish on an external build/research host. Runtime receives only the pre-activated immutable manifest, registry provenance, and content-digested artifact payloads assembled into the deployment.

## Main Loop Phases

- refresh live bars from bridge ticks/bars
- load latest feature rows per timeframe
- compute one versioned capital-governance snapshot from the latest complete cycle and current book before evaluating entries
- score live signal and baseline gates
- compute lifecycle / reversal / entry candidates
- apply direct adaptive policy ranking when enabled; optional observation diagnostics are a staged-safe-only computation and are never production authority
- resolve one canonical post-adaptive entry intent, rerun hard risk in allocator order, and reserve portfolio capacity only for entries with an exact approved order
- refresh sleeve health after exit accounting; degraded, missing, mismatched, or invalid sleeve state hard-blocks new entries only when direct adaptive execution is enabled
- pass the canonical risk-approved intent to committee/governor orchestration; later stages are veto-only and cannot resurrect a blocked or payload-less entry
- submit exits first, entries second
- patch the exact admission-time governance snapshot to both top-level `governance` and runtime diagnostics, then persist decisions

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync
- lifecycle models score exit / partial / reversal on the enriched row
- direct adaptive runtime can override hold/exit/rotation on top of model outputs without depending on an observation twin
- live startup rejects `FXSTACK_ADAPTIVE_SHADOW_ENABLED=1`; production authority comes from `FXSTACK_ADAPTIVE_EXECUTION_ENABLED` directly, while observation-only ranking remains confined to non-live diagnostics and cannot veto, reorder, or reserve capacity
- live `enter` requires a fresh MQ4 heartbeat with a known `demo` or `real` broker account mode and a non-empty account scope; `contest`, `unknown`, missing, or stale attestation fails closed, while protective lifecycle authority remains independent of this entry-only gate
- entry canary rollout gates only new `enter` actions; protective `exit`, `reduce`, and `tighten_stop` actions remain available subject to their live intent scope and the ordinary runtime, queue, governance, and risk vetoes
- command submission happens only at final cycle evaluation

## Handshakes

- filtered installed package -> package preflight -> runner: dependency and physical-isolation checks complete before any production entrypoint is trusted
- direct installed runner -> startup admission: `Settings.validate_for_startup()`, launch-posture invariants, complete forbidden-module absence, and read-only active-model validation all complete before bridge checks or `RuntimeService` import/construction
- external activation -> immutable deployment handoff -> DB seed -> loaded runtime: one preflight SHA-256 anchors the pre-deployed manifest through seeding and consistency; required pairs must match on presence, model-set ID, registry path, and available artifact identity/digests or startup fails
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime feature root -> sibling raw root: live bar refresh and feature-tail writes stay in one explicitly selected data tree
- runtime -> bridge state store: `patch_state`, `store_decisions`
- direct adaptive allocator -> canonical final entry risk -> committee/governor: adaptive selection can recover only scorer-owned probability rejections; freshness, venue, protection, governance, exposure, and direct-adaptive sleeve blockers remain binding, and only final-risk-approved entries reserve portfolio slots; observation diagnostics have no authority
- MQ4 heartbeat -> bridge state -> live entry admission: every heartbeat emits `account_mode`, `account_scope`, and `account_magic`; the bridge resets the attested mode/scope before parsing so missing identity tokens cannot inherit prior entry authority, and the scope binds account number, server, and Magic without exposing the raw account number
- runtime -> commands queue -> broker poll: live BUY/SELL uses `submit_approved_command` with in-process `FinalEntryApproval` bound to the fresh broker account mode/scope and current monotonic authority revision; enqueue atomically rechecks the exact revision plus runtime authority and fresh heartbeat/pair tick, and broker poll repeats the same check immediately before delivery, expiring revoked, superseded, stale, or unattested entries without blocking protective commands
- live model set -> startup command admission: every configured pair must have an active explicit pair-allowlisted canary rollout with positive budget, and the live intent scope must include `enter` plus enabled protective lifecycle intents, or startup fails closed
- queue admission -> live entry evidence: attempts and raw submit calls remain diagnostic only; an accepted entry requires a newly queued command or a duplicate that resolves to an existing `queued` or `delivered` record
- runtime evidence -> canary advancement: entry evidence is stamped with its observation time and current stage index/percentage; a ramp clears it, and only a later matching-stage observation inside the configured alert window can authorize another ramp; ACK metrics use the command event's durable `ts`
- runtime/release/operator -> state store: each runtime cycle sends the live-authority snapshot it read, while release and operator mutations use a row-locked atomic authority patch. Every authority mutation advances `authority_revision`; safety mutations are dominant, and only an explicit canary start may re-enable killed authority. Transactional cycle patches preserve any newer kill, release scope, canary stage, bundle identity, audit fields, and stage-evidence reset instead of shallow-overwriting them with stale state.
- bridge state -> operator readiness: `entry_configuration_ready` reports static rollout admission, while `new_entry_ready` additionally requires effective authority enabled in `live` mode with a positive revision, live runtime/queue state, fresh signal transport, matching broker-mode/scope attestation, and no unresolved execution; authoritative empty scopes and zero values are never replaced by configuration fallbacks
- previous cycle -> capital governance -> entry admission: missing, stale, or schema-mismatched state pauses new entries for one fresh bootstrap cycle; shadow alignment consumes the producer's `shadow_live_divergence_counts` snake-case contract; protective lifecycle actions remain available
- sleeve tracker -> final entry admission: the current in-memory typed snapshot binds only direct-adaptive entries; `watch` remains a soft allocator penalty and `degraded` is a hard adaptive-entry veto, while strict entries are independent of observation-only sleeve state

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
