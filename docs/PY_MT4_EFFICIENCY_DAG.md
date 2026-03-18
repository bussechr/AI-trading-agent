# Python <-> MT4 Interconnection Efficiency DAG

## Goal
Define a deterministic, auditable stage contract for measuring end-to-end latency and suppression in the Python -> Bridge -> MT4 -> Bridge lifecycle.

## Stage DAG
1. `tick_ingest`
- MT4 EA posts `/v2/market/tick` and bridge updates latest symbol quote snapshot.

2. `decision_ready`
- Python runtime (`fxstack.runtime.runner`) fetches tick/state and computes candidate decisions.
- Compute telemetry emitted to `compute_trace.jsonl` (`agent_cycle_ms`, `score_symbol_ms`, `decision_count`).

3. `signal_post`
- Python bridge client posts `/v2/commands` with `command_id`, `trace_id`, `interop_mode`, `t_py_signal_post_start`.

4. `bridge_queue`
- Bridge validates payload, applies queue/rate-limit policy, and stores pending row with `t_bridge_queued`.

5. `poll_delivery`
- MT4 EA polls `/v2/commands/poll`; bridge marks `t_bridge_delivered` and returns command payload.

6. `ea_handle`
- EA parses command and records handle/execute timing fields.

7. `ack_post`
- EA posts `/v2/commands/ack` with execution status and timing fields:
  - `t_ea_received`
  - `t_ea_exec_start`
  - `t_ea_exec_end`
  - `t_ea_ack_post`
  - `ea_handle_to_ack_ms`

8. `ack_finalize`
- Bridge finalizes pending signal and emits transport trace row with merged stage latencies.

## Required Timestamps
- `t_py_signal_post_start`: Python client pre-POST timestamp.
- `t_bridge_queued`: signal accepted/queued timestamp.
- `t_bridge_delivered`: bridge delivery timestamp on `/v2/commands/poll`.
- `t_ea_received`: EA command receive timestamp.
- `t_ea_exec_start`: EA execution start timestamp.
- `t_ea_exec_end`: EA execution end timestamp.
- `t_ea_ack_post`: EA ACK POST timestamp.
- `t_bridge_ack_finalized`: bridge ACK-finalized timestamp.

## Derived Stage Latencies (ms)
- `signal_post_to_ack_ms` = `t_bridge_ack_finalized - t_py_signal_post_start`
- `bridge_queue_wait_ms` = `t_bridge_delivered - t_bridge_queued`
- `poll_delivery_lag_ms` = `t_bridge_delivered - t_bridge_queued`
- `ea_handle_to_ack_ms` = EA payload field, fallback to `t_ea_ack_post - t_ea_received`

## Error Budget Keys
- `rejected:backpressure_queue_full`
- `rejected:max_total_commands_per_minute`
- `rejected:max_new_entries_per_minute`
- `failed:retry_exhausted`
- `failed:<ea_message>`
- `acked:none`

## Trace Outputs
- Transport lifecycle rows: `data/state/audit/interop/transport_trace.jsonl`
- Agent/runtime compute rows: `data/state/audit/interop/compute_trace.jsonl`

## Notes
- Audit mode is instrumentation-only and does not alter strategy logic or risk rules.
- `INFO`/safe probe commands are preferred for live shadow transport measurements.
