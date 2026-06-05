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

# Emit a single proposal for the seed config (no evaluation loop).
trader agent propose --seed 1729

# Run the full self-improvement loop on synthetic data and emit an experiment proposal.
trader agent improve --iterations 12 --seed 1729 --run-name nightly

# Run against a real scored-signals parquet (ts,pair,swing_prob,entry_prob,
# trade_prob,expected_edge_bps,spread_bps,fwd_ret_bps).
trader agent improve --dataset data/scored_signals.parquet --out-dir artifacts/improve/runs/eurusd
```

Artifacts written under `--out-dir` (default `<FXSTACK_IMPROVE_ARTIFACT_ROOT>/runs/<run-name>`):
`best_config.json`, `summary.json`, `reflection_memory.jsonl`, and (unless
`--no-experiment`) a contract-valid `proposal.json` + `reflection_memory.json`.

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
