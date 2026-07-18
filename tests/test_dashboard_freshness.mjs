import test from "node:test"
import assert from "node:assert/strict"

import {
  ageSecsFromTimestamp,
  normalizeAgeSecs,
  timestampToMs,
} from "../lib/trading/freshness.ts"

test("freshness timestamps normalize seconds, milliseconds, and naive UTC", () => {
  const expected = Date.parse("2026-04-01T12:00:00Z")
  assert.equal(timestampToMs(expected / 1000), expected)
  assert.equal(timestampToMs(String(expected)), expected)
  assert.equal(timestampToMs("2026-04-01T12:00:00"), expected)
})

test("freshness rejects non-finite, negative, and far-future ages", () => {
  const now = Date.parse("2026-04-01T12:00:00Z")
  assert.equal(normalizeAgeSecs(-1), null)
  assert.equal(normalizeAgeSecs(Number.POSITIVE_INFINITY), null)
  assert.equal(ageSecsFromTimestamp("2026-04-01T12:00:30Z", now), null)
  assert.equal(ageSecsFromTimestamp("2026-04-01T11:59:50Z", now), 10)
  assert.equal(timestampToMs(Number.NaN), 0)
})
