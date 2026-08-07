# End-to-End MT4 Testing — causal research + actual runtime bridge

Two complementary tests exercise the system through the **MT4 bridge path** without
needing the MT4 GUI or a broker connection. Both run fully offline.

## Backward test — physically isolated causal research

Three tools, fastest → richest:

| Tool | CLI | What it does | Needs |
|---|---|---|---|
| Cost-aware | `uv run --project fx-quant-stack python fx-quant-stack/scripts/backtest.py --pair EURUSD --timeframe M5` | feature-derived edge, no models | feature parquet |
| Model-driven | `uv run --project fx-quant-stack python tools/fxstack_full_backtest.py --pairs ... --max-rows-per-pair N` | real model scoring → entries/edge/rejections | features + active models |
| Causal research | `python tools/fxstack_causal_research_backtest.py --pairs <basket> ...` | offline lifecycle/economic research with delayed fills | immutable raw snapshot + research-only manifest |

The causal research backtest is advisory and is not production-runtime validation.
Run it on a separate host or VM without live database, bridge, credential, broker,
registry-write, or activation access. **It requires the multi-pair basket** (`--pairs EURUSD,USDJPY,
GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD`): the trained intraday model consumes
cross-pair features (`usd_strength_basket_ret_1`, `cross_pair_dispersion`) that can only
be computed across the universe — running a single pair raises `missing feature columns`.
The full-lifecycle research pass is ~seconds/bar, so bound the window (`--start-ts/--end-ts`)
for quick runs; use `tools/fxstack_full_backtest.py --max-rows-per-pair` for a faster model-driven
pass over more data.

For point-in-time walk-forward research, use `tools/run_causal_walk_forward.py` with
explicit train/test windows. It builds separate physically truncated training and replay
snapshots, trains a research-only manifest, and requires delayed execution plus passing
causal audits before economic results may be interpreted.

## Software validation — actual MT4 bridge lifecycle (headless)

Stands up the real bridge on SQLite and drives it with a **headless mock MT4 EA**
(`tools/mock_mt4_ea.py`) that speaks the exact v2 protocol — the MT4 terminal,
simulated. This exercises the full live command lifecycle end to end.

```bash
# 1. Offline SQLite DB
uv run --project fx-quant-stack python -m fxstack.runtime.db_tools migrate \
  --database-url sqlite:///./fxstack_e2e.db --allow-sqlite --project-root .

# 2. Bridge (loopback, auth off for the test)
FXSTACK_DATABASE_URL=sqlite:///./fxstack_e2e.db FXSTACK_ALLOW_SQLITE=true \
FXSTACK_BRIDGE_AUTH_REQUIRED=false FXSTACK_PAIRS=EURUSD,GBPUSD,USDJPY,AUDUSD \
FXSTACK_SKIP_STARTUP_VALIDATION=true FXSTACK_REQUIRE_ACTIVE_MODELS=false \
  uv run --project fx-quant-stack python -m uvicorn fxstack.api.app:app \
  --loop asyncio:SelectorEventLoop --host 127.0.0.1 --port 58710 &

# 3. Forward test: stream ticks + heartbeats, inject a command, verify it reaches 'acked'
python tools/mock_mt4_ea.py --bridge-url http://127.0.0.1:58710 \
  --pairs EURUSD,GBPUSD,USDJPY,AUDUSD --duration-secs 16 --inject-command BUY
```

The mock EA: `GET /v2/handshake` → loop `POST /v2/market/tick` (keeps ticks fresh),
`POST /v2/reports` (heartbeat equity), `GET /v2/commands/poll?format=line` (parse the
MT4 wire line), simulate the fill against a tiny position book, `POST /v2/commands/ack`
(status `executed` → the bridge's `acked` terminal state). With `--inject-command` it
submits a command via `/v2/commands` and verifies the lifecycle reaches `acked`; it
exits non-zero (`forward_test_passed=false`) if ticks/heartbeats fail or the command
isn't acked.

Verify the bridge view during/after a run:

```bash
curl -s localhost:58710/v2/health   # status=ok, tick_status=fresh, heartbeat_age_secs small
curl -s localhost:58710/v2/metrics  # ticks_fresh=true, commands.acked, trades_executed
curl -s "localhost:58710/v2/commands/events?command_id=<id>"  # queued → delivered → acked
```

### A representative passing run

```
forward_test_passed: true   injected_command_status: acked
stats: ticks=116 heartbeats=15 polls=30 commands=1 acks=1 opens=1 errors=0
bridge: status=ok tick_status=fresh heartbeat_age_secs≈0.6 trades_executed=1
```

The mock EA's wire parsing + fill simulation are unit-tested in
`fx-quant-stack/tests/test_mock_mt4_ea.py`; the in-process bridge happy-path is covered
by `tests/test_e2e_smoke.py`.
