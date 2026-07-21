# Runtime Loop

## Primary Files

- [package_preflight.py](../../fx-quant-stack/src/fxstack/runtime/package_preflight.py)
- [startup_preflight.py](../../fx-quant-stack/src/fxstack/runtime/startup_preflight.py)
- [model_manifest_preflight.py](../../fx-quant-stack/src/fxstack/runtime/model_manifest_preflight.py)
- [release_contract.py](../../fx-quant-stack/src/fxstack/runtime/release_contract.py)
- [release_trust.py](../../fx-quant-stack/src/fxstack/runtime/release_trust.py)
- [release_authority.py](../../fx-quant-stack/src/fxstack/runtime/release_authority.py)
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
- runner admission -> validate profile/mode/live arming and explicit scopes -> require binding structure-timing/chase, uncertainty, belief, campaign, and capital-governance producers -> read-only active-manifest preflight; live also requires a fixed OS trust policy plus independently observed DB, bridge/EA, token, and credential boundaries, otherwise it fails explicitly with `physical_boundary_unproven`; no adaptive observation-twin toggle or baseline comparator is present in the production package
- bridge checks -> boot -> patch boot state -> purge pending commands
- hash-anchored manifest seed -> required-pair seed gate -> model load -> runtime package/config/model attestation -> signed release generation revalidation/ACK -> live command admission -> live feature refresh
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
- compute exit-model and reversal evidence plus explicit hard lifecycle floors; with direct adaptive execution, those model outputs are evidence rather than a parallel action producer
- apply direct adaptive policy ranking only when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`; that same control exclusively owns adaptive history and evaluator execution
- resolve one canonical post-adaptive entry intent, rerun hard risk in allocator order, and reserve portfolio capacity only for entries with an exact approved order
- refresh sleeve health after exit accounting; degraded, missing, mismatched, or invalid sleeve state hard-blocks new entries only when direct adaptive execution is enabled
- pass the canonical risk-approved intent to committee/governor orchestration; later stages are veto-only and cannot resurrect a blocked or payload-less entry
- reread and revalidate the externally signed release generation; any package, config, model, manifest, evidence, scope, lease, boot, account, or nonce drift disables egress and quarantines queued commands
- submit exits first, entries second
- patch the exact admission-time governance snapshot to both top-level `governance` and runtime diagnostics, then persist decisions

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync
- lifecycle models score exit / partial / reversal evidence on the enriched row
- when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`, `adaptive_lifecycle_decision` is the single strategy producer for hold/reduce/exit; no baseline lifecycle action is compared with or allowed to suppress it
- the monotonic `hard_lifecycle_*` floor is limited to the hard time-stop exit and a pipeline-failure stop adjustment that has proved it strictly tightens the existing broker stop; it can upgrade protection but never downgrade an adaptive reduce/exit
- `FXSTACK_ADAPTIVE_SHADOW_ENABLED`, `FXSTACK_SHADOW_POLICY_ENABLED`, and the baseline shadow-ranking implementation are absent from production settings, startup, and telemetry
- live `enter` requires a fresh MQ4 heartbeat with a known `demo` or `real` broker account mode and a non-empty account scope; `contest`, `unknown`, missing, or stale attestation fails closed, while protective lifecycle authority remains independent of this entry-only gate
- entry canary rollout gates only new `enter` actions. Protective `exit`, `reduce`, and `tighten_stop` do not depend on entry-canary budget, but they still require the externally signed protective scope for the same account, pair, generation, and boot; broker-wide flatten requires a separately signed emergency capability
- every command type is stamped at final cycle evaluation and rechecked at enqueue and MQ4 poll; no legacy, status, or protective command bypasses release authority

## Handshakes

