import test from "node:test"
import assert from "node:assert/strict"
import { readFileSync } from "node:fs"

import { describeBridgeSource } from "../lib/trading/bridge-source.ts"

test("equivalent bridge URLs are treated as the primary source", () => {
  const source = describeBridgeSource("HTTP://127.0.0.1:58791/", "http://127.0.0.1:58791")

  assert.equal(source.isNonPrimary, false)
  assert.equal(source.endpointLabel, "127.0.0.1:58791")
})

test("a compatible fallback bridge is identified with an operator label", () => {
  const source = describeBridgeSource("http://127.0.0.1:58800", "http://127.0.0.1:58791")

  assert.equal(source.isNonPrimary, true)
  assert.equal(source.endpointLabel, "127.0.0.1:58800")
  assert.equal(source.primaryUrl, "http://127.0.0.1:58791")
})

test("dashboard chrome renders an accessible badge for a non-primary serving source", () => {
  const layout = readFileSync(new URL("../components/dashboard-layout.tsx", import.meta.url), "utf8")
  const stateRoute = readFileSync(new URL("../app/api/trading/state/route.ts", import.meta.url), "utf8")

  assert.match(layout, /bridgeSource\.isNonPrimary/)
  assert.match(layout, /role="status"/)
  assert.match(layout, /Fallback source/)
  assert.match(stateRoute, /bridgePrimaryUrl: BRIDGE_URL/)
})
