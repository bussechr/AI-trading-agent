import test from "node:test"
import assert from "node:assert/strict"

import {
  mergePinnedTradingHistorySnapshot,
  mergeTradingHistoryPayloads,
} from "../lib/trading/history-normalize.ts"

const CURRENT = {
  metrics: { previous: true },
  reports: [{ id: "old-report" }],
  commands: [{ id: "old-command" }],
  commandEvents: [{ id: "old-event" }],
  governanceEvents: [{ id: "old-governance" }],
}

test("trading history updates every successful slice", () => {
  const result = mergeTradingHistoryPayloads(CURRENT, {
    metrics: { status: "success", data: { current: true } },
    reports: { status: "success", reports: [{ id: "new-report" }] },
    commands: { status: "success", commands: [{ id: "new-command" }] },
    commandEvents: { status: "success", events: [{ id: "new-event" }] },
    governance: { status: "success", events: [{ id: "new-governance" }] },
  })

  assert.equal(result.error, null)
  assert.deepEqual(result.history.metrics, { current: true })
  assert.equal(result.history.reports[0].id, "new-report")
  assert.equal(result.history.commands[0].id, "new-command")
  assert.equal(result.history.commandEvents[0].id, "new-event")
  assert.equal(result.history.governanceEvents[0].id, "new-governance")
})

test("a degraded slice preserves its last good data and surfaces the source", () => {
  const result = mergeTradingHistoryPayloads(CURRENT, {
    metrics: { status: "success", data: { current: true } },
    reports: { status: "error", error: "bridge timeout", reports: [] },
    commands: { status: "success", commands: [{ id: "new-command" }] },
    commandEvents: { status: "success", events: [] },
    governance: { status: "success", events: [] },
  })

  assert.match(result.error || "", /reports: bridge timeout/)
  assert.deepEqual(result.history.reports, CURRENT.reports)
  assert.equal(result.history.commands[0].id, "new-command")
})

test("malformed successful payload retains cached history and surfaces every degraded source", () => {
  const result = mergeTradingHistoryPayloads(CURRENT, {
    metrics: { status: "success", data: null },
    reports: { status: "success", reports: null },
    commands: { status: "success", commands: "not-an-array" },
    commandEvents: { status: "success", events: null },
    governance: { status: "success", events: null },
  })

  assert.match(result.error || "", /metrics: malformed success payload/)
  assert.match(result.error || "", /reports: malformed success payload/)
  assert.match(result.error || "", /commands: malformed success payload/)
  assert.match(result.error || "", /command events: malformed success payload/)
  assert.match(result.error || "", /governance: malformed success payload/)
  assert.deepEqual(result.history, CURRENT)
})

test("malformed array members retain every last-good history slice", () => {
  const result = mergeTradingHistoryPayloads(CURRENT, {
    metrics: { status: "success", data: { current: true } },
    reports: { status: "success", reports: [null] },
    commands: { status: "success", commands: [{ id: "new-command" }, null] },
    commandEvents: { status: "success", events: [null] },
    governance: { status: "success", events: [null] },
  })

  assert.match(result.error || "", /reports: malformed success payload/)
  assert.match(result.error || "", /commands: malformed success payload/)
  assert.match(result.error || "", /command events: malformed success payload/)
  assert.match(result.error || "", /governance: malformed success payload/)
  assert.deepEqual(result.history.metrics, { current: true })
  assert.deepEqual(result.history.reports, CURRENT.reports)
  assert.deepEqual(result.history.commands, CURRENT.commands)
  assert.deepEqual(result.history.commandEvents, CURRENT.commandEvents)
  assert.deepEqual(result.history.governanceEvents, CURRENT.governanceEvents)
})

test("a source change never retains slices from the previous bridge", () => {
  const result = mergePinnedTradingHistorySnapshot(
    { history: CURRENT, bridgeUrl: "http://127.0.0.1:9000" },
    {
      status: "success",
      bridgeUrl: "http://127.0.0.1:9001/",
      sources: {
        metrics: { status: "success", data: { source: "candidate" } },
        reports: { status: "error", error: "candidate reports unavailable" },
        commands: { status: "success", commands: [{ id: "candidate-command" }] },
        commandEvents: { status: "success", events: [] },
        governance: { status: "success", events: [] },
      },
    },
  )

  assert.equal(result.bridgeUrl, "http://127.0.0.1:9001")
  assert.match(result.error || "", /candidate reports unavailable/)
  assert.deepEqual(result.history.metrics, { source: "candidate" })
  assert.deepEqual(result.history.reports, [])
  assert.equal(result.history.commands[0].id, "candidate-command")
})

test("a degraded slice retains last-good data when the source is unchanged", () => {
  const result = mergePinnedTradingHistorySnapshot(
    { history: CURRENT, bridgeUrl: "http://127.0.0.1:9000" },
    {
      status: "success",
      bridgeUrl: "http://127.0.0.1:9000/",
      sources: {
        metrics: { status: "success", data: { current: true } },
        reports: { status: "error", error: "report timeout" },
        commands: { status: "success", commands: [] },
        commandEvents: { status: "success", events: [] },
        governance: { status: "success", events: [] },
      },
    },
  )

  assert.match(result.error || "", /report timeout/)
  assert.deepEqual(result.history.reports, CURRENT.reports)
})
