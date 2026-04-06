# Runtime Loop

## Primary Files
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
- boot -> patch boot state -> purge pending commands
- manifest seed -> model load -> live feature refresh
- startup inference dry run -> activation consistency -> readying state
- main loop -> per-pair scoring -> lifecycle -> submissions -> state patch

## Main Loop Phases
- refresh live bars from bridge ticks/bars
- load latest feature rows per timeframe
- score live signal and baseline gates
- compute lifecycle / reversal / entry candidates
- apply shadow and adaptive ranking
- submit exits first, entries second
- patch runtime state and persist decisions

## Position And Action Flow
- open position state comes from bridge state + adaptive registry sync
- lifecycle models score exit / partial / reversal on the enriched row
- adaptive runtime can override hold/exit/rotation on top of model outputs
- command submission happens only at final cycle evaluation

## Handshakes
- runtime -> bridge ready: `/v2/ready`
- runtime -> bridge ticks/bars: live bar refresh inputs
- runtime -> bridge state store: `patch_state`, `store_decisions`
- runtime -> commands queue: `submit_command`

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [twin-vs-prod-parity.md](twin-vs-prod-parity.md)
- [../../fx-quant-stack/docs/architecture.md](../../fx-quant-stack/docs/architecture.md)
