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
- runner admission -> validate profile/mode/live arming and explicit scopes -> require binding structure-timing/chase, uncertainty, belief, campaign, and capital-governance producers -> read-only active-manifest preflight; no adaptive observation-twin toggle or baseline comparator is present in the production package
- bridge checks -> read the pre-boot state -> restore managed-position memory -> hydrate only the durable partial/exit command gap after the restart watermark -> patch boot state -> purge pending commands
- hash-anchored manifest seed -> required-pair seed gate -> model load -> runtime package/config/model attestation -> production operator-scope rollout -> live command admission -> production boot/scopes egress arm -> live feature refresh
- startup inference dry run -> pre-deployed manifest/model consistency -> readying state
- main loop -> per-pair scoring -> lifecycle -> submissions -> state patch

## Single Runtime Boundary

- Production launches only the installed `fxstack.runtime.runner` module under `python -I`. The bridge, feature-push worker, monitor, and preflights are sibling entrypoints from the same filtered distribution.
- No production process imports repository source, a repository CLI shim, training activation, mutable MLflow/registry code, causal research, or a repository worker helper.
- There is one model-loading and decision path. Alternate bundle loading is absent; `FXSTACK_AGENT_MODE=shadow` only changes execution authority for the same runner and deployed model set.
- Training, research, promotion, and activation finish on an external build/research host. Runtime receives only the pre-activated immutable manifest, registry provenance, and content-digested artifact payloads assembled into the deployment.

## Main Loop Phases

