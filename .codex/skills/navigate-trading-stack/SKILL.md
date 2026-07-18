---
name: navigate-trading-stack
description: Navigate and change the active Trading Agent repository end to end. Use for runtime, bridge/API, dashboard, model or feature math, parquet/cache integrity, twin parity, operator-plane, or Windows startup work where Codex must find the authoritative files, trace a contract across boundaries, choose a safe shadow run posture, and verify with the smallest high-signal checks.
---

# Navigate Trading Stack

Use the repository's existing agent docs as the source of truth; do not reproduce them in context.

## Route the task

1. Read `AGENTS.md`.
2. Read only the matching page in `docs/agents/README.md`; consult `docs/agents/system-map.yaml` for machine-readable ownership and handshakes.
3. Trace the changed value or decision through producer, normalization, persistence, API, and consumer. Search `AGENT FLOW`, `AGENT HANDSHAKE`, `AGENT PARITY`, and `AGENT HOT PATH` to find seams quickly.

Use these primary routes:

- Runtime, command lifetime, ACKs, and persistence: `docs/agents/runtime-loop.md` and `docs/agents/bridge-and-api-handshakes.md`.
- Dashboard source selection, state, history, and UI consumers: `docs/agents/dashboard-dataflow.md`.
- Quant math, session/multi-timeframe features, parquet source contracts, model/registry artifacts, RL checkpoints, and activation: `docs/agents/model-stack-and-feature-flow.md`.
- Twin/live shared logic: `docs/agents/twin-vs-prod-parity.md`.
- Process ownership and guarded startup: `docs/agents/ops-entrypoints.md`.
- Operator-only supervision: `docs/agents/operator-plane.md`.

## Change a boundary

- Update the producer and every typed or normalized consumer together.
- Preserve finite-number, freshness, version, and source-identity invariants; fail closed when evidence is missing.
- Keep model deserialization behind `models/artifact_contract.py`, RL checkpoint use behind `rl/trainer.py` plus `rl/proposal.py`, and raw snapshot reads behind `io/parquet_store.py` plus `features/multi_tf_contract.py`.
- Update the relevant agent page and `system-map.yaml` when ownership, an entrypoint, or a handshake changes.
- Ignore legacy paths unless an active agent page points to them.

## Verify efficiently

- Run the smallest regression that would fail without the change, plus targeted lint/type checks for touched files.
- Avoid broad suites unless the user requests them or a boundary change cannot be covered narrowly.
- For end-to-end verification, use the isolated shadow posture in `docs/agents/ops-entrypoints.md`. Never enable live orders, reuse an unrelated listener, or stop processes without proving repository ownership.
- For live-action backtests, require delayed execution (`FXSTACK_TWIN_FILL_DELAY_BARS>=1`) and verify the emitted `causal_replay` metadata before interpreting PnL.
- For retraining, keep candidate artifacts separate with the exact `FXSTACK_TRAIN_*` and `FXSTACK_ACTIVATE_*` variables documented in `docs/agents/ops-entrypoints.md`; confirm the spawned command line before a long batch. Train the cross-pair belief bundle once, then pass `FXSTACK_TRAIN_WITH_BELIEF=0` to pair jobs.
- Before committing, run `git diff --check`, inspect `git status --short`, and keep the commit cohesive.
