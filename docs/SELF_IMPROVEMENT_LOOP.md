# Self-Improvement Loop — "the LLM proposes; deterministic code disposes"

This subsystem closes the existing experiment-factory pipeline into a
self-correcting, self-improving research loop. An LLM (or a deterministic
heuristic fallback) *proposes* small, testable changes to strategy configuration;
deterministic code *disposes* — it sanitizes every proposal against a safety
allowlist, backtests the candidate, scores it against a single objective with hard
guardrails, and only then accepts or rejects it. The result is emitted as a
Phase-7 `ExperimentProposal` that flows into the existing
`draft → review → replay → paper → canary → promote` factory.

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
3. **Human/factory gate** — the emitted `ExperimentProposal` starts in
   `approval_status="draft"` and still passes through the normal promotion gates
   (replay windows, paper, canary, operator sign-off) before anything goes live.

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

```bash
# Report the configured local LLM backend (offline-safe; prints null when none).
trader agent llm-check

# Explain a prior run in plain language (LLM narrates if available, else a
# deterministic template renders the same code-computed facts).
trader agent explain --run-dir artifacts/improve/runs/nightly

# Emit a single proposal for the seed config (no evaluation loop).
trader agent propose --seed 1729

# Run the full self-improvement loop on synthetic data and emit an experiment proposal.
trader agent improve --iterations 12 --seed 1729 --run-name nightly

# Run against a real scored-signals parquet (ts,pair,swing_prob,entry_prob,
# trade_prob,expected_edge_bps,spread_bps,fwd_ret_bps).
trader agent improve --dataset data/scored_signals.parquet --out-dir artifacts/improve/runs/eurusd

# Multi-restart campaign: explore the same landscape from several seeds and keep
# the global out-of-sample-validated best (escapes local optima).
trader agent improve --restarts 6 --iterations 20 --register
```

## Multi-restart campaign

`run_improvement_campaign` (CLI `--restarts N`) runs N independent searches over
the *same* dataset and base config — only the search seed differs — then keeps the
global winner ranked by in-sample objective, with the OOS objective and seed as
deterministic tiebreaks. The winning seed is replayed once with emission enabled so
the registered `ExperimentProposal` corresponds exactly to the selected best.

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
canonical for OOS guarding, campaigns, and factory emission.

```bash
trader agent improve --runner graph --iterations 20
```

## Determinism

With the heuristic proposer and a fixed `--seed` + dataset, the loop is fully
reproducible — the same best change-set, objective, and experiment proposal every
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
