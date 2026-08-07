# Self-Improvement Loop — "the LLM proposes; deterministic code disposes"

This subsystem is a physically isolated, self-correcting research loop. An LLM (or a deterministic
heuristic fallback) *proposes* small, testable changes to strategy configuration;
deterministic code *disposes* — it sanitizes every proposal against a safety
allowlist, backtests the candidate, scores it against a single objective with hard
guardrails, and only then accepts or rejects it. The result is emitted as advisory
files only. It is not registered with the runtime experiment database and cannot
enter a production promotion chain automatically.

## Why this shape

The model is never trusted to *decide*. It only emits a candidate change-set.
Three deterministic fences stand between a proposal and any effect:

1. **Allowlist** (`fxstack/improve/knobs.py` → `validate_change_set`) — only
   registered knobs may be touched, every value is clamped to hard bounds, and
   **risk-locked caps may only move in the tightening direction** vs the
   incumbent. A hallucinating or compromised model cannot enlarge position size,
   loosen exposure, widen the spread gate, or relax correlation limits.
2. **Backtest + objective** (`evaluator.py`, `objective.py`) — a candidate must
   clear hard guardrails (minimum trades, bounded drawdown) and beat the
   incumbent's risk-adjusted objective before it is accepted.
3. **Physical isolation** — production Windows operations and the operator plane
   expose no self-correction launcher. The research package has no
   `RuntimeService`, database-upsert, registry-write, activation, bridge, or broker
   path. Moving a hashed evidence bundle into independent candidate-runtime
   validation is an explicit operator action outside this loop.

## Components (`fxstack/improve/`)

| Module | Role |
|---|---|
| `knobs.py` | The change-set safety allowlist. `validate_change_set` / `apply_change_set` / `default_config`. |
| `evaluator.py` | Deterministic backtest over a scored-signals dataset; `build_synthetic_dataset` for offline/CI. |
| `objective.py` | Single Sharpe-like objective + guardrail gate. |
| `memory.py` | Append-only reflection memory; makes the loop self-correcting and feeds the proposer. |
| `proposer.py` | `LLMProposer` (schema-constrained) + deterministic `HeuristicProposer` fallback. |
| `loop.py` | The driver: propose → validate → backtest → score → accept/reject → reflect → emit. |

## Local LLM client (`fxstack/llm/`)

Offline-first by design. The default backend is `null`: it reports unavailable so
the loop runs on the deterministic heuristic proposer — no GPU, no network, works
in CI and air-gapped. Plug in a local model server when you have one:

- **Ollama** (easiest): `FXSTACK_LLM_BACKEND=ollama`,
  `FXSTACK_LLM_BASE_URL=http://127.0.0.1:11434`, `FXSTACK_LLM_MODEL=qwen2.5:14b-instruct`.
- **vLLM / llama.cpp** (OpenAI-compatible server): `FXSTACK_LLM_BACKEND=openai_compat`,
  `FXSTACK_LLM_BASE_URL=http://127.0.0.1:8000`, `FXSTACK_LLM_MODEL=<served-model-id>`.

