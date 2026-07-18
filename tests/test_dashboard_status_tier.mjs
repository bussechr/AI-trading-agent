import test from "node:test"
import assert from "node:assert/strict"

import { isLiveStateRunning, normalizeBridgeStatusTier, validateLiveStateEnvelope } from "../lib/trading/status-tier.ts"

test("status tier cannot report live when local freshness is stale", () => {
  assert.equal(
    normalizeBridgeStatusTier("bridge_up_mt4_live", {
      databaseOk: true,
      mt4Fresh: true,
      ticksFresh: false,
      runtimeSignalFresh: true,
      runtimeStatus: "running",
    }),
    "bridge_up_runtime_ready_mt4_stale",
  )
})

test("database and runtime failures outrank transport freshness", () => {
  const fresh = {
    mt4Fresh: true,
    ticksFresh: true,
    runtimeSignalFresh: true,
    runtimeStatus: "running",
  }
  assert.equal(
    normalizeBridgeStatusTier("bridge_up_mt4_live", { ...fresh, databaseOk: false }),
    "bridge_up_db_unhealthy",
  )
  assert.equal(
    normalizeBridgeStatusTier("bridge_up_runtime_failed", { ...fresh, databaseOk: true }),
    "bridge_up_runtime_failed",
  )
})

test("live status requires all three freshness signals", () => {
  assert.equal(
    normalizeBridgeStatusTier("", {
      databaseOk: true,
      mt4Fresh: true,
      ticksFresh: true,
      runtimeSignalFresh: true,
      runtimeStatus: "running",
    }),
    "bridge_up_mt4_live",
  )
})

test("database health is a hard live-running gate", () => {
  const fresh = {
    databaseOk: true,
    mt4Connected: true,
    mt4Fresh: true,
    ticksFresh: true,
    runtimeSignalFresh: true,
  }
  assert.equal(isLiveStateRunning(fresh), true)
  assert.equal(isLiveStateRunning({ ...fresh, databaseOk: false }), false)
})

test("live-state success envelopes require the minimum safety contract", () => {
  const valid = {
    status: "success",
    data: {
      isRunning: true,
      databaseOk: true,
      signalDataFresh: true,
      bridgeState: "bridge_up",
      statusTier: "bridge_up_mt4_live",
      agentDecisions: [],
    },
  }
  assert.equal(validateLiveStateEnvelope(valid, true).ok, true)
  assert.equal(validateLiveStateEnvelope(valid, false).ok, false)
  assert.equal(
    validateLiveStateEnvelope({ ...valid, data: { ...valid.data, databaseOk: undefined } }, true).error,
    "Malformed live-state response",
  )
  assert.equal(
    validateLiveStateEnvelope({ ...valid, data: { ...valid.data, agentDecisions: null } }, true).error,
    "Malformed live-state response",
  )
})

test("live-state errors retain safe partial diagnostics for fail-closed merging", () => {
  const result = validateLiveStateEnvelope(
    { status: "error", error: "bridge timeout", data: { bridgeUrl: "http://127.0.0.1:9000" } },
    false,
  )
  assert.equal(result.ok, false)
  assert.equal(result.error, "bridge timeout")
  assert.equal(result.data?.bridgeUrl, "http://127.0.0.1:9000")
})
