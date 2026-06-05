# E2E MT4 Test Results (2026-06-05)

Real end-to-end back + forward results through the MT4 path, run offline against the
on-disk Dukascopy features and the 18-pair trained model stack
(`artifacts_shadow/full_20260323`, manifest `artifacts/active_models.json`).
These are small, bounded windows — directional evidence the pipeline works end to
end, **not** statistically robust performance claims.

## Backward — model-driven signal backtest (9-pair basket, ≤5000 M5 rows/pair)

`trader backtest full` scoring real models, MT4 cost policy (`fxstack_policy_v1`):

- 9/9 pairs OK, **920 cost-positive entries**, **mean net edge +14.16 bps**, all metrics finite.
- Per pair (trades @ mean net edge): USDJPY 223@20.6, NZDUSD 214@13.6, AUDUSD 198@13.7,
  USDCHF 184@16.0, EURJPY 46@18.0, EURUSD 31@19.8, USDCAD 19@16.3, EURGBP 5@9.5,
  **GBPUSD 0** (every setup rejected by the gates).

## Backward — full MT4-parity digital twin (2024-01-16 → 2024-01-18)

Full lifecycle replay (regime → swing → intraday → meta → exit → reversal → belief
overlay → campaign → allocator), real equity simulation:

- 7 trades, 4W / 3L (**57.1% win**), net **+$1,396.07 on $10k = +13.96%**,
  profit factor **5.88**, max drawdown **−2.44%**.
- Gate activity (rejections): weak_entry 921, edge_below_hurdle 437, meta_reject 304,
  portfolio_exposure_cap 1485, session_blocked:pacific 249, spread_too_wide 55 —
  the deterministic risk/quality gates do the bulk of the filtering.

## Forward — walk-forward / out-of-sample twin (2026-01-06 → 2026-01-08)

Same models + logic on a window two years later (unseen):

- 11 trades, 4W / 7L (**36.4% win**), net **+$422.77 = +4.23%**, profit factor **2.33**,
  max drawdown **−2.41%**.

**Read:** out-of-sample the edge degrades (win rate 57% → 36%, PF 5.88 → 2.33) yet stays
net-positive, and — importantly — **max drawdown is essentially unchanged (~2.4%)**: the
deterministic risk controls behave consistently across a 2-year regime gap. This is the
"LLM/quant proposes, deterministic risk disposes" separation holding up out of sample.

## Forward — live MT4 bridge lifecycle (headless mock EA)

Bridge on SQLite + `tools/mock_mt4_ea.py` driving the real v2 protocol:

- handshake `v2.1.0` ✓; **116 ticks**, **15 heartbeats**, 0 errors over a 16s run;
  injected BUY command → `queued → delivered → acked`; bridge `status=ok`,
  `tick_status=fresh`, `heartbeat_age≈0.6s`, `trades_executed=1`.
- `forward_test_passed=true`.

## How to reproduce

See [E2E_MT4_TESTING.md](E2E_MT4_TESTING.md). Backward twin runs are bounded by window
(`--start-ts/--end-ts`) because the full-lifecycle replay is ~seconds/bar; the twin
requires the multi-pair basket for cross-pair features. Artifacts (equity curves,
trades, decision history, per-pair, rejections) are written under
`artifacts/reports/backtests/<run>/` and are not committed.