- filtered installed package -> package preflight -> runner: dependency and physical-isolation checks complete before any production entrypoint is trusted
- direct installed runner -> startup admission: `Settings.validate_for_startup()`, launch-posture invariants, complete forbidden-module absence, and read-only active-model validation all complete before bridge checks or `RuntimeService` import/construction
- live startup -> physical boundary admission: the fixed OS trust policy measures the runtime principal and pinned trust files, but policy claims are only intent; independently observed least-privilege DB grants, singleton bridge consumer, terminal-wide EA lease, poll/ACK token, credential rotation, and research-credential absence must also match or live remains non-operable
- external activation -> immutable deployment handoff -> DB seed -> loaded runtime: one preflight SHA-256 anchors the pre-deployed manifest through seeding and consistency; required pairs must match on presence, model-set ID, registry path, and available artifact identity/digests or startup fails
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime feature root -> sibling raw root: live bar refresh and feature-tail writes stay in one explicitly selected data tree
- runtime -> bridge state store: `patch_state`, `store_decisions`
- direct adaptive allocator -> canonical final entry risk -> committee/governor: adaptive selection can recover only scorer-owned probability rejections; freshness, venue, protection, governance, exposure, and direct-adaptive sleeve blockers remain binding, and only final-risk-approved entries reserve portfolio slots; no baseline twin is computed or persisted
- adaptive lifecycle -> monotonic hard lifecycle floor -> final lifecycle risk -> committee/governor: model/reversal probabilities feed the one adaptive producer, hard floors may only increase protection, and every actionable post-adaptive intent is reapproved before the committee can veto it
- MQ4 heartbeat -> bridge state -> live entry admission: every heartbeat emits `account_mode`, `account_scope`, and `account_magic`; the bridge resets the attested mode/scope before parsing so missing identity tokens cannot inherit prior entry authority, and the scope binds account number, server, and Magic without exposing the raw account number
- external evidence -> unsigned request -> external witness -> runtime ACK: `models release-request-export` emits exact portable claims and has no signing capability; `models canary-start --release-request ... --external-witness ...` imports an Ed25519 witness only after content-addressed evidence, eligible binding model components, semantic config, measured installed-package Merkle inventory, trust domains/principals, scopes, nonce, and identities agree. The booted runner repeats those checks before ACK and egress enablement
- runtime -> commands queue -> broker poll: all seven command verbs (`BUY`, `SELL`, `CLOSE`, `CLOSE_ALL`, `CLOSE_PARTIAL`, `MODIFY_SL`, `INFO`) carry the active generation/request/model/manifest/boot identity. Enqueue and poll atomically recheck release status, lease, scope, nonce/generation, broker identity, and command-specific entry/protective/emergency authority; revoked, superseded, stale, legacy, or unattested commands are quarantined instead of delivered
- live model set -> startup command admission: every configured pair must have an active explicit pair-allowlisted canary rollout with positive budget, and the live intent scope must include `enter` plus enabled protective lifecycle intents, or startup fails closed
- queue admission -> live entry evidence: attempts and raw submit calls remain diagnostic only; an accepted entry requires a newly queued command or a duplicate that resolves to an existing `queued` or `delivered` record
- runtime evidence -> canary advancement: entry evidence is stamped with its observation time and current stage index/percentage; a ramp clears it, and only a later matching-stage observation inside the configured alert window can authorize another ramp; ACK metrics use the command event's durable `ts`
- runtime/release/operator -> state store: each runtime cycle sends the live-authority snapshot it read, while release and operator mutations use a row-locked atomic authority patch. Every authority mutation advances `authority_revision`; safety mutations are dominant, and only an explicit canary start may re-enable killed authority. Transactional cycle patches preserve any newer kill, release scope, canary stage, bundle identity, audit fields, and stage-evidence reset instead of shallow-overwriting them with stale state.
- bridge state -> operator readiness: `entry_configuration_ready` reports static rollout admission, while `new_entry_ready` additionally requires effective authority enabled in `live` mode with a positive revision, live runtime/queue state, fresh signal transport, matching broker-mode/scope attestation, and no unresolved execution; authoritative empty scopes and zero values are never replaced by configuration fallbacks
- previous cycle -> capital governance -> entry admission: missing, stale, or schema-mismatched state pauses new entries for one fresh bootstrap cycle; stale persisted `shadow_policy` data is ignored and cannot pause trading or scale capital; protective lifecycle actions remain available
- sleeve tracker -> final entry admission: the current in-memory typed snapshot binds only direct-adaptive entries; `watch` remains a soft allocator penalty and `degraded` is a hard adaptive-entry veto

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
