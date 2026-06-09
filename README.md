# FX Trading Agent

## An AI trading agent for IG MT4 that reads the market, debates every setup, and improves itself

This is a full stack autonomous FX trading system built on one operating principle: the AI proposes and deterministic code disposes. Trained probability models read every pair on every bar. A committee of specialist agents debates each setup. A governor arbitrates their votes through a transparent decision path. A self-improvement loop keeps tuning the whole machine, and a digital twin proves every change against history before it can ever touch a live order.

What follows is a tour of the five things that make it tick: the AI, the agents, the training, the workflows, and the paths that connect them.

The active strategy stack lives in [`fx-quant-stack`](fx-quant-stack/README.md). Runtime and bridge execution are v2 only (`fxstack`). The one-click Windows launcher is `launch_all.bat live`, built on the modular scripts under [`ops/windows/`](ops/windows/).

## Safety First

This repository is research software, not financial advice. Run on a demo account first, keep credentials and account identifiers out of Git, and read [SAFETY.md](SAFETY.md), [SECURITY.md](SECURITY.md), and [DISCLAIMER.md](DISCLAIMER.md) before any live use.

No open-source license is granted yet. See [LICENSE.md](LICENSE.md) for the current no-warranty, source-available terms.

## The AI

At the core are trained probability models that turn raw price action into calibrated conviction. For every pair on every bar they produce three numbers: a swing probability (is direction real), an entry probability (is the timing right), and a trade probability (is the whole setup worth taking). A probability gated policy in [`fxstack/live/policy.py`](fx-quant-stack/src/fxstack/live/policy.py) converts those into a clean approved or blocked decision, with expected edge measured net of cost and spread.

Around that core sits a deeper layer of intelligence:

- Regime filtering and adaptive policy in [`fxstack/backtest/adaptive_policy.py`](fx-quant-stack/src/fxstack/backtest/adaptive_policy.py) tilt behavior to the market state, so the agent presses in clean trends and stands down in chop.
- A cross pair directional belief engine in [`fxstack/belief/`](fx-quant-stack/src/fxstack/belief) ranks hypotheses across the whole universe, sharing one query grouping contract between training, twin replay, and live shadow inference.
- A model intelligence score travels alongside every decision for full observability.

On top of all of that runs the self improvement loop in [`fxstack/improve/`](fx-quant-stack/src/fxstack/improve). A local first LLM client in [`fxstack/llm/`](fx-quant-stack/src/fxstack/llm), running on Ollama, vLLM, or llama.cpp over loopback only and fully offline, proposes configuration changes drawn from a strict allowlist. Deterministic evaluators then score each proposal against held out data, an objective function, and robustness checks before anything is accepted. The LLM proposes. The code disposes. The agent gets better on its own while every change stays auditable.

## The Agents

Every entry is decided by a committee of deterministic specialist agents in [`fxstack/orchestration/`](fx-quant-stack/src/fxstack/orchestration), each an expert in one way to read the market:

- Trend pullback, range mean reversion, breakout expansion, and reversal specialists each argue their own playbook.
- A spread and microstructure gate guards execution quality at the tick level.
- An execution quality agent vets the entry itself.
- A portfolio risk agent protects the book.

These agents run inside a LangGraph orchestration graph that walks a clear sequence on every cycle: assemble context, signal, risk, portfolio, lifecycle, committee, aggregate packet, govern, finalize. A governor in [`fxstack/orchestration/governor.py`](fx-quant-stack/src/fxstack/orchestration/governor.py) ranks every proposal and arbitrates through a transparent staged decision path (hard policy blocks, lifecycle exits, portfolio checks, entry ranking, final decision). A sleeve allocator and thesis campaign manager in [`fxstack/strategy/`](fx-quant-stack/src/fxstack/strategy) size and select across the portfolio, and a risk kernel in [`fxstack/risk/kernel.py`](fx-quant-stack/src/fxstack/risk/kernel.py) gives the final approval. Every proposal, score, vote, and block reason is recorded, so you can always read exactly why the agent acted.

An optional operator plane in [`services/operator_plane/`](services/operator_plane) exposes supervisory MCP servers for runtime state, twin artefacts, and the release registry, giving you agent grade tooling for inspection and staging.

## The Training

The edge is earned in training, and training is a first class workflow here. The pipeline runs end to end through the unified CLI and the numbered ops scripts:

- Ingest, build features, generate labels, train, and activate, each as its own stage.
- A model stack that combines gradient boosted swing and intraday models, regime detection, and the cross pair directional belief ranker.
- A weekly full retrain and auto activate cycle keeps the models fresh against new market data.
- A GPU first full pipeline backtest (`run_full_scale_backtest_gpu.sh`) runs the entire training to evaluation flow offline in WSL.
- A digital twin in [`tools/fxstack_digital_twin_backtest.py`](tools/fxstack_digital_twin_backtest.py) replays the exact production decision logic against history, so the twin and the live runtime stay in lockstep.

