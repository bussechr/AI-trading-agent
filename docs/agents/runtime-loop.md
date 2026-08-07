# Runtime Loop

## Primary Files

- [package_preflight.py](../../fx-quant-stack/src/fxstack/runtime/package_preflight.py)
- [startup_preflight.py](../../fx-quant-stack/src/fxstack/runtime/startup_preflight.py)
- [model_manifest_preflight.py](../../fx-quant-stack/src/fxstack/runtime/model_manifest_preflight.py)
- [scalp_runtime_admission.py](../../fx-quant-stack/src/fxstack/runtime/scalp_runtime_admission.py)
- [scalp_engine_identity.py](../../fx-quant-stack/src/fxstack/runtime/scalp_engine_identity.py)
- [scalp_execution_authority.py](../../fx-quant-stack/src/fxstack/runtime/scalp_execution_authority.py)
- [scalp_runtime_control.py](../../fx-quant-stack/src/fxstack/runtime/scalp_runtime_control.py)
- [scalp_live_loop.py](../../fx-quant-stack/src/fxstack/runtime/scalp_live_loop.py)
- [scalp_execution_boundary.py](../../fx-quant-stack/src/fxstack/runtime/scalp_execution_boundary.py)
- [execution_ack_attestation.py](../../fx-quant-stack/src/fxstack/runtime/execution_ack_attestation.py)
- [scalp_validation_evidence.py](../../fx-quant-stack/src/fxstack/runtime/scalp_validation_evidence.py)
- [21_start_scalp_runtime.bat](../../ops/windows/21_start_scalp_runtime.bat)
- [release_contract.py](../../fx-quant-stack/src/fxstack/runtime/release_contract.py)
- [release_trust.py](../../fx-quant-stack/src/fxstack/runtime/release_trust.py)
- [release_authority.py](../../fx-quant-stack/src/fxstack/runtime/release_authority.py)
- [entry_protection.py](../../fx-quant-stack/src/fxstack/live/entry_protection.py)
- [runner.py](../../fx-quant-stack/src/fxstack/runtime/runner.py)
- [service_contract.py](../../fx-quant-stack/src/fxstack/runtime/service_contract.py)
- [service.py](../../fx-quant-stack/src/fxstack/runtime/service.py)
- [postgres_store.py](../../fx-quant-stack/src/fxstack/runtime/postgres_store.py)
- [broker_contract_state.py](../../fx-quant-stack/src/fxstack/runtime/broker_contract_state.py)

