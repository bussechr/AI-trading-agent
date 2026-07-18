import test from "node:test"
import assert from "node:assert/strict"

import {
  formatAgeSeconds,
  formatFiniteNumber,
  formatNonNegativeInteger,
  formatRatioPercent,
  formatSignedBps,
} from "../lib/trading/live-state.ts"

test("missing or invalid ages never render as current", () => {
  assert.equal(formatAgeSeconds(null), "n/a")
  assert.equal(formatAgeSeconds(undefined), "n/a")
  assert.equal(formatAgeSeconds(-1), "n/a")
  assert.equal(formatAgeSeconds(Number.POSITIVE_INFINITY), "n/a")
  assert.equal(formatAgeSeconds(0), "0s ago")
  assert.equal(formatAgeSeconds(61.9), "1m ago")
})

test("status numeric formatters reject non-finite telemetry", () => {
  assert.equal(formatFiniteNumber("bad"), "n/a")
  assert.equal(formatFiniteNumber(null), "n/a")
  assert.equal(formatRatioPercent(Number.NaN), "n/a")
  assert.equal(formatRatioPercent(null), "n/a")
  assert.equal(formatNonNegativeInteger(-2), "n/a")
  assert.equal(formatSignedBps(Number.POSITIVE_INFINITY), "n/a")
  assert.equal(formatRatioPercent(0.975, 1), "97.5%")
  assert.equal(formatSignedBps(1.25), "+1.25 bps")
})
