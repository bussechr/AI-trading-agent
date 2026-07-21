# Bridge And API Handshakes

## Primary Files

- [app.py](../../fx-quant-stack/src/fxstack/api/app.py)
- [protocol.py](../../fx-quant-stack/src/fxstack/runtime/protocol.py)
- [service.py](../../fx-quant-stack/src/fxstack/runtime/service.py)
- [20_start_bridge.bat](../../ops/windows/20_start_bridge.bat)
- [package_preflight.py](../../fx-quant-stack/src/fxstack/runtime/package_preflight.py)
- [BridgeEA.mq4](../../MQL4/Experts/BridgeEA.mq4)
- [BridgeHttp.mqh](../../MQL4/Include/BridgeHttp.mqh)
- [route.ts](../../app/api/trading/state/route.ts)

## Upstream
- [runtime-loop.md](runtime-loop.md)
- [ops-entrypoints.md](ops-entrypoints.md)

## Downstream
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Bridge Contracts

- `/v2/handshake`: public wire-version/build metadata; major mismatch or a server `min_compatible` newer than the client is incompatible
- `/v2/ready`: readiness, freshness, runtime startup progress
- `/v2/state`: full bridge state snapshot used by dashboard route, including current database health
- `POST /v2/market/bars`: bounded completed-bar backfill from the authenticated MQL4 edge after bridge restart
- `GET /v2/market/bars`: merged broker-history and live-tick bars consumed by runtime feature refresh
- `/v2/commands`: enqueue or poll broker commands; direct live BUY/SELL enqueue is forbidden because entry authority is in-process and runner-owned
- `/v2/commands/ack`: authenticated, idempotent terminal broker-outcome ingestion
- `/v2/commands/events`: ACK and delivery history
- `/v2/decision-snapshots`: persisted decision history for runtime audit and offline export

## Installed API Boundary

- Production starts the bridge directly as `python -I -m uvicorn fxstack.api.app:app --loop asyncio:SelectorEventLoop`. The explicit Windows selector loop prevents expected EA socket closes from becoming Proactor callback trace churn. The bridge never enters through a repository source-tree shim or a second service path.
- `fxstack.api.app`, `RuntimeService`, the command store, ACK ingestion, and provider adapters come from the same filtered non-editable runtime distribution as the decision runner, feature worker, monitor, and preflights.
- The installer does not deploy raw `fx-quant-stack/src`. Training activation, mutable registry operations, research/backtest, replay/experiment, and alternate model loaders are physically absent from the API interpreter.
- The API accepts only artifacts and model identities prepared before deployment. It exposes no endpoint that trains, promotes, activates, rewrites the active manifest, or launches research.
- Shadow mode is a command-admission posture of the one installed runner behind this API; it is not a parallel model stack.

## Command Lifecycle

