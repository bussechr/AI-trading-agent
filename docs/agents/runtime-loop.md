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
- runner admission -> validate profile/mode/live arming and explicit scopes -> require binding desk-overlay, structure-timing/chase, uncertainty, belief, campaign, and capital-governance producers -> read-only active-manifest preflight
- bridge checks -> boot -> patch boot state -> purge pending commands
- hash-anchored manifest seed -> required-pair seed gate -> model load -> live feature refresh
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
- apply adaptive policy ranking plus same-runner execution-posture diagnostics
- refresh sleeve health after exit accounting; degraded, missing, mismatched, or invalid sleeve state hard-blocks new adaptive entries
- submit exits first, entries second
- patch the exact admission-time governance snapshot to both top-level `governance` and runtime diagnostics, then persist decisions

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync
- lifecycle models score exit / partial / reversal on the enriched row
- adaptive runtime can override hold/exit/rotation on top of model outputs
- command submission happens only at final cycle evaluation

## Handshakes

- filtered installed package -> package preflight -> runner: dependency and physical-isolation checks complete before any production entrypoint is trusted
- direct installed runner -> startup admission: `Settings.validate_for_startup()`, launch-posture invariants, complete forbidden-module absence, and read-only active-model validation all complete before bridge checks or `RuntimeService` import/construction
- external activation -> immutable deployment handoff -> DB seed -> loaded runtime: one preflight SHA-256 anchors the pre-deployed manifest through seeding and consistency; required pairs must match on presence, model-set ID, registry path, and available artifact identity/digests or startup fails
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime -> bridge state store: `patch_state`, `store_decisions`
- runtime -> commands queue: `submit_command`
- previous cycle -> capital governance -> entry admission: missing, stale, or schema-mismatched state pauses new entries for one fresh bootstrap cycle; shadow alignment consumes the producer's `shadow_live_divergence_counts` snake-case contract; protective lifecycle actions remain available
- sleeve tracker -> final entry admission: the current in-memory typed snapshot binds both strict and adaptive-ready paths; `watch` remains a soft allocator penalty and `degraded` is a hard entry veto

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [causal-research-and-runtime-validation.md](causal-research-and-runtime-validation.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