```bash
uv run --project fx-quant-stack python -m src.trader.cli stack preflight
uv run --project fx-quant-stack python -m src.trader.cli train all --pair EURUSD --force-retrain
uv run --project fx-quant-stack python -m src.trader.cli models activate --require-all
```

## The Workflows

One command brings the whole stack to life, and a clean set of workflows takes it from a fresh checkout all the way to live execution and back to validation.

- `launch_all.bat live 10000` starts the bridge, runtime, dashboard, and supporting workers, then opens the operator dashboard at `http://127.0.0.1:3000`.
- The numbered scripts in [`ops/windows/`](ops/windows) form a readable pipeline from `00_preflight` through training, activation, start, monitoring, and `90_stop_all`.
- `ops/windows/40_full_scale_e2e_validation.bat` runs a fail fast training to live to gate to finalization validation in one shot.
- `ops/windows/31_shadow_24h.bat` runs a full day of shadow decisions for canary comparison before any cutover.
- The feature push worker, confidence monitor, and aggregate `25_monitor_everything.ps1` keep the running system observable.

Status and shutdown are equally simple:

```bash
launch_all.bat status
launch_all.bat stop
```

## The Paths

The whole repository is wired so that an agent, or a new engineer, can navigate from intent to code in a couple of hops.

- [`AGENTS.md`](AGENTS.md) is the front door.
- [`docs/agents/system-map.yaml`](docs/agents/system-map.yaml) is the authoritative machine readable map of every system, file, handshake, and entrypoint, covering the runtime, providers, portfolio, strategy allocator, belief engine, the self improvement loop, the LLM client, and more.
- Source files carry `# AGENT:` breadcrumbs (ROLE, ENTRYPOINT, DEPENDS ON, CALLED BY, SEE) so each file tells you who calls it, what it depends on, and where to read next.
- A linter in [`tools/audit_agent_nav_graph.py`](tools/audit_agent_nav_graph.py) keeps every one of those paths resolving.

The live decision path is just as legible. A tick arrives, features refresh, the models score, the committee debates, the governor arbitrates, the allocator sizes, the risk kernel approves, the bridge queues the order, and the MT4 EA fills it. Start at [`docs/agents/runtime-loop.md`](docs/agents/runtime-loop.md) and follow the breadcrumbs.

## Quick Start

### 1. Install

```bash
git clone <your-repo-url>
cd "Trading Agent"

# Authoritative Python environment
cd fx-quant-stack
uv sync --extra dev
cd ..

# Dashboard dependencies
pnpm install
```

### 2. Configure MT4

See [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md) for the full MT4 setup.

Requirements:

- An IG MT4 account, demo or live. Keep your account number and broker server name out of any committed file.
- WebRequest enabled for `http://127.0.0.1:58710`.
- `BridgeEA.mq4` compiled and attached to a chart with AutoTrading enabled.

### 3. Run the system

The recommended operator path serves the dashboard at `http://127.0.0.1:3000`:

```bash
launch_all.bat live 10000
```

Developer preview only, on `http://127.0.0.1:3001`:

```bash
pnpm dev
```

In MT4: open an H1 chart, enable AutoTrading (Alt+A), drag `BridgeEA` onto the chart, and confirm the EA is running.

## System Architecture

```
┌──────────────────┐
│  Dashboard       │  http://127.0.0.1:3000
│  (Next.js)       │  Real-time monitoring
└────────┬─────────┘
         │
┌────────▼─────────┐
│  Bridge Server   │  http://127.0.0.1:58710
│ (FastAPI fxstack)│  v2 state + command lifecycle
└────────┬─────────┘
         │
    ┌────┴────┐
    │         │
┌───▼───┐ ┌───▼───┐
│ Agent │ │ MT4   │
│ (Py)  │ │ EA    │
└───────┘ └───────┘
```

## Configuration and Gates

The active stack reads configuration from environment variables and an optional `.env` file at the project root. Defaults live in [`fx-quant-stack/src/fxstack/settings.py`](fx-quant-stack/src/fxstack/settings.py).

### Active live entry gates

Conditions enforced by `gate_decision()` for a new entry, in order:

| Gate | Threshold (env var) | Default | Behavior |
|---|---|---|---|
| Spread | `FXSTACK_MAX_ALLOWED_SPREAD_BPS` | 3.0 bps | Reject `spread_too_wide` if exceeded |
| Expected edge | `FXSTACK_MIN_EXPECTED_EDGE_BPS` (rescue margin `FXSTACK_MIN_EXPECTED_EDGE_RESCUE_MARGIN_BPS`) | 3.0 / 0.5 bps | Reject `edge_below_hurdle` if edge falls below hurdle by more than the rescue margin |
| Swing probability | `FXSTACK_MIN_SWING_PROB` | 0.58 | Reject `low_swing_prob` |
| Entry probability | `FXSTACK_MIN_ENTRY_PROB` | 0.62 | Reject `low_entry_prob` |
| Trade probability | `FXSTACK_MIN_TRADE_PROB` | 0.60 | Reject `low_trade_prob` |
| Model intelligence score | computed, logged only | n/a | Recorded for observability, no entry block |

