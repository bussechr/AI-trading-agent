import test from "node:test"
import assert from "node:assert/strict"

import { mergeClosedTradePayload } from "../lib/trading/closed-trades-normalize.ts"

const OLD_TRADE = {
  ticket: 1,
  symbol: "EURUSD",
  broker_symbol: "EURUSD",
  side: "BUY",
  type: 0,
  lots: 0.1,
  open_price: 1.1,
  close_price: 1.2,
  open_time: "2026-07-18T10:00:00Z",
  close_time: "2026-07-18T11:00:00Z",
  close_time_epoch: 1_752_836_400,
  profit: 10,
  swap: 0,
  commission: -1,
  net_profit: 9,
  duration_secs: 3600,
  report_ts: 1_752_836_400,
}

const OLD_SUMMARY = {
  closedTrades: 1,
  wins: 1,
  losses: 0,
  winRate: 100,
  realizedNet: 9,
  averageNet: 9,
  closedTrades24h: 1,
  wins24h: 1,
  losses24h: 0,
  winRate24h: 100,
  realizedNet24h: 9,
  averageNet24h: 9,
}

const CURRENT = { trades: [OLD_TRADE], summary: OLD_SUMMARY }

test("valid closed-trade success replaces the cached snapshot", () => {
  const nextTrade = { ...OLD_TRADE, ticket: 2, net_profit: -3 }
  const result = mergeClosedTradePayload(CURRENT, {
    status: "success",
    trades: [nextTrade],
    summary: { ...OLD_SUMMARY, realizedNet: -3 },
  })

  assert.equal(result.error, null)
  assert.equal(result.trades[0].ticket, 2)
  assert.equal(result.summary.realizedNet, -3)
})

test("null or non-finite trade members retain the last-good snapshot", () => {
  for (const trades of [[null], [{ ...OLD_TRADE, lots: Number.NaN }]]) {
    const result = mergeClosedTradePayload(CURRENT, {
      status: "success",
      trades,
      summary: OLD_SUMMARY,
    })
    assert.match(result.error || "", /malformed trade records/)
    assert.deepEqual(result.trades, CURRENT.trades)
    assert.deepEqual(result.summary, CURRENT.summary)
  }
})

test("a malformed summary retains both last-good trades and summary", () => {
  const result = mergeClosedTradePayload(CURRENT, {
    status: "success",
    trades: [{ ...OLD_TRADE, ticket: 2 }],
    summary: { ...OLD_SUMMARY, wins: "1" },
  })

  assert.match(result.error || "", /malformed summary/)
  assert.deepEqual(result, {
    ...CURRENT,
    error: result.error,
  })
})

test("HTTP failures preserve closed-trade last-good data", () => {
  const result = mergeClosedTradePayload(CURRENT, { status: "error", error: "bridge timeout" }, false)

  assert.equal(result.error, "bridge timeout")
  assert.deepEqual(result.trades, CURRENT.trades)
  assert.deepEqual(result.summary, CURRENT.summary)
})
