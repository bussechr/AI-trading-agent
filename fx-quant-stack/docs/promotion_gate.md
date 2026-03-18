# Promotion Gate (Fast Profile)

Candidate cutover criteria:

1. Contract parity: all required `/v2/*` endpoints return compatible payloads.
2. Reliability: no critical command lifecycle failures (`queued -> delivered -> acked/failed` state machine intact).
3. Throughput: candidate acked-entry throughput is not materially degraded.
4. Risk: no hard drawdown/daily-breaker rule violations.

If all checks pass, switch launchers to `fxstack` runtime and retire legacy strategy modules.

Automation helper (fast gate, 15m strict):

```bash
python -m src.trader.cli scenario shadow-run -- \
  --baseline-url http://127.0.0.1:58710 \
  --candidate-url http://127.0.0.1:58711 \
  --duration-secs 900 \
  --poll-secs 2 \
  --min-throughput-delta 1 \
  --max-timeout-rate 0.05 \
  --require-nonzero-entries \
  --out-dir docs \
  --prefix canary_shadow_fast15m
```

Balanced assurance profile requires both:

1. `15m` fast gate pass (strict thresholds).
2. `24h` shadow run pass before final GO/HOLD signoff.
