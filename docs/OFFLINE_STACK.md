# Offline Agentic Stack — serving, security, brokers, research

This documents the stack layers around the self-improvement loop (see
[SELF_IMPROVEMENT_LOOP.md](SELF_IMPROVEMENT_LOOP.md)). Everything here is
offline-first: the default paths make no network calls, bind only loopback, and
keep secrets local. The design principle holds throughout — **the LLM proposes;
deterministic code disposes.**

## 1. Local model serving (Gemma / Phi / Qwen)

Run a local model server and point the LLM client at it (the proposer stays the
only non-deterministic actor; judging is always deterministic code):

```bash
# Ollama (easiest)
FXSTACK_LLM_BACKEND=ollama FXSTACK_LLM_MODEL=qwen2.5:14b-instruct
# vLLM / llama.cpp (OpenAI-compatible)
FXSTACK_LLM_BACKEND=openai_compat FXSTACK_LLM_BASE_URL=http://127.0.0.1:8000
```

`fx-quant-stack/docker/docker-compose.offline.yml` brings up `ollama` + a
vLLM-style OpenAI-compatible service + the app, all bound to `127.0.0.1`, on an
`internal: true` network, with weights pre-staged via a read-only volume.
`fx-quant-stack/docker/llamacpp.Dockerfile` is a GGUF llama.cpp server skeleton.

Validate the air-gap invariants before bringing it up:

```bash
trader security validate-offline      # asserts loopback-only ports, internal net, no remote LLM
```

## 2. Download weights once, checksum, then block egress

```bash
# After staging weights, verify them against a checksum manifest at startup:
trader agent verify-weights --manifest model_manifest.json
# Then block outbound traffic except loopback (dry-run by default):
ops/security/block_egress.sh            # preview
ops/security/block_egress.sh --apply    # enforce (root; nft/iptables)
```

`fxstack.llm.weights` refuses to touch the network unless `allow_network=True` is
explicitly passed; the default path only verifies already-staged files by SHA-256.

## 3. Broker credentials in a local secret manager

```bash
FXSTACK_SECRET_VALUE=... trader security secret --set OANDA_API_TOKEN
trader security secret --list           # names only; values are never printed
```

`fxstack.security.secrets.SecretStore` is an encrypted, file-backed store
(Fernet/AEAD when `cryptography` is installed, a documented dev-grade stdlib
fallback otherwise). The master key comes from `FXSTACK_SECRET_KEY` or a local key
file; secret values are never logged. The store dir is gitignored.

## 4. Broker connectors (OANDA / IBKR / MT5)

`fxstack.providers.execution.{oanda,ibkr,mt5}` implement the existing execution
contract in **dry-run by default** — they shape and record order intents with no
network call. Live API clients are imported lazily and only reachable when
`dry_run=False` with explicit endpoints. Credentials are injected from the secret
store / env, never hardcoded. Install clients via the `[brokers]` extra.

## 5. Richer research metrics (vectorbt)

```bash
trader agent metrics --run-dir artifacts/improve/runs/nightly
```

`fxstack.research.vectorbt_harness.run_vectorbt_research` reports trades, win rate,
Sharpe, Sortino, Calmar, max drawdown, exposure, and profit factor over a
scored-signals frame. It computes everything on a dependency-free numpy path and
uses `vectorbt` (the `[research]` extra) only as an optional equity-curve
cross-check.

## 6. Export to QuantConnect Lean

```bash
trader backtest export-lean --run-dir artifacts/improve/runs/nightly \
  --out lean/eurusd --pairs EURUSD,GBPUSD --start 2022-01-01 --end 2023-01-01
```

`fxstack.backtest.harness.lean_codegen` renders a runnable `QCAlgorithm` (main.py +
config.json) that applies the tuned gate/risk thresholds, for institutional-grade
backtesting in Lean. Wire real model probabilities into the generated `_signal`
hook before using it for live Lean runs.

## Optional dependency extras

```bash
uv sync --extra research    # vectorbt + optuna + duckdb + jupyter
uv sync --extra security    # cryptography (Fernet secret backend)
uv sync --extra brokers     # oandapyV20, ib_insync, MetaTrader5 (win32)
```