- runtime resolves a canonical post-adaptive entry, obtains an exact final risk-approved order, and then applies committee/governor and live-rollout vetoes
- live BUY/SELL uses `RuntimeService.submit_approved_command` with an in-process `FinalEntryApproval` that binds canonical readiness, the risk-approved payload, committee/governor permission, canary state, pair, side, trace identity, the fresh broker account mode/scope, and the current monotonic live-authority revision; the final payload may only reduce approved lots or add transport metadata
- immediately before enqueue, `submit_approved_command` rereads bridge state and the store repeats the check atomically with insertion; runtime/admission/kill state, broker account mode/scope, and the exact authority revision must still match, and the heartbeat plus the command pair's persisted broker-event-time tick must be fresh
- immediately before broker delivery, the store reauthorizes every queued BUY/SELL against the current runtime/admission/kill state, exact authority revision, approved pair/intent, broker account identity, heartbeat, and pair tick in the polling transaction; revoked, stale, superseded, or legacy-unattested entries are expired with an audit event, while protective commands remain independently pollable
- public `RuntimeService.submit_command`, including direct `POST /v2/commands`, rejects live MT4 BUY/SELL with `403 final_entry_approval_required`; HTTP callers cannot manufacture the in-process approval
- non-entry commands continue through `RuntimeService.submit_command`; protective CLOSE, CLOSE_ALL, CLOSE_PARTIAL, and MODIFY_SL actions are not gated by the entry canary, although live intent scope and the ordinary runtime, queue, governance, reconciliation, and broker protections still bind them
- any delivered, reconciliation-required, or previously delivered non-terminal command blocks new BUY/SELL admission with `409 reconciliation_required`; the store repeats the check atomically at enqueue and withholds prequeued BUY/SELL from polling, while CLOSE, CLOSE_ALL, CLOSE_PARTIAL, and MODIFY_SL remain admissible
- `protocol.command_to_mt4_line` serializes MT4 wire line
- after broker handling, the EA writes each JSON ACK to its pinned account/server + bridge-endpoint + Magic scoped `FILE_COMMON/FXStack/AckOutbox` directory, flushes it, closes it, and atomically promotes the temporary file before the first HTTP attempt; `ApiKey` is never written to disk, and filenames include a hash of `TERMINAL_DATA_PATH` plus chart identity to avoid cross-terminal writer collisions
- the EA does not invent an `account_0` namespace: account number, account server, endpoint, Magic, and terminal data path are pinned once all required identity is available. Startup remains command-fenced while identity is unavailable, and any later account/server/endpoint/Magic/terminal drift keeps polling fenced while replay and deinit continue using the original pinned scope
- every MQ4 heartbeat reports `account_mode` (`demo`, `contest`, `real`, or `unknown`), a non-reversible `account_scope` derived from account number + server + Magic, and `account_magic`; the bridge treats each heartbeat as authoritative and resets the attested mode/scope before parsing so an old or partial EA cannot retain prior entry authority
- only an explicit plain-text heartbeat or JSON report with `report_type=heartbeat` may refresh bridge liveness or account identity; generic JSON reports such as `positions_snapshot` can update their own data but cannot keep a stale heartbeat or prior broker attestation alive
- `/v2/state` and `/v2/ready` expose `entry_configuration_ready` for static rollout admission and a separate dynamic `new_entry_ready`; the latter is true only while the effective authority is enabled in `live` mode with a positive revision, runtime and queue are enabled, MT4/ticks/runtime signal data are fresh, the expected broker mode and account scope are attested, and execution reconciliation is clear. Effective empty scopes and zero budgets remain empty/zero rather than falling back to configuration. `new_entry_blocking_reasons` names the current owner of a stop.
- startup and every timer replay the durable outbox directly to `/v2/commands/ack`; replay never enters command handling. Timer replay is bounded to four files and rotates across pending files so one non-2xx record does not prevent attempts for other records
- the EA removes an ACK file only after HTTP 2xx. Transport, auth, unknown-command, invalid-transition, schema, and server failures remain queued and visible; temporary promotion failures are recovered on startup/timer, and deinit performs a final flushed persistence pass
- while any ACK is queued, staged, or not yet durable, the EA fences `/v2/commands/poll`. This prevents a delivered command from being re-polled and broker-executed while its outcome is awaiting reconciliation
- the outbox is capped at 512 persisted files with one slot reserved before polling; capacity or persistence failure is fail-closed and shown in the EA dashboard/log
- duplicate terminal delivery relies on the store contract: after the first accepted terminal ACK, replay returns HTTP 200 with `idempotent: true`, appends no duplicate terminal event, and does not increment trade telemetry. A terminal late ACK clears the exposure fence, including for a delivered command whose queue TTL elapsed
- BUY/SELL lot size is immutable after Python risk approval: the EA accepts it only when it is finite, positive, within the live broker `MODE_MINLOT`/`MODE_MAXLOT` bounds, and exactly on `MODE_LOTSTEP`; it rejects instead of rounding or clamping
- partial closes are preflighted across the current broker tickets before the first `OrderClose`; every close chunk and any ticket remainder must be exactly executable, and cumulative closed lots may never exceed the approved `close_lots`
- BUY/SELL requires finite positive absolute `sl` and `tp_price` at the EA boundary. A BUY requires SL below bid and TP above ask; a SELL requires SL above ask and TP below bid. Broker stop-level distance is checked immediately before every `OrderSend`, including retries
- `MODIFY_SL` is tightening-only at the EA boundary: BUY stops must move strictly up and SELL stops strictly down, except that an absent broker stop may be initialized

## State Handshakes
- bridge stores runtime patch fragments in DB + in-memory tick caches
- runtime state exposes `live_command_admission`, broker account attestation, and entry-ratio evidence counts/status; the ratio numerator counts only newly `queued` commands or duplicates backed by existing `queued`/`delivered` records, never rejected or failed attempts, and zero approved/accepted entries is `insufficient_evidence`
- while runtime is not ready, the EA backfills completed M5 broker bars in bounded batches; the bridge merges them with live tick aggregation so startup can rebuild causal M15/H1/H4/D context without accepting stale features
- EA position reports include the current broker `sl`; lifecycle fail-safes use it to suppress non-monotonic stop commands before submission
- dashboard route fetches a verified state source, pins dependent reads to that exact bridge, and normalizes it into a stable client contract
- ops scripts use `/v2/ready` and `/v2/state` for health gates
- runtime and dashboard verify the exact reachable bridge through `/v2/handshake`; compatible minor/patch drift warns, while transient handshake failures are retried

## Related Docs
- [dashboard-dataflow.md](dashboard-dataflow.md)
- [runtime-loop.md](runtime-loop.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
