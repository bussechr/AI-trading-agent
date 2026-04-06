# Bridge And API Handshakes

## Primary Files
- [app.py](../../fx-quant-stack/src/fxstack/api/app.py)
- [protocol.py](../../fx-quant-stack/src/fxstack/runtime/protocol.py)
- [service.py](../../fx-quant-stack/src/fxstack/runtime/service.py)
- [route.ts](../../app/api/trading/state/route.ts)

## Upstream
- [runtime-loop.md](runtime-loop.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Bridge Contracts
- `/v2/ready`: readiness, freshness, runtime startup progress
- `/v2/state`: full bridge state snapshot used by dashboard route
- `/v2/commands`: enqueue or poll broker commands
- `/v2/commands/events`: ACK and delivery history
- `/v2/decision-snapshots`: persisted decision history for twin validation

## Command Lifecycle
- runtime builds `ExecutionCommand`
- `RuntimeService.submit_command` dedupes and enqueues via store
- `protocol.command_to_mt4_line` serializes MT4 wire line
- MT4 ACKs via bridge -> store updates command state and events

## State Handshakes
- bridge stores runtime patch fragments in DB + in-memory tick caches
- dashboard route fetches bridge JSON and normalizes it into a stable client contract
- ops scripts use `/v2/ready` and `/v2/state` for health gates

## Related Docs
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [runtime-loop.md](runtime-loop.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