- refresh live bars from bridge ticks/bars
- read broker positions with their bridge-issued `positions_snapshot_token` and `positions_snapshot_received_at`; only a non-empty token different from the pre-boot or prior-cycle token is a newly observed broker snapshot
- reconcile pending partial and full-exit ledgers before new lifecycle decisions; an ACK is terminal broker truth, while lot reduction or position absence is accepted only from a newly received snapshot whose bridge receipt time is later than the command submission
- load latest feature rows per timeframe
- compute one versioned capital-governance snapshot from the latest complete cycle and current book before evaluating entries
- score the live signal and retain strict baseline-gate results as diagnostic evidence; probability, edge, regime, structure, uncertainty, spread, session, belief, and chase values do not independently authorize or veto an entry
- compute exit-model and reversal evidence plus explicit hard lifecycle floors; with direct adaptive execution, those model outputs are evidence rather than a parallel action producer
- apply direct adaptive policy ranking only when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`; that same control exclusively owns adaptive history and evaluator execution
- compare `enter` with `no_trade` in the production-owned adaptive policy using continuous model, setup, edge, execution-quality, uncertainty, and portfolio evidence; the winning margin continuously scales requested lots
- resolve one canonical post-intelligence entry intent, discard only operational-integrity blockers from the old gate reasons, rerun hard risk in allocator order, and reserve portfolio capacity only for an exact approved order
- refresh sleeve health after exit accounting; sleeve, cross-pair, campaign, and desk-overlay states remain visible evidence and allocator inputs rather than fixed entry vetoes
- pass the canonical risk-approved intent to committee/governor orchestration; playbook and execution specialists compare enter with an abstention benchmark by normalized utility, while hard operational/risk failures remain binding and a payload-less entry cannot be submitted
- reread production execution authority; any runtime boot, authority revision, pair/sleeve/intent scope, kill state, account, heartbeat, tick, or reconciliation drift blocks or quarantines the command
- submit exits first, entries second
- patch the exact admission-time governance snapshot to both top-level `governance` and runtime diagnostics, then persist decisions

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync; the registry preserves campaign, partial-close, and lifecycle memory while the broker position signature is unchanged and reseeds only when that signature changes
- every broker positions report receives a unique `positions_snapshot_token` and bridge-clock `positions_snapshot_received_at`; the runner seeds its last-seen token from pre-boot state, so a persisted snapshot is never mistaken for post-restart confirmation
- lifecycle models score exit / partial / reversal evidence on the enriched row
- when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`, `adaptive_lifecycle_decision` is the single strategy producer for hold/reduce/exit; no baseline lifecycle action is compared with or allowed to suppress it
- the monotonic `hard_lifecycle_*` floor is limited to the hard time-stop exit and a pipeline-failure stop adjustment that has proved it strictly tightens the existing broker stop; it can upgrade protection but never downgrade an adaptive reduce/exit
- after adaptive, campaign, and RL lifecycle routing has selected the final action, the runner materializes a partial close against current broker lots, lot step/minimum, cooldown, and partial-count caps; an otherwise sub-minimum residue becomes a full exit, and only that executable action reaches final lifecycle risk approval
- accepted `CLOSE_PARTIAL` and `CLOSE` queue writes create pending management records keyed by broker position signature and command ID. Queue acceptance does not increment the partial count and does not mutate recent-exit, campaign-close, campaign-transition, or sleeve-outcome state
- partial accounting commits only when the command row is `acked` or a newer broker snapshot proves the lot count fell. Full-exit accounting commits only when the command row is `acked` or a newer broker snapshot proves the submitted position signature is absent; campaign, recent-exit, and sleeve state advance together at that confirmation boundary
- a terminal non-success command is resolved without management credit only after it is known to be undelivered or a newer broker snapshot proves the position/lots stayed unchanged. Missing, stale, or same-token snapshots cannot manufacture success or failure
- managed-position persistence includes pending partial state, the pending/resolved exit-command ledger, recent exits, campaign state, and bounded sleeve trade history. On restart the runner restores that snapshot, then hydrates durable command rows only when `max(created_at, updated_at)` is newer than `max(managed_state.saved_at, runtime_last_cycle_ts)`, closing the enqueue-to-state-patch crash window without replaying already persisted outcomes
- every entry still carries broker-side SL/TP protection. With the Windows managed-runner setting at `4.0R`, the TP is a distant fail-safe while adaptive lifecycle partials and exits manage the normal outcome; the existing SL calculation is unchanged
- `FXSTACK_ADAPTIVE_SHADOW_ENABLED`, `FXSTACK_SHADOW_POLICY_ENABLED`, and the baseline shadow-ranking implementation are absent from production settings, startup, and telemetry
- live `enter` requires a fresh MQ4 heartbeat with a known `demo` or `real` broker account mode and a non-empty account scope; `contest`, `unknown`, missing, or stale attestation fails closed, while protective lifecycle authority remains independent of this entry-only gate
- production rollout gates only new `enter` actions. Protective `exit`, `reduce`, and `tighten_stop` do not depend on entry budget, but remain bound to the production runtime's explicit intent/pair scope and current boot; broker-wide flatten remains a production emergency action
- every command type is stamped at final cycle evaluation and rechecked at enqueue and MQ4 poll; no legacy, status, or protective command bypasses production execution authority
- twin/research findings may be imported and displayed as advisory evidence. They cannot arm egress, mutate production authority, or block a production decision; failure or absence of the twin therefore cannot stop the live agent

## Handshakes