## Upstream
- [model-stack-and-feature-flow.md](model-stack-and-feature-flow.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Startup Phases

- installed-package admission -> validate settings/dependencies/database posture -> prove the complete forbidden training, activation, registry-write, research, replay, improvement, and RL-training module set is absent
- runner admission -> validate the strategy-specific profile/mode/arming posture, explicit scopes, expected broker account mode, and `FXSTACK_ENTRY_STRATEGY_FAMILY` before bridge or service access
- model-stack admission -> require binding structure-timing/chase, uncertainty, belief, campaign, and capital-governance producers -> read-only active-manifest preflight; no adaptive observation-twin toggle or baseline comparator is present in the production package
- production-scalp admission -> require scope v3's exact ordered 22-symbol IG MT4 universe, exact `scalp` sleeve, `enter,exit` intents, explicit live arming, configured expected account mode `demo` or `real`, and the hash-pinned exact-22 runtime-native cost capture. Demo and real use the same engine and gates. Legacy external release files remain compatibility inputs only when the runtime-native cost contract is absent.
- model-stack bridge bootstrap -> read the pre-boot state -> restore managed-position memory -> hydrate only the durable partial/exit command gap after the restart watermark -> patch boot state -> purge pending commands
- scalp bridge bootstrap -> bridge handshake/readiness checks -> disable prior-boot egress and revoke stale strategy authority -> patch the new boot identity -> purge queued entries while preserving queued exposure-reducing commands and quarantining stale delivered rows; exact broker-position ownership is reconstructed from authoritative ticket/Magic/comment plus durable command/ACK evidence during each cycle
- model-stack branch: hash-anchored manifest seed -> required-pair seed gate -> model load -> runtime package/config/model attestation -> production operator-scope rollout -> live command admission -> production boot/scopes egress arm -> live feature refresh
- MTVCLC runtime-native branch: startup derives one deterministic generation, exact cost rows, break-even probabilities, conservative probability floors, qualification cells, execution contract, and v3 authority identity from the installed engine plus the pinned broker capture. Its long-lived authority sentinel is the maximum signed-32-bit wire epoch, matching command DTO and MT4 provider transport. The live control plane persists that authority only after current broker mode/scope and normal live arming pass. Demo and real use this same engine and admission path.
- startup inference dry run -> pre-deployed manifest/model consistency -> readying state
- main loop -> per-pair scoring -> lifecycle -> submissions -> state patch

## Single Runtime Boundary

- Production launches only the installed `fxstack.runtime.runner` module under `python -I`. The bridge, feature-push worker, monitor, and preflights are sibling entrypoints from the same filtered distribution.
- Runner import keeps optional heavy branches cold: LangGraph, orchestration contracts and bridge helpers, Pydantic, and OpenTelemetry load only when an enabled agent-mode cycle needs them; directional-belief, cross-pair, RL-proposal, feature-serving, multi-timeframe feature, Parquet storage, provider-registry, market-data, feature-push, adaptive-policy, allocator, campaign, sleeve-governance, desk-overlay, generic portfolio, generic risk contracts/kernel/sizing/envelope, managed-position, and model-scorer code loads only on its first real operation. Shared playbook/campaign identities and rollout-execution identities live in dependency-free strategy and risk constants modules, so orchestration and the MTVCLC branch do not hydrate model-stack implementations merely to normalize a sleeve or decide rollout admission. `FinalEntryApproval` and the external service protocol live in the lightweight `runtime.service_contract`; the concrete service re-exports the same approval class for compatibility, while first live submission no longer imports PostgreSQL, settings, API schemas, or provider adapters just to construct the proof. The execution protocol resolves and caches only the selected MT4, paper, OANDA, IBKR, or MT5 serializer on its first command; importing the protocol hydrates none of those adapters, and the execution package initializer exposes its MT4/paper compatibility names lazily without importing a sibling adapter selected by another venue. Scalp-authority validation stays lazy for model-stack approvals that carry no scalp authority. XGBoost classes load only at their validated artifact or inference boundaries; feature-push utilities load the PostgreSQL store only for a store operation; and pandas/NumPy hydrate only for their first dataframe or numerical operation. The validated settings stack loads at startup admission, dependency-free bridge protocol identity loads for runtime handshake validation, Pydantic API wire schemas load only for API model handling, `urllib.request` loads for an actual bridge handshake or capability probe, file locking loads for the first artifact/parquet lock, and release evidence/physical-trust verifiers load only when authority validation actually requests them. Alternate Binance providers load only when selected. Disabled and MTVCLC-only branches do not pay those import costs.
- The dependency-light scalp execution-authority contract owns its two stable
  admission-mode wire literals directly. Importing it through the PostgreSQL
  store or concrete service does not hydrate signed validation evidence or the
  runtime-release verifier; the verifier class loads only when a runtime-release
  result is projected into a new scalp authority.
- Importing the PostgreSQL store or concrete service also keeps the validated
  settings stack cold. Settings load only when a concrete store is constructed
  or a later settings-backed operation needs them; import-only protocol and
  composition consumers do not pay the Pydantic settings cost.
- The feature-push worker claims each bounded oldest-first outbox batch with one
  atomic update-returning statement. PostgreSQL candidate selection uses
  `FOR UPDATE SKIP LOCKED`; stale-claim recovery, fresh-claim exclusion,
  ownership stamping, and attempt increments occur in that same statement.
- After production authority activation, each model-stack runtime cycle reads
  state and governance metrics through one transactional store snapshot rather
  than issuing two independent state reads plus a later metrics read. The first
  activation attempt retains a separate pre-mutation state read, then refreshes
  the combined snapshot before evaluating governance.
- Each production-scalp cycle likewise reads its pre-activation state and
  exact capital-governance metric subset through one coherent store snapshot
  and passes the captured feature-parity breach count into capital governance.
  Dashboard-wide command, decision, model, outbox, and audit aggregates stay
  outside this one-second hot path. A successful authority mutation still
  receives its required post-mutation state refresh. The final state write
  merges only compact operational diagnostics under the store's row lock and
  removes any stale embedded production-scalp cycle; the full cycle remains in
  the append-only decision snapshot, avoiding a redundant pre-write read and a
  duplicate large JSON document while preserving concurrent diagnostics.
- No production process imports repository source, a repository CLI shim, training activation, mutable MLflow/registry code, causal research, or a repository worker helper.
- The retired demo execution probe has no importable runtime module, DTO, candidate builder, command-identity helper, or engine component. Its three historical environment aliases remain parse-only in `Settings` solely to turn stale deployment input into an explicit startup refusal rather than silently ignoring it.
- There is one installed runner and one queue/API/MT4 execution boundary. `FXSTACK_ENTRY_STRATEGY_FAMILY` selects either the pre-deployed model stack or the installed `mtvclc` strategy branch; it never selects an external process, research package, alternate bridge, or alternate queue.
- The production scalp engine identity schema is v3. It hashes both production proposal profiles and adapters, qualification, quote refresh, restart/lifecycle, broker-contract, live-loop, control-plane, signed validation/revocation verification, legacy authority-schema handling, and the last-mile API/provider/risk/store/service/startup plus MQL4 `BridgeEA`/`BridgeHttp`/`BridgeUtils` sources. Changing a measured source changes the installed engine identity and therefore breaks the exact v5 sealed-engine/outer-v2 runtime-release match before another entry can be authorized; authority-free evidence-v3 remains a separately authenticated historical result rather than the whole-engine witness.
- The production loop consumes the `mtvclc.v1` profile only with exact ordered scope-v3 bar/quote/cost mappings, one authenticated MT4 producer, direct bid M1/iVolume rows, and current bid/ask snapshots. Each pair uses its last 241 strictly ordered observed bars; legitimate no-tick minute gaps are allowed and duplicate/reordered bars are refused. A tick-built M1 bar is evaluated when the first post-close broker tick makes it observable, even when that tick arrives more than five wall-clock seconds after the nominal minute boundary. Runtime-native costs bind qualification and bracket economics in both demo and real modes. Candidates remain immediate market orders with pending orders forbidden; current-quote cost proof, broker-grid proof, risk approval, v3 authority, queue, and MT4 checks remain binding.
- The broker-grid planner has no implicit or legacy spread-only economics. Every build requires the exact `fxstack.production_scalp_cost.mtvclc.v1` model with a positive frozen p90 spread ceiling, and enqueue/poll revalidation refuses a missing, renamed, spread-only, or self-consistent-but-arbitrary cost-model identity.
- `ops/windows/21_start_scalp_runtime.bat` pins the scalp family, exact ordered 22-symbol universe, scalp enter/exit scopes, shared runtime cost contract, and one-second cadence. Mutating launch requires live/live/armed and shadow-off posture; the canonical launcher captures that wrapper's runtime-native engine/config/cost/account binding, permits cold start only from an authenticated inert bridge with disabled egress, revalidates the binding after replacement, and waits for fresh complete MT4 broker attestation before runtime spawn. No one-shot trade mode exists.
- `ops/windows/21_start_runtime.bat` validates `FXSTACK_RUNTIME_LOOP_SLEEP_SECS` as an integer from 1 through 60 before launch mutation and forwards it to every runner path. The shared default is 10 seconds; latency-sensitive strategy profiles must deliberately pin a tighter cadence rather than inheriting that default.
- Training, research, promotion, and activation finish on an external build/research host. Runtime receives only the pre-activated immutable manifest, registry provenance, and content-digested artifact payloads assembled into the deployment.

## Main Loop Phases

- refresh live bars from bridge ticks/bars
- once the exact per-symbol M1 history is warm, retain its validated ordered
  epochs beside the runtime-owned rows. The bounded two-row boundary merge then
  parses every external epoch but compares exact built-in rows against the
  retained private row before projection or copying. Stable tails therefore
  keep their existing immutable batches; only inserted or content-changed rows
  are copied, matching epochs are replaced by binary search, and the retained
  window is trimmed without reparsing or reindexing all 5,302 stable rows every
  second. Duplicate incoming epochs retain last-row-wins behavior. Mapping
  subclasses and ordinary caller-supplied dictionaries retain the complete
  copy/compatibility path
- authenticate every bar and quote against the current singleton terminal
  identity. The canonical identity digest and immutable strategy-identity
  validity use small bounded value caches; exact canonical dictionaries project
  their ordered scalar identity through one C-level map/tuple comparison, while
  changed, non-canonical, malformed, unhashable, or custom mapping inputs retain
  the complete reconstruct-and-hash
  validation path. The cache never substitutes for comparing each row, and a
  changed identity projection receives a distinct key and fails closed
- canonical adapter rows take exact built-in fast paths for finite floats,
  ordinary positive integer epochs, and empty list/tuple quality flags. Huge
  integers, strings, container subclasses, and malformed values retain the full
  conversion and validation path. Each OHLC value is converted once and reused
  for positivity, geometry, and typed-bar construction, avoiding temporary
  tuple and generator walks without weakening fail-closed row validation.
  A bounded exact-content projection cache reuses only frozen typed bars across
  one-second cycles; every validation-relevant field, symbol, minute, and
  authenticated source identity participates in the key. Runtime-owned warm
  history rows are immutable and retain their private projection identity, so
  stable rows do not rebuild or deeply hash the same field tuple every second.
  Ordinary mutable rows remain keyed by freshly projected exact content;
  mutations receive a distinct key, while custom or unhashable inputs bypass
  the cache. Equal
  immutable authenticated-source projections are interned with a precomputed
  structural hash, so the 5,302 recurring bar-cache lookups do not re-hash or
  deeply compare the same nested identity on every cycle; any source or broker
  identity change produces a distinct projection
- completed bar preparation is reused within one minute only when every input
  row is the private immutable runtime form. Its bounded key contains the
  ordered row-projection identities, symbol, authenticated source, current
  minute, and common closed minute. Runtime-owned history publishes those rows
  through an immutable batch whose ordered projection key and structural hash
  are computed only when the history changes; stable cycles therefore avoid
  rebuilding or rehashing all 5,302 row identities. Any row replacement,
  reorder, minute, or source change performs full preparation again. Ordinary
  sequences of private rows still rescan their ordered identities on every
  call, while ordinary mutable rows bypass prepared-result reuse entirely;
  future-receipt refusals are never retained, and a backward evaluation clock
  cannot reuse a result prepared at a later instant
- the pure MTVCLC evaluator caches exact immutable-bar validation, type-7 V90,
  direction, body, and close-location results by the complete 241-bar tuple,
  symbol, and recorded cost. Runtime-owned prepared tuples precompute their
  structural hash once, so repeated one-second cycles avoid both deep tuple
  hashing and rescanning every bar. Any bar or cost change receives a distinct
  key; the once-per-minute cold request performs the complete calculation.
  Raw quote transport freshness, provenance, prices, event identity, timing,
  and ordering remain live on every adapter call. The canonical one-dictionary
  quote path projects that row directly without allocating the general
  multi-row collection/sort intermediates; sequences retain the complete
  ordering path. A successful measured adapter handoff seals the resulting
  immutable quote tuple to its exact symbol and
  authenticated source, allowing the evaluator to skip only the duplicate
  typed-contract scan. Source drift and ordinary externally constructed quote
  tuples retain the complete validator. Contemporaneous-quote selection,
  spread, entry timing, and bracket geometry still run on every evaluation;
  malformed unhashable fields retain the complete fail-closed path
- immutable MTVCLC policy, authenticated-source, frozen-cost SHA-256, and
  frozen-cost structural validity are memoized by exact frozen dataclass value,
  with an uncached compatibility path for malformed unhashable inputs. The
  exact-dict bar adapter reads the primary epoch once, bypasses abstract mapping
  dispatch, and derives latest finalized time from the already sorted validated
  epoch set
- read broker positions with their bridge-issued `positions_snapshot_token` and `positions_snapshot_received_at`; only a non-empty token different from the pre-boot or prior-cycle token is a newly observed broker snapshot
- the store scopes each restart-reconciliation read to active, unresolved, and
  unknown-status rows; when broker positions exist it also includes every
  historical entry/management verb at every status so contradictory evidence
  remains fail-closed. The query projects only command ID, verb, symbol, Magic,
  intent, status, payload, and ACK—the eight fields the pure reconciler
  consumes—instead of decoding the complete generic command row. The pure
  reconciler then rejects generic rows before allocating or validating scalp
  command projections. Relevant rows retain the complete contract and
  canonical output ordering; input order no longer triggers a redundant full
  command sort
- reconcile pending partial and full-exit ledgers before new lifecycle decisions; only an exact broker-attested ACK is terminal success, while lot reduction or position absence is accepted only from a newly received snapshot whose bridge receipt time is later than the command submission
- load latest feature rows per timeframe
- realized-correlation preparation converts and validates each bounded pair
  return series once, then freezes the map for the runtime cycle; repeated
  portfolio evaluations bypass whole-map scans and reuse clean finite, unique,
  monotonic series without replacement/cast copies. Pair alignment, sample
  counts, latest observations, and winsorized correlations retain one symmetric
  configured-window/observation-floor result per symbol pair inside that cycle;
  each candidate/active-set/configuration result also retains one private
  isolated snapshot template and its exact oldest contributing timestamp.
  Reuse clones the template and derives freshness from the current process wall
  clock without rebuilding pandas time objects or correlation maps.
  Heuristic-only evaluation bypasses realized-return preparation entirely, and
  arbitrary external inputs still receive full normalization without cross-call
  caching
- compute one versioned capital-governance snapshot from the latest complete cycle and current book before evaluating entries
- portfolio allocation serializes each concentration, correlation, budget, and
  stress snapshot once without recursive dataclass deep copies. Allocator-owned
  snapshots already normalized by their producers use the flat cached-schema
  path, while the public telemetry builder retains full finite-number and
  container-subclass normalization for arbitrary external snapshots. The
  trusted path reads reporting aliases directly from those producer-normalized
  snapshots instead of reparsing the same finite scalars from their serialized
  maps; pre-entry and cycle diagnostics reuse that allocation-owned telemetry
  payload. Repeated
  book reconstruction reuses private static instrument identities and takes a
  direct JSON-safety path for built-in primitive metadata values. The runner
  contract-annotates and normalizes the invariant open-position base once per
  cycle; later candidate, reapproval, and cycle-summary allocations compose
  only their changing pending-entry reservations onto that prepared base. A
  reservation-free allocation also reuses the base's once-normalized book
  payload, active-symbol set, concentration, stress results, and normalized
  book/concentration/stress telemetry templates. The runner's private
  read-only allocation posture references those prepared concentration,
  stress, and budget snapshots without cloning them again; public allocation
  calls retain isolated mutable snapshots. Prepared heuristic-correlation and
  valid budget caches retain their normalized shallow telemetry templates, so
  repeated decisions copy mutable fields without rescanning the dataclass
  schema. The private read-only runtime path keys those caches directly from
  exact producer-normalized scalars; arbitrary or malformed public inputs keep
  the complete conversion and validation path. Realized correlation keeps its
  freshness-aware cache contract.
  Allocation decisions retain
  only the canonical snapshots and telemetry; serializers materialize isolated
  top-level snapshot views on demand, so the compact runtime form builds only
  its budget view instead of eagerly retaining four duplicate maps, and removes
  the same budget from nested telemetry rather than emitting it twice. Valid
  candidate-specific allocator budgets are keyed by every scalar input they
  consume and reused as isolated clones; malformed numeric contracts and
  changing reservations always recompute. Callers receive deep-isolated payload
  copies, so their mutations cannot contaminate later decisions in the same
  cycle
- canonical session aliases and session-family labels use bounded scalar-string
  caches across book and budget evaluation; null, NaN, array-like, and other
  arbitrary inputs retain the uncached contract-validation path, and policy
  imports remain lazy until the first normalization request. Allocator
  concentration scans keep deterministic key ordering without copying the
  already normalized exposure maps before each scan
- each risk call materializes one JSON-safe portfolio-allocation payload and
  reuses its book, concentration, correlation, stress, and telemetry sections;
  allocation serialization reuses the already normalized snapshot payloads
  owned by telemetry. Book and telemetry serialization share one cached
  dataclass-schema extractor and exact built-in type dispatch, scanning only
  mutable or non-built-in values for JSON normalization rather than recursively
  revisiting every primitive field.
  The runtime form keeps the budget plus canonical telemetry, whose exposure
  aggregates are also the risk kernel's input; it omits the duplicate book,
  raw position rows, and top-level concentration/correlation/stress snapshots.
  The public full serializer remains available to callers that explicitly need
  those records
- risk policy, market, portfolio, rule-trace, approved-order, and final-decision
  contracts use the same package-level cached dataclass schema and JSON
  normalizer. Final decisions compose their already serialized children once;
  they do not recursively deep-copy the full decision and then serialize every
  child a second time. The runtime emits verdict, reason, lifecycle values,
  rollout, trace, and approved order only through their canonical surrounding
  fields instead of also retaining a nested serialized decision; the full
  public decision serializer remains available for replay. Canonical kernel
  decisions privately mark their owned, normalized rule-trace details as
  trusted, allowing the decision to isolate the complete trace as one shallow
  batch without per-rule compatibility dispatch. The kernel-only default
  envelope preserves that marker; configuring any
  post-rule clears it because the rule may replace or mutate traces. Public and
  externally constructed plug-in traces always retain the full recursive,
  validating serializer, and the trust marker is never exposed in the public
  decision payload. The final-sizing trace retains only its unique sizing
  source, refusal, numeric-error, sensible-cap, and post-builder evidence rather
  than copying the canonical command, lot, rollout, or broker sizing payloads.
  Trace-detail and approved-command serializers normalize atomic
  top-level mapping values inline, recursing only for nested or uncommon
  values; arbitrary numeric types and container subclasses retain the complete
  validating path.
  Approved entries normalize their shared order metadata once and emit isolated
  compact-decision plus broker-command views from that one normalized source.
  The broker view reuses the decision order's already normalized scalar fields
  instead of recursively validating an equivalent command base a second time.
  Concentration, correlation, and stress diagnostics remain in the canonical
  portfolio-allocation payload rather than being duplicated into policy
  metadata, approved orders, and broker commands; lossless public serializers
  remain available for replay. Entry evaluation
  computes one validated budget plan and reuses it for rollout/exposure
  diagnostics plus final order construction instead of repeating sizing and
  numeric-contract checks. Entry-budget and approved-order validation convert
  each raw numeric field once, reuse that finite value for range and sizing
  checks, and preserve explicit zero contract values so invalid broker geometry
  cannot fall through to a default contract
- heuristic correlation memoizes symmetric instrument-overlap relationships in
  a bounded process cache, and flat concentration/correlation/budget/stress
  snapshots serialize their cached schemas in one pass instead of constructing
  an intermediate dataclass mapping. Built-in immutable scalar fields bypass
  container classification, exact maps/lists take direct shallow copies, and
  uncommon container subclasses retain the normalizing compatibility path;
  recursive dataclass deep-copy machinery remains absent
- score the live signal and retain strict baseline-gate results as diagnostic evidence; probability, edge, regime, structure, uncertainty, spread, session, belief, and chase values do not independently authorize or veto an entry
- scorer-side feature projection reuses bounded exact column-position plans,
  avoids copying unrequested wide-row columns, skips adaptive meta enrichment
  unless the loaded artifact declares those features, and computes shared
  structure/session evidence once per pair score
- compute exit-model and reversal evidence plus explicit hard lifecycle floors; with direct adaptive execution, those model outputs are evidence rather than a parallel action producer
- apply direct adaptive policy ranking only when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`; that same control exclusively owns adaptive history and evaluator execution
- compare `enter` with `no_trade` in the production-owned adaptive policy using continuous model, setup, edge, execution-quality, uncertainty, and portfolio evidence; the winning margin continuously scales requested lots
- resolve one canonical post-intelligence entry intent, discard only operational-integrity blockers from the old gate reasons, rerun hard risk in allocator order, and reserve portfolio capacity only for an exact approved execution plan
- refresh sleeve health after exit accounting; sleeve, cross-pair, campaign, and desk-overlay states remain visible evidence and allocator inputs rather than fixed entry vetoes
- pass the canonical risk-approved intent to committee/governor orchestration; playbook and execution specialists compare enter with an abstention benchmark by normalized utility, while hard operational/risk failures remain binding and a payload-less entry cannot be submitted. In live mode the runner requires the service's in-process `submit_approved_command` boundary; a missing or incompatible service method is an explicit zero-submission refusal and never falls back to public `submit_command`
- reread production execution authority; any runtime boot, authority revision, pair/sleeve/intent scope, kill state, account, heartbeat, tick, or reconciliation drift blocks or quarantines the command
- submit exits first, entries second
- patch the exact admission-time governance snapshot to both top-level `governance` and runtime diagnostics, then persist decisions

### Production Scalp Branch

- obtain strategy ticks for exactly the 22 configured symbols, completed causal M1 bars, broker contracts, account/venue attestation, authoritative positions snapshot, and durable command history through the same bridge and `RuntimeService`; the broker may additionally publish direct/inverse account-currency conversion crosses as market-data-only inputs, but those extras never enter the strategy or execution scope
- broker-contract projection revalidates account, venue, margin, timestamp
  freshness, authenticated source, exact scope, and every row on each boundary
  call. Exact built-in dictionary rows project all sizing-consumed scalars into
  a bounded content-keyed cache of frozen `BrokerContractSpec` values; a
  same-timestamp field mutation receives a distinct key. Mapping subclasses,
  uncommon values, and oversized integers retain full uncached conversion and
  validation
- account-conversion projection scopes live tick mappings by reference only for
  the duration of the pure scalar projection; no raw row survives the call.
  Broker-contract and conversion aggregate results split into strict
  per-symbol frozen contracts through direct constructors rather than generic
  dataclass replacement, while each symbol retains independent mutable
  rates/coverage maps at the private runtime boundary
- project one ordered readiness result for every member of the exact 22-symbol scope. At least one ready pair is sufficient for scalp entry readiness and only that pair may advance; the all-pairs-ready aggregate is health telemetry, so a closed or stale market on another symbol cannot suppress a qualified ready pair. Missing/reordered symbols or inconsistent any/all aggregates fail closed
- build the scalp hot-path health view from the authenticated heartbeat state plus the exact tick-scope result; do not call the operations-grade `/v2/ready` report before market evaluation. The bridge keeps one broker M1 chart materialized per configured symbol. Warm the exact ordered 22-symbol 241-row completed-M1 baseline once, then fetch and merge the two-row exact-scope tail every runtime cycle. Each pair becomes eligible as soon as its exact direct shift-1 bar is first observable; no all-pairs barrier and no wall-clock T+5 bar-arrival deadline exists. The first authenticated post-close quote defines the immediate entry epoch, after which the bounded five-second command-delivery deadline remains binding.
- refresh runtime-native admission every cycle: rebind the installed engine/config, venue, account mode, exact scope, daily cap, exact cost rows, and all 44 side-by-symbol probability cells. The 64 measured engine components are reread and content-hashed through a reused bounded two-worker pool. A bounded lexical-only plan avoids reconstructing invariant `Path` objects, while four balanced worker batches still resolve every unique component parent and reread, normalize, and hash every source file on every cycle; bytes, resolved filesystem identities, and digests are never cached. The pinned capture is likewise reread and hashed before reuse of a bounded content-keyed derived-surface template. A contract-specific clone copies only the template's mutable maps while preserving its shared-map topology, so broker mode drift or cost/engine identity drift still revokes new entries in the same cycle without generic recursive-copy cost or cross-cycle mutation.
- retain the pure evaluator's causal per-symbol activity ratio, signal direction, bar body/close location, spread, recorded cost, and bracket economics in batch/decision diagnostics even when the candidate abstains. Missing later-stage values remain null rather than being invented; this telemetry has no qualification, release, sizing, queue, or broker authority
- symbol preparation retains a compact ordered diagnostic base and constructs
  each frozen public symbol diagnostic exactly once after evaluation. The
  import-time field-order guard refuses contract drift; its full and recurring
  sparse serializers reuse the guarded required/optional name plans instead of
  re-introspecting the dataclass schema for every symbol. Ready symbols no
  longer allocate a default diagnostic and then walk/reconstruct every field
  through `dataclasses.replace`
- join restart state only by authoritative ticket/Magic/comment plus the exact durable BUY/SELL command primary key and its full entry-time admission mode, account mode, generation, runtime boot, admission identity, engine/config, venue/scope, and owner binding; require either a successful matching ACK or a fresh authoritative position snapshot, and quarantine ambiguous or unowned positions
- serialize immutable restart, lifecycle, and rollover diagnostics through cached flat field projections plus explicit nested-contract serialization. The wire payload remains exactly equal to recursive dataclass serialization without its repeated deep-copy walk or temporary allocation cost
- serialize the one-second MTVCLC proposal, qualification, refreshed-quote,
  quote-diagnostic, capacity, and broker-plan contracts through guarded fixed
  field plans and cached flat projections, explicitly materializing nested
  immutable contracts only at their public wire boundary. Exact typed capacity
  diagnostics also bypass generic serializer dispatch and its redundant map
  copy. Broker command and diagnostic payloads retain their exact shapes and
  independent mutation isolation without recursively deep-copying every scalar
  field
- persist only the versioned admission status and content-addressed release/engine/config/scope/expiry bindings in each recurring cycle diagnostic. Exact-22 per-symbol cost/hash surfaces, measured component inventories, and local file paths remain available in the full startup admission snapshot rather than being copied into every second's decision row. Authority activation serializers retain their complete public shape while copying only the nested mutable payloads that require isolation
- persist complete account-currency conversion rates and path evidence once at the cycle root. Each of the 22 ordered symbol-readiness rows carries only its quote-currency reference, local conversion refusal set, and readiness boolean; it never embeds another copy of the same six quote-currency coverage cells
- retain full proposal and capacity evidence once in each symbol decision's metadata. The cycle-wide proposal and capacity sections contain only batch totals, scope/source identity, selected symbols, and aggregate counts; they do not repeat the same 44 detailed symbol records or selected proposal already stored in the decision list. `/v2/state` and replay retain the detailed evidence, while `/v2/ready` reads the bounded summaries it actually needs
- make per-symbol decision metadata sparse only for absent optional stages and absent evaluation measurements. Populated proposal, qualification, quote, broker-plan, risk, enqueue, refusal, and near-signal values remain explicit; abstentions do not allocate or serialize empty stage maps or null evaluation fields, and dashboard normalization treats those optional maps as absent-by-default
- expose the funding boundary as diagnostics only. It no longer creates a wall-clock entry blackout or forced close. Exact finalized bars, current spread/cost, signal rules, broker availability, risk, and the 30-M1-bar time stop remain binding; no-tick minutes are never synthesized.
- evaluate confirmed time-stop exits first, then sample post-fetch clocks before broker-contract/snapshot validation, qualification, current-quote refresh, and broker-plan construction. Never reuse the pre-request cycle-start timestamp for quote, contract, or snapshot age: the bridge may stamp any of them while HTTP or store work is in flight, and newly received broker truth must not be mislabeled as future data. Refresh an entry candidate against the current venue quote and recheck spread, break-even probability, the matching signed certified-cell lower bound, and expected value; there is no local probability assumption or qualification bypass
- build an immutable `fxstack.production_scalp_broker_entry_plan.v2` for the selected pair before risk. It binds `execution_type=market`, `pending_orders_forbidden=true`, and the proposal's positive integer `entry_deadline_epoch` alongside the logical/broker symbol and complete current contract geometry, places quote/worst-fill/SL/TP on the broker tick grid, fixes `max_slippage_points=20`, and recomputes risk/payoff from the adverse permitted fill. The broker protection floor is directional against current bid/ask—BUY SL below bid and TP above ask; SELL SL above ask and TP below bid—and adds a fixed five-point cushion beyond `stop_level_points`
- plan deterministic remaining capacity across open positions and durable queued immediate-trade reservations, then pass only qualified immediate-market candidates through the canonical broker-contract risk kernel and `FinalEntryApproval`. The scalp risk call uses the signed per-symbol p90 spread calibration already enforced by proposal, quote refresh, and broker-plan positive-edge checks; the generic model-stack 3 bps ceiling cannot contradict that instrument-specific cost authority. Scalp lot size is the largest broker-step-valid value allowed by stop-distance cash risk (`<=0.5%` of current equity), broker maximum, and available margin. Generic cross-instrument gross/net lot ceilings are non-binding in this lane; one position per symbol, six total positions, and the `5%` drawdown stop remain binding. Qualification and plan construction refuse at or after the five-second deadline measured from the first post-close observed quote; command lifetime is server-owned as `min(configured TTL, entry_deadline_epoch - server_now)`, and transactional enqueue/poll quarantines anything that reaches that deadline. The MT4 entry boundary executes only an immediate BUY at ask or SELL at bid. Internally, MT4 names that API `OrderSend(..., OP_BUY, ...)` or `OrderSend(..., OP_SELL, ...)`; pending limit/stop opcodes are not reachable in the production-scalper lane
- restart reconciliation rejects unrelated durable queue rows through direct ownership-marker branches before allocating prepared command records. Ordinary database dictionaries bypass abstract mapping dispatch, and the already validated payload is passed into preparation rather than classified twice; mapping subclasses retain the complete compatibility path. The generic queue may contain thousands of rows from other lanes, so relevance scanning also avoids generator and temporary-tuple allocation while preserving entry intent, management strategy, strategy lane, and historical strategy-binding markers
- stamp entries with the current admission-mode/account-mode-bound scalp authority and exact `production_scalper` owner identity; stamp every exit with `managed_entry_command_id` plus the exact target ticket/Magic/comment ownership tuple so later generations and boots can verify the immutable entry-time authority; submit exits before entries
- There is no separate scalp shadow runtime or watchdog. Native broker SL/TP and exact-owner protective lifecycle remain separate from new-entry authority.

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync; the registry preserves campaign, partial-close, and lifecycle memory while the broker position signature is unchanged and reseeds only when that signature changes
- every broker positions report receives a unique `positions_snapshot_token` and bridge-clock `positions_snapshot_received_at`; the runner seeds its last-seen token from pre-boot state, so a persisted snapshot is never mistaken for post-restart confirmation
- lifecycle models score exit / partial / reversal evidence on the enriched row
- when `FXSTACK_ADAPTIVE_EXECUTION_ENABLED=1`, `adaptive_lifecycle_decision` is the single strategy producer for hold/reduce/exit; no baseline lifecycle action is compared with or allowed to suppress it
- the monotonic `hard_lifecycle_*` floor is limited to the hard time-stop exit and a pipeline-failure stop adjustment that has proved it strictly tightens the existing broker stop; it can upgrade protection but never downgrade an adaptive reduce/exit
- after adaptive, campaign, and RL lifecycle routing has selected the final action, the runner materializes a partial close against current broker lots, lot step/minimum, cooldown, and partial-count caps; an otherwise sub-minimum residue becomes a full exit, and only that executable action reaches final lifecycle risk approval
- accepted `CLOSE_PARTIAL` and `CLOSE` queue writes create pending management records keyed by broker position signature and command ID. Queue acceptance does not increment the partial count and does not mutate recent-exit, campaign-close, campaign-transition, or sleeve-outcome state
- partial accounting commits only when the command row is broker-attested `acked` or a newer broker snapshot proves the lot count fell. Full-exit accounting commits only when the command row is broker-attested `acked` or a newer broker snapshot proves the submitted position signature is absent; campaign, recent-exit, and sleeve state advance together at that confirmation boundary
- a terminal non-success command is resolved without management credit only after it is known to be undelivered or a newer broker snapshot proves the position/lots stayed unchanged. Missing, stale, or same-token snapshots cannot manufacture success or failure
- managed-position persistence includes pending partial state, the pending/resolved exit-command ledger, recent exits, campaign state, and bounded sleeve trade history. On restart the runner restores that snapshot, then hydrates durable command rows only when `max(created_at, updated_at)` is newer than `max(managed_state.saved_at, runtime_last_cycle_ts)`, closing the enqueue-to-state-patch crash window without replaying already persisted outcomes
- scalp boot, disabled-poll, and activation-failure quarantine preserve already queued exposure-reducing commands. A reducer that expired without ever being delivered may be requeued only when a retry reuses the command identity and matches the complete immutable business payload field-for-field; payload drift or any prior delivery remains refused
- every entry still carries broker-side SL/TP protection. One side-effect-free `live.entry_protection` calculator owns the geometry used by both the production runner and offline research. The Windows baseline disables the former forced `4.0R` override (`FXSTACK_MANAGED_RUNNER_TP_R_MULTIPLE=0`): generic geometry uses the measured `3.0*ATR` stop and `max(1.5*ATR, 0.5R)` target, while strategy-specific signed authority may bind a stricter bracket. Risk-based sizing reduces lots as the stop widens, and adaptive lifecycle partials/exits remain subject to final risk approval
- `FXSTACK_ADAPTIVE_SHADOW_ENABLED`, `FXSTACK_SHADOW_POLICY_ENABLED`, and the baseline shadow-ranking implementation are absent from production settings, startup, and telemetry
- MTVCLC live `enter` requires a fresh MQ4 heartbeat with broker mode `demo` or `real`, a non-empty account scope, and an exact match to the configured expected mode and v3 authority. `contest`, `unknown`, missing, stale, or changed attestation fails closed.
- normal production rollout gates only new `enter` actions. Its protective `exit`, `reduce`, and `tighten_stop` do not depend on entry budget, but remain bound to the production runtime's explicit intent/pair scope and current boot; broker-wide flatten remains a production emergency action. The invalid-signed-evidence scalp protective-management plane is intentionally narrower and authorizes only exact-owner full `CLOSE`
- every command type is stamped at final cycle evaluation and rechecked at enqueue and MQ4 poll; no legacy, status, or protective command bypasses production execution authority
- twin/research findings may be imported and displayed as advisory evidence. They cannot arm egress, mutate production authority, or block a production decision; failure or absence of the twin therefore cannot stop the live agent

## Handshakes

- filtered installed package -> package preflight -> runner: dependency and physical-isolation checks complete before any production entrypoint is trusted
- direct installed runner -> startup admission: `Settings.validate_for_startup()`, strategy-specific launch posture, explicit live arming, and forbidden-module absence finish before bridge checks or service construction. Model families perform active-model validation; MTVCLC verifies the runtime-native installed-engine and pinned exact-22 cost contract for the configured demo/real mode.
- production package boundary -> startup admission: research/write-side modules and credentials are absent from the installed runtime, while advisory artifacts may cross the boundary as inert data; the singleton bridge consumer and dedicated poll/ACK token still protect the broker channel
- external activation -> immutable deployment handoff -> DB seed -> loaded runtime: one preflight SHA-256 anchors the pre-deployed manifest through seeding and consistency; required pairs must match on presence, model-set ID, registry path, and available artifact identity/digests or startup fails
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime feature root -> sibling raw root: live bar refresh and feature-tail writes stay in one explicitly selected data tree
- runtime -> bridge persistence: `patch_state` keeps authority, recovery, and
  operational state in the row-locked runtime snapshot; `store_decisions`
  appends bulky decision telemetry to `decision_snapshots`. The production
  scalp cycle commits its compact state patch and decision snapshot in one
  transaction with one shared timestamp. The bridge joins the complete latest
  decision snapshot for `/v2/state`, while `/v2/ready` reads state, metrics,
  and only the latest timestamp plus diagnostic column in one transaction;
  state and diagnostic are scalar projections of the same summary statement.
  Scalp readiness reads that
  hydrated diagnostic directly without copying it back into `runtime_diag`, so
  queue and safety transactions never deserialize or rewrite duplicate cycle
  diagnostics, readiness polls do not deserialize unused decision telemetry,
  and readers cannot pair a new cycle state with an old snapshot.
  Large decision and diagnostic JSON values use a checksummed, size-bounded,
  transparent zlib envelope in the existing JSON columns; small and legacy
  uncompressed rows retain the same read contract and API shape.
  Dashboard metrics count `snapshots_5m` over the exact indexed five-minute
  timestamp window instead of scanning and reporting the lifetime snapshot
  history; historical snapshots remain available to replay readers.
- direct adaptive intelligence -> allocator -> canonical final entry risk -> committee/governor: the strict scorer is diagnostic only; the adaptive policy selects enter versus abstain from the whole evidence vector and may override strategy-gate reasons, while continuous decision confidence scales requested lots. Missing/non-finite evidence, freshness, venue identity, broker protection, production authority, exposure, and hard risk remain binding, and only final-risk-approved entries reserve portfolio slots; no baseline twin is computed or persisted
- adaptive lifecycle -> monotonic hard lifecycle floor -> final action materialization -> final lifecycle risk -> committee/governor: model/reversal probabilities feed the one adaptive producer, position-signature-stable state survives loop refresh, hard floors may only increase protection, partial quantities are made broker-executable after the last producer, lifecycle overrides synchronize action, reason, score, size, and protection metadata before invalidating the prior approval, and every actionable intent is reapproved before the committee can veto it
- MQ4 positions report -> bridge receipt stamp -> runtime reconciliation: every legacy or JSON positions payload advances `positions_snapshot_token` and records `positions_snapshot_received_at`; only a token newly observed by the runner and received after the relevant command can prove lot reduction, position absence, or an unchanged broker position
- execution uncertainty -> authoritative broker book -> symbol-aware admission: unresolved mutation outcomes are account-wide until a fresh current-schema positions snapshot for the same account has broker source and bridge receipt times later than every uncertain transition. Exact-symbol uncertainty then quarantines only those symbols because the possible exposure is visible to portfolio and margin controls; missing-symbol and `CLOSE_ALL` uncertainty cannot be contained and remain global
- lifecycle command enqueue -> pending partial/exit ledgers -> command ACK or newer broker snapshot -> management-state commit: enqueue carries a versioned management context but cannot close a campaign, record a sleeve outcome, create recent-exit memory, or increment partial history; those mutations occur once at broker confirmation
- persisted managed state -> restart watermark -> durable command hydration: the runner restores ledgers and sleeve/campaign memory first, then scans only command rows changed after the later of the saved-state timestamp and last completed runtime-cycle timestamp
- MQ4 heartbeat -> bridge state -> live entry admission: every heartbeat emits `account_mode`, `account_scope`, and `account_magic`; the bridge resets the attested mode/scope before parsing so missing identity tokens cannot inherit prior entry authority, and the scope binds account number, server, and Magic without exposing the raw account number. The scalp authority and command both bind `admission_mode` plus expected `account_mode`, and enqueue/poll reject drift
- twin/research -> advisory artifact -> production telemetry: research output outside an explicitly selected authenticated `signed_validation` bundle can inform operators and future builds, but it is never read as a live permit or veto; no local demo admission can convert research output into certified evidence
- runtime -> commands queue -> broker poll: all seven command verbs (`BUY`, `SELL`, `CLOSE`, `CLOSE_ALL`, `CLOSE_PARTIAL`, `MODIFY_SL`, `INFO`) are bound to the current production boot, authority revision, and explicit scopes in the normal live plane. Enqueue and poll atomically recheck those fields, the queue kill, broker identity, freshness, and command-specific intent; stale, out-of-scope, or unattested commands are quarantined instead of delivered. The scalp protective-management plane narrows this contract to exact-owner `CLOSE` only
- model-stack candidate -> selected broker contract -> risk/provider/MQL: before live MT4 entry risk, the runner projects the current authenticated selected-symbol contract and binds the side quote plus an exact 20-point adverse fill on the broker tick grid. Risk sizing and bracket payoff use that worst permitted fill, and the complete account/venue/symbol/broker-symbol/contract geometry survives approval and the provider's one shared exact-entry serializer. That serializer has no bare BUY/SELL compatibility branch: every non-MTVCLC MT4 entry must satisfy the same exact envelope before a wire line exists. The EA parses the strategy-neutral `MarketEntryEnvelope` and rechecks the same contract, quote bound, lot grid, margin, SL/TP, and remaining command-owned slippage immediately before every `OrderSend`; there is no generic `SlipPts` fallback or positive-ticket ACK shortcut.
- production scalp candidate -> market envelope -> risk/store/poll/MQL: the selected pair carries `execution_type=market`, `pending_orders_forbidden=true`, its immutable five-second post-observation entry deadline, broker-plan v2, exact 20-point worst-fill allowance, a grid-aligned bracket whose directional bid/ask protection floor includes the fixed five-point cushion, current broker contract binding, and `trade_allowed`; authority validates complete exact-22 contract identity and geometry without globalizing the session flag, while selected-symbol sizing, enqueue, poll, provider, and EA boundaries each require the complete immediate-entry contract and `trade_allowed=true`. The EA rereads its advancing UTC clock on every retry immediately before `OrderSend`, so an expired immediate command can never become a late market mutation
- MT4 mutation -> post-ticket `OrderSelect` -> ACK classifier -> durable command: entries and ticket-addressed protective mutations report versioned actual command/symbol/broker symbol/side/type/ticket/Magic/comment/lots/remaining-lots/open/close-time/SL/TP evidence as applicable. Only confirmed semantic agreement may become `acked`; full close additionally proves zero remainder and positive close time. `failed`/`duplicate` is terminal only for an explicit ticketless `not_attempted`, while positive-ticket, mismatch, missing, attempted, or unknown outcomes become sticky `reconcile_required`; typed ACK-safety fields, the command event, and runtime counters commit atomically
- durable terminal ACK -> replay: exact equality of effective status, mutation state, ticket, actuals, and attestation is idempotent and creates no second event or trade count; a contradictory replay escalates to `reconcile_required` and cannot silently replace prior broker evidence
- live model set -> startup command admission: every configured pair must have an active explicit pair-allowlisted production rollout with positive budget, and the live intent scope must include `enter` plus enabled protective lifecycle intents, or startup fails closed
- runtime-native MTVCLC admission -> v3 scalp strategy authority -> final entry approval: demo and real derive the same exact-22 engine/cost/surface/execution identity; the live control plane persists boot-bound authority, and every entry command binds its generation, cost mapping, qualification surface, execution contract, revision, boot, and expiry.
- invalid signed scalp evidence -> revoked historical authority -> protective-management egress: read-only preflight may continue, but BUY/SELL remain refused; readiness requires a structurally valid revoked authority, exact-22 `scalp`/`EXIT` scopes, zero budget, current boot/account attestation, and an exact historical entry binding joined by successful ACK or a fresh authoritative position snapshot. Each eligible `CLOSE` carries `managed_entry_command_id`; every other verb fails closed
- queue admission -> live entry evidence: attempts and raw submit calls remain diagnostic only; an accepted entry requires a newly queued command or a duplicate that resolves to an existing `queued` or `delivered` record
- runtime evidence -> canary advancement: entry evidence is stamped with its observation time and current stage index/percentage; a ramp clears it, and only a later matching-stage observation inside the configured alert window can authorize another ramp; ACK metrics use the command event's durable `ts`
- runtime/operator -> state store: each runtime cycle sends the live-authority snapshot it read, while operator/runtime mutations use a row-locked atomic authority patch. Every authority mutation advances `authority_revision`; safety mutations are dominant, and only a fresh strategy-specific startup activation may re-enable killed authority. Transactional cycle patches preserve any newer kill, production scope, and audit fields instead of shallow-overwriting them with stale state. Release/twin fields remain advisory telemetry.
- bridge state -> operator readiness: `entry_configuration_ready` reports static rollout admission, while `new_entry_ready` additionally requires effective authority enabled in `live` mode with a positive revision, live runtime/queue state, matching broker-mode/scope attestation, no unresolved execution, and `production_scalp_any_pair_ready` for the scalp branch. `production_scalp_all_pairs_ready` reports exact-22 health only; it does not turn one otherwise ready pair into a global refusal. Authoritative empty scopes and zero values are never replaced by configuration fallbacks
- previous cycle -> capital governance -> entry admission: missing, stale, or schema-mismatched state pauses new entries for one fresh bootstrap cycle; stale persisted `shadow_policy` data is ignored and cannot pause trading or scale capital; protective lifecycle actions remain available
- capital-governance snapshots serialize their provider, rollback, reasons, and metrics contracts through explicit field projections. Nested diagnostic payloads retain independent mutation isolation without recursively serializing rollback actions once through the parent and then rebuilding them a second time
- sleeve tracker -> final entry admission: the current in-memory typed snapshot binds only direct-adaptive entries; `watch` remains a soft allocator penalty and `degraded` is a hard adaptive-entry veto

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
