import test from "node:test"
import assert from "node:assert/strict"

import { tickMidPrice } from "../lib/trading/ticks.ts"

test("two positive sides produce their midpoint", () => {
  assert.equal(tickMidPrice({ bid: 1.1, ask: 1.1002, mid: 9 }), 1.1001)
})

test("mid-only ticks never average zero placeholder sides", () => {
  assert.equal(tickMidPrice({ bid: 0, ask: 0, mid: 1.2345 }), 1.2345)
  assert.equal(tickMidPrice({ bid: null, ask: null, mid: 1.2345 }), 1.2345)
})

test("tick price normalization rejects non-positive quotes", () => {
  assert.equal(tickMidPrice({ bid: 0, ask: 0, mid: 0 }), null)
})