- filtered installed package -> package preflight -> runner: dependency and physical-isolation checks complete before any production entrypoint is trusted
- direct installed runner -> startup admission: `Settings.validate_for_startup()`, launch-posture invariants, complete forbidden-module absence, and read-only active-model validation all complete before bridge checks or `RuntimeService` import/construction
- production package boundary -> startup admission: research/write-side modules and credentials are absent from the installed runtime, while advisory artifacts may cross the boundary as inert data; the singleton bridge consumer and dedicated poll/ACK token still protect the broker channel
- external activation -> immutable deployment handoff -> DB seed -> loaded runtime: one preflight SHA-256 anchors the pre-deployed manifest through seeding and consistency; required pairs must match on presence, model-set ID, registry path, and available artifact identity/digests or startup fails
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime feature root -> sibling raw root: live bar refresh and feature-tail writes stay in one explicitly selected data tree
- runtime -> bridge state store: `patch_state`, `store_decisions`
- direct adaptive intelligence -> allocator -> canonical final entry risk -> committee/governor: the strict scorer is diagnostic only; the adaptive policy selects enter versus abstain from the whole evidence vector and may override strategy-gate reasons, while continuous decision confidence scales requested lots. Missing/non-finite evidence, freshness, venue identity, broker protection, production authority, exposure, and hard risk remain binding, and only final-risk-approved entries reserve portfolio slots; no baseline twin is computed or persisted
- adaptive lifecycle -> monotonic hard lifecycle floor -> final action materialization -> final lifecycle risk -> committee/governor: model/reversal probabilities feed the one adaptive producer, position-signature-stable state survives loop refresh, hard floors may only increase protection, partial quantities are made broker-executable after the last producer, and every actionable intent is reapproved before the committee can veto it
- MQ4 positions report -> bridge receipt stamp -> runtime reconciliation: every legacy or JSON positions payload advances `positions_snapshot_token` and records `positions_snapshot_received_at`; only a token newly observed by the runner and received after the relevant command can prove lot reduction, position absence, or an unchanged broker position
- lifecycle command enqueue -> pending partial/exit ledgers -> command ACK or newer broker snapshot -> management-state commit: enqueue carries a versioned management context but cannot close a campaign, record a sleeve outcome, create recent-exit memory, or increment partial history; those mutations occur once at broker confirmation
- persisted managed state -> restart watermark -> durable command hydration: the runner restores ledgers and sleeve/campaign memory first, then scans only command rows changed after the later of the saved-state timestamp and last completed runtime-cycle timestamp
- MQ4 heartbeat -> bridge state -> live entry admission: every heartbeat emits `account_mode`, `account_scope`, and `account_magic`; the bridge resets the attested mode/scope before parsing so missing identity tokens cannot inherit prior entry authority, and the scope binds account number, server, and Magic without exposing the raw account number
- twin/research -> advisory artifact -> production telemetry: signed or unsigned research evidence can inform operators and future builds, but it is never read as a live permit or veto
- runtime -> commands queue -> broker poll: all seven command verbs (`BUY`, `SELL`, `CLOSE`, `CLOSE_ALL`, `CLOSE_PARTIAL`, `MODIFY_SL`, `INFO`) are bound to the current production boot, authority revision, and explicit scopes. Enqueue and poll atomically recheck those fields, the queue kill, broker identity, freshness, and command-specific intent; stale, out-of-scope, or unattested commands are quarantined instead of delivered
- live model set -> startup command admission: every configured pair must have an active explicit pair-allowlisted production rollout with positive budget, and the live intent scope must include `enter` plus enabled protective lifecycle intents, or startup fails closed
- queue admission -> live entry evidence: attempts and raw submit calls remain diagnostic only; an accepted entry requires a newly queued command or a duplicate that resolves to an existing `queued` or `delivered` record
- runtime evidence -> canary advancement: entry evidence is stamped with its observation time and current stage index/percentage; a ramp clears it, and only a later matching-stage observation inside the configured alert window can authorize another ramp; ACK metrics use the command event's durable `ts`
- runtime/operator -> state store: each runtime cycle sends the live-authority snapshot it read, while operator/runtime mutations use a row-locked atomic authority patch. Every authority mutation advances `authority_revision`; safety mutations are dominant, and only explicit production arming may re-enable killed authority. Transactional cycle patches preserve any newer kill, production scope, and audit fields instead of shallow-overwriting them with stale state. Release/twin fields remain advisory telemetry.
- bridge state -> operator readiness: `entry_configuration_ready` reports static rollout admission, while `new_entry_ready` additionally requires effective authority enabled in `live` mode with a positive revision, live runtime/queue state, fresh signal transport, matching broker-mode/scope attestation, and no unresolved execution; authoritative empty scopes and zero values are never replaced by configuration fallbacks
- previous cycle -> capital governance -> entry admission: missing, stale, or schema-mismatched state pauses new entries for one fresh bootstrap cycle; stale persisted `shadow_policy` data is ignored and cannot pause trading or scale capital; protective lifecycle actions remain available
- sleeve tracker -> final entry admission: the current in-memory typed snapshot binds only direct-adaptive entries; `watch` remains a soft allocator penalty and `degraded` is a hard adaptive-entry veto

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
