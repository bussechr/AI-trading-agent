import test from "node:test"
import assert from "node:assert/strict"

import {
  mergeClosedTradePayload,
  summarizeClosedTrades,
} from "../lib/trading/closed-trades-normalize.ts"

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

const CURRENT = {
  trades: [OLD_TRADE],
  summary: OLD_SUMMARY,
  bridgeUrl: "http://127.0.0.1:58710",
}

const EMPTY = {
  trades: [],
  summary: {
    ...OLD_SUMMARY,
    closedTrades: 0,
    wins: 0,
    losses: 0,
    winRate: null,
    realizedNet: 0,
    averageNet: null,
    closedTrades24h: 0,
    wins24h: 0,
    losses24h: 0,
    winRate24h: null,
    realizedNet24h: 0,
    averageNet24h: null,
  },
  bridgeUrl: null,
}

test("closed-trade summaries count breakeven records in totals and averages", () => {
  const summary = summarizeClosedTrades(
    [
      { net_profit: 12, recent: true },
      { net_profit: -3, recent: false },
      { net_profit: 0, recent: true },
    ],
    (trade) => trade.recent,
  )

  assert.deepEqual(summary, {
    closedTrades: 3,
    wins: 1,
    losses: 1,
    winRate: (1 / 3) * 100,
    realizedNet: 9,
    averageNet: 3,
    closedTrades24h: 2,
    wins24h: 1,
    losses24h: 0,
    winRate24h: 50,
    realizedNet24h: 12,
    averageNet24h: 6,
  })
})

test("valid closed-trade success replaces the cached snapshot", () => {
  const nextTrade = { ...OLD_TRADE, ticket: 2, net_profit: -3 }
  const result = mergeClosedTradePayload(
    CURRENT,
    {
      status: "success",
      bridgeUrl: "http://127.0.0.1:58710/",
      trades: [nextTrade],
      summary: {
        ...OLD_SUMMARY,
        wins: 0,
        losses: 1,
        winRate: 0,
        realizedNet: -3,
        averageNet: -3,
        wins24h: 0,
        losses24h: 1,
        winRate24h: 0,
        realizedNet24h: -3,
        averageNet24h: -3,
      },
    },
    true,
    EMPTY,
  )

  assert.equal(result.error, null)
  assert.equal(result.trades[0].ticket, 2)
  assert.equal(result.summary.realizedNet, -3)
  assert.equal(result.bridgeUrl, "http://127.0.0.1:58710")
})

test("null or non-finite trade members retain the last-good snapshot", () => {
  for (const trades of [[null], [{ ...OLD_TRADE, lots: Number.NaN }]]) {
    const result = mergeClosedTradePayload(
      CURRENT,
      {
        status: "success",
        bridgeUrl: CURRENT.bridgeUrl,
        trades,
        summary: OLD_SUMMARY,
      },
      true,
      EMPTY,
    )
    assert.match(result.error || "", /malformed trade records/)
    assert.deepEqual(result.trades, CURRENT.trades)
    assert.deepEqual(result.summary, CURRENT.summary)
  }
})

test("a malformed summary retains both last-good trades and summary", () => {
  const result = mergeClosedTradePayload(
    CURRENT,
    {
      status: "success",
      bridgeUrl: CURRENT.bridgeUrl,
      trades: [{ ...OLD_TRADE, ticket: 2 }],
      summary: { ...OLD_SUMMARY, wins: "1" },
    },
    true,
    EMPTY,
  )

  assert.match(result.error || "", /malformed summary/)
  assert.deepEqual(result, {
    ...CURRENT,
    error: result.error,
  })
})

test("internally impossible summary counts retain the last-good snapshot", () => {
  for (const summary of [
    { ...OLD_SUMMARY, closedTrades: 1, wins: 1, losses: 1 },
    { ...OLD_SUMMARY, closedTrades24h: 2 },
    { ...OLD_SUMMARY, wins24h: 2 },
    { ...OLD_SUMMARY, winRate: 101 },
    { ...OLD_SUMMARY, averageNet: 8 },
    { ...EMPTY.summary, winRate: 0 },
  ]) {
    const result = mergeClosedTradePayload(
      CURRENT,
      {
        status: "success",
        bridgeUrl: CURRENT.bridgeUrl,
        trades: [{ ...OLD_TRADE, ticket: 2 }],
        summary,
      },
      true,
      EMPTY,
    )

    assert.match(result.error || "", /malformed summary/)
    assert.deepEqual(result.trades, CURRENT.trades)
    assert.deepEqual(result.summary, CURRENT.summary)
  }
})

test("HTTP failures preserve closed-trade last-good data", () => {
  const result = mergeClosedTradePayload(CURRENT, { status: "error", error: "bridge timeout" }, false, EMPTY)

  assert.equal(result.error, "bridge timeout")
  assert.deepEqual(result.trades, CURRENT.trades)
  assert.deepEqual(result.summary, CURRENT.summary)
})

test("malformed data from a new bridge never inherits the previous bridge snapshot", () => {
  const result = mergeClosedTradePayload(
    CURRENT,
    {
      status: "success",
      bridgeUrl: "http://127.0.0.1:58711",
      trades: [null],
      summary: OLD_SUMMARY,
    },
    true,
    EMPTY,
  )

  assert.match(result.error || "", /did not carry data across bridge sources/)
  assert.deepEqual(result.trades, [])
  assert.deepEqual(result.summary, EMPTY.summary)
  assert.equal(result.bridgeUrl, "http://127.0.0.1:58711")
})

test("missing or non-string bridge identity cannot replace or re-label cached data", () => {
  for (const bridgeUrl of [undefined, 58711]) {
    const result = mergeClosedTradePayload(
      CURRENT,
      {
        status: "success",
        bridgeUrl,
        trades: [{ ...OLD_TRADE, ticket: 2 }],
        summary: OLD_SUMMARY,
      },
      true,
      EMPTY,
    )

    assert.match(result.error || "", /malformed source metadata/)
    assert.deepEqual(result.trades, CURRENT.trades)
    assert.equal(result.bridgeUrl, CURRENT.bridgeUrl)
  }
})