### Portfolio and position limits

| Limit | Env var | Default | Where enforced |
|---|---|---|---|
| Max total open positions | `FXSTACK_MAX_TOTAL_POSITIONS` | 6 | `risk/kernel.py` |
| Max positions per pair | `FXSTACK_MAX_PAIR_POSITIONS` | 1 | `risk/kernel.py` |
| Default order lots | `FXSTACK_DEFAULT_ORDER_LOTS` | 0.10 | `risk/kernel.py:_round_lots` |
| Min order lots | `FXSTACK_MIN_ORDER_LOTS` | 0.01 | `risk/kernel.py:_round_lots` |
| Lot step | `FXSTACK_ORDER_LOT_STEP` | 0.01 | `risk/kernel.py:_round_lots` |

### Key knobs

```
# Gating
FXSTACK_MIN_SWING_PROB=0.58
FXSTACK_MIN_ENTRY_PROB=0.62
FXSTACK_MIN_TRADE_PROB=0.60
FXSTACK_MAX_ALLOWED_SPREAD_BPS=3.0
FXSTACK_MIN_EXPECTED_EDGE_BPS=3.0

# Position caps
FXSTACK_MAX_TOTAL_POSITIONS=6
FXSTACK_MAX_PAIR_POSITIONS=1
FXSTACK_DEFAULT_ORDER_LOTS=0.10

# Portfolio correlation
FXSTACK_CAPITAL_MAX_REALIZED_CORR_SHARE=0.75
FXSTACK_PORTFOLIO_REALIZED_CORR_WINDOW_BARS=96

# Bridge
MT4_BRIDGE_URL=http://127.0.0.1:58710
FXSTACK_BRIDGE_API_KEY=<set-a-secret>
FXSTACK_BRIDGE_AUTH_REQUIRED=true   # Production default. Set "false" only for loopback dev/test.
```

## Monitoring

The dashboard at `http://127.0.0.1:3000` reads `/api/trading/state` as a truth first adapter for bridge heartbeat, tick freshness, equity visibility, and live signal visibility. It surfaces connection status, live equity and profit, active decisions, the equity curve, signal metrics, and an activity log. AI training telemetry stays observe only and has no execution authority.

## Documentation

- [Agent entry point](AGENTS.md) and the [agent docs index](docs/agents/README.md)
- [Machine system map](docs/agents/system-map.yaml)
- [Runtime loop](docs/agents/runtime-loop.md)
- [Bridge and API handshakes](docs/agents/bridge-and-api-handshakes.md)
- [Model stack and feature flow](docs/agents/model-stack-and-feature-flow.md)
- [Twin vs prod parity](docs/agents/twin-vs-prod-parity.md)
- [Operator plane](docs/agents/operator-plane.md)
- [Ops entrypoints](docs/agents/ops-entrypoints.md)
- [IG MT4 setup](docs/IG_MT4_SETUP.md)
- [Safety guide](SAFETY.md)
- [Security policy](SECURITY.md)
- [Disclaimer](DISCLAIMER.md)
- [Public release checklist](docs/PUBLIC_RELEASE_CHECKLIST.md)

## Project Structure

```
Trading Agent/
├── fx-quant-stack/    # v2 models, runtime, api, training, strategy, belief, improve, llm
├── src/trader/        # unified CLI and DB shim
├── ops/               # Windows and WSL orchestration workflows
├── tools/             # backtest, digital twin, audit, and nav-graph helpers
├── app/, components/  # Next.js dashboard
├── services/          # operator plane and MCP supervisory servers
├── docs/agents/       # agent navigation graph
└── MQL4/              # MT4 EA and utility scripts
```

## Testing on Demo

Always validate on a demo account before going live. Use your IG demo server in MT4, start with conservative equity, and let it run.

```bash
launch_all.bat live 1000
```

Run for 24 to 48 hours and confirm that signals generate correctly, trades execute at the expected lot size, take profits hit as designed, the basket closes at its target, and the stack stays healthy. See [VALIDATION_CHECKLIST.md](VALIDATION_CHECKLIST.md) for the full checklist.

## License and Disclaimer

This project is for educational and research purposes only. It is not financial advice, it carries no guarantee of profitability, and past performance does not predict future results. Test thoroughly on a demo account before any live use, and consult a licensed financial advisor. By using this software you accept all associated risks.

Keep live account numbers, broker references, and `.env` contents out of the repository. Use environment variables and a gitignored `.env` for anything account specific.

## Support

- MT4 setup: [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)
- IG MT4: https://www.ig.com/en/mt4
