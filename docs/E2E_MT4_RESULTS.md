# E2E MT4 Test Results (2026-06-05)

Real end-to-end back + forward results through the MT4 path, run offline against the
on-disk Dukascopy features and the 18-pair trained model stack
(`artifacts_shadow/full_20260323`, manifest `artifacts/active_models.json`).
These are small, bounded windows — directional evidence the pipeline works end to
end, **not** statistically robust performance claims.

## Backward — model-driven signal backtest (9-pair basket, ≤5000 M5 rows/pair)

`tools/fxstack_full_backtest.py` scoring real models, MT4 cost policy (`fxstack_policy_v1`):

- 9/9 pairs OK, **920 cost-positive entries**, **mean net edge +14.16 bps**, all metrics finite.
- Per pair (trades @ mean net edge): USDJPY 223@20.6, NZDUSD 214@13.6, AUDUSD 198@13.7,
  USDCHF 184@16.0, EURJPY 46@18.0, EURUSD 31@19.8, USDCAD 19@16.3, EURGBP 5@9.5,
  **GBPUSD 0** (every setup rejected by the gates).

Historical figures from the removed duplicate lifecycle-replay implementation are not
retained as software-validation evidence. Strategy economics now come from physically
isolated causal research; software behavior is proved with the actual runtime.

## Software validation — live MT4 bridge lifecycle (headless mock EA)

Bridge on SQLite + `tools/mock_mt4_ea.py` driving the real v2 protocol:

- handshake `v2.1.0` ✓; **116 ticks**, **15 heartbeats**, 0 errors over a 16s run;
  injected BUY command → `queued → delivered → acked`; bridge `status=ok`,
  `tick_status=fresh`, `heartbeat_age≈0.6s`, `trades_executed=1`.
- `forward_test_passed=true`.

## How to reproduce

See [E2E_MT4_TESTING.md](E2E_MT4_TESTING.md). Causal research runs are bounded by window
(`--start-ts/--end-ts`) and require the multi-pair basket for cross-pair features.
Research artifacts are written only beneath the isolated research output root and are
not committed or imported into production automatically.