Security posture (matches the project's offline requirement):

- The client only ever **calls** a loopback URL; it never binds a port.
- Non-localhost URLs are rejected unless `FXSTACK_AGENT_ALLOW_REMOTE_LLM=true`.
- Structured output is enforced by JSON-mode + Pydantic schema validation with
  bounded retries (Instructor/Outlines-style, dependency-free).
- Download weights once, checksum them, and block outbound traffic after setup —
  the loop needs no network once a local server is up.

## CLI

These commands use the repository-only compatibility facade through the external
`fx-quant-stack` development environment. No `trader`/`fx-trader` console script is
published, and production packages do not contain this research surface. The facade
has a closed research/security/export command allowlist and cannot parse runtime,
bridge, database, training, activation, deployment, or operator commands.

```bash
# Report the configured local LLM backend (offline-safe; prints null when none).
uv run --project fx-quant-stack python -m src.trader.cli agent llm-check

# Explain a prior run in plain language (LLM narrates if available, else a
# deterministic template renders the same code-computed facts).
uv run --project fx-quant-stack python -m src.trader.cli agent explain --run-dir artifacts/improve/runs/nightly

# Fragility check: how much does the objective move under a +/- one-step nudge to
# each tuned knob? (robustness_score near 1.0 == robust, not a curve-fit spike)
uv run --project fx-quant-stack python -m src.trader.cli agent robustness --run-dir artifacts/improve/runs/nightly

# Emit a single proposal for the seed config (no evaluation loop).
uv run --project fx-quant-stack python -m src.trader.cli agent propose --seed 1729

# Run the full self-improvement loop on synthetic data and emit advisory evidence.
uv run --project fx-quant-stack python -m src.trader.cli agent improve --iterations 12 --seed 1729 --run-name nightly

# Convert the live scorer's output (whatever its column names) into the loop's
# scored-signals schema, then run the loop on real data.
uv run --project fx-quant-stack python -m src.trader.cli agent build-dataset --features data/scored_features.parquet \
  --out data/scored_signals.parquet --spread-col spread --fwd-ret-col fwd_ret
uv run --project fx-quant-stack python -m src.trader.cli agent improve --dataset data/scored_signals.parquet --out-dir artifacts/improve/runs/eurusd

# Multi-restart campaign: explore the same landscape from several seeds and keep
# the global out-of-sample-validated best (escapes local optima).
uv run --project fx-quant-stack python -m src.trader.cli agent improve --restarts 6 --iterations 20
```

## Multi-restart campaign

`run_improvement_campaign` (CLI `--restarts N`) runs N independent searches over
the *same* dataset and base config — only the search seed differs — then keeps the
global winner ranked by in-sample objective, with the OOS objective and seed as
deterministic tiebreaks. The winning seed is replayed once with file emission enabled
so the advisory proposal corresponds exactly to the selected best. No runtime or
factory registration is available.

Artifacts written under `--out-dir` (default `<FXSTACK_IMPROVE_ARTIFACT_ROOT>/runs/<run-name>`):
`best_config.json`, `summary.json`, `reflection_memory.jsonl`, and (unless
`--no-experiment`) a contract-valid `proposal.json` + `reflection_memory.json`.

## Walk-forward overfit guard

The loop is self-correcting, not curve-fitting: the dataset is split time-ordered
into an in-sample train slice and a held-out out-of-sample (OOS) test slice
(`FXSTACK_IMPROVE_OOS_FRACTION`, default the last 30%). A candidate is ranked on
train, but it is only accepted if it *also* holds up out-of-sample — its OOS
objective may not drop more than `FXSTACK_IMPROVE_OOS_TOLERANCE` below the
incumbent's. Changes that only win in-sample are recorded as `rejected_overfit`.
Set `FXSTACK_IMPROVE_OOS_FRACTION=0` to disable and fall back to a single split.

## LangGraph runner

The same propose → dispose → reflect cycle is also available as a checkpointed
LangGraph `StateGraph` (`fxstack/improve/graph.py`, CLI `--runner graph`), giving
per-node observability, durable state, and a natural seam for human-approval
interrupts. It reuses the identical shared primitives
(`validate_change_set` / `apply_change_set` / `evaluate_config` / `score_metrics`),
so the deterministic "code disposes" guarantees are the same; the plain loop remains
canonical for OOS guarding, campaigns, and advisory evidence emission.

```bash
uv run --project fx-quant-stack python -m src.trader.cli agent improve --runner graph --iterations 20
```

## Determinism

With the heuristic proposer and a fixed `--seed` + dataset, the loop is fully
reproducible — the same best change-set, objective, and advisory proposal every
run. That property is what makes the loop testable and auditable; the LLM only
improves *proposal quality*, never the judging.

## Settings

| Env var | Default | Meaning |
|---|---|---|
| `FXSTACK_LLM_BACKEND` | `null` | `null` / `ollama` / `openai_compat` |
| `FXSTACK_LLM_BASE_URL` | `http://127.0.0.1:11434` | Local model server |
| `FXSTACK_LLM_MODEL` | `qwen2.5:14b-instruct` | Served model id |
| `FXSTACK_AGENT_ALLOW_REMOTE_LLM` | `false` | Allow non-localhost model URL |
| `FXSTACK_IMPROVE_MAX_ITERATIONS` | `12` | Loop iterations |
| `FXSTACK_IMPROVE_SEED` | `1729` | Reproducibility seed |
| `FXSTACK_IMPROVE_MIN_TRADES` | `30` | Guardrail: minimum trades |
| `FXSTACK_IMPROVE_MAX_DRAWDOWN_PCT` | `12.0` | Guardrail: max drawdown |
| `FXSTACK_IMPROVE_OOS_FRACTION` | `0.3` | Walk-forward holdout fraction (0 disables) |
| `FXSTACK_IMPROVE_OOS_TOLERANCE` | `0.25` | Max allowed OOS objective degradation |
