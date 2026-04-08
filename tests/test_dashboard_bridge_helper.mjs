import test from "node:test"
import assert from "node:assert/strict"

import { fetchBridgeJson, resolveBridgeBaseUrls } from "../lib/server/bridge.ts"

test("resolveBridgeBaseUrls prefers explicit bridge env and current bridge ports", () => {
  const original = {
    BRIDGE_URL: process.env.BRIDGE_URL,
    MT4_BRIDGE_URL: process.env.MT4_BRIDGE_URL,
    TRADER_BRIDGE_URL: process.env.TRADER_BRIDGE_URL,
    BRIDGE_PORT: process.env.BRIDGE_PORT,
    TRADER_BRIDGE_PORT: process.env.TRADER_BRIDGE_PORT,
    FXSTACK_CANDIDATE_BRIDGE_PORT: process.env.FXSTACK_CANDIDATE_BRIDGE_PORT,
  }

  try {
    process.env.BRIDGE_URL = "http://127.0.0.1:9000/"
    process.env.MT4_BRIDGE_URL = "http://127.0.0.1:9001/"
    process.env.TRADER_BRIDGE_URL = "http://127.0.0.1:9002/"
    process.env.BRIDGE_PORT = "9003"
    process.env.TRADER_BRIDGE_PORT = "9004"
    process.env.FXSTACK_CANDIDATE_BRIDGE_PORT = "9005"

    assert.deepEqual(resolveBridgeBaseUrls(), [
      "http://127.0.0.1:9000",
      "http://127.0.0.1:9001",
      "http://127.0.0.1:9002",
      "http://127.0.0.1:9003",
      "http://127.0.0.1:9004",
      "http://127.0.0.1:9005",
      "http://127.0.0.1:58710",
    ])
  } finally {
    for (const [key, value] of Object.entries(original)) {
      if (value === undefined) {
        delete process.env[key]
      } else {
        process.env[key] = value
      }
    }
  }
})

test("fetchBridgeJson falls back across bridge bases and paths", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      attempts.push(url)
      if (url === "http://127.0.0.1:9001/v2/state") {
        return new Response(JSON.stringify({ status: "ok", source: "state" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      if (url === "http://127.0.0.1:9001/v2/ready") {
        return new Response(JSON.stringify({ status: "ok", source: "ready" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      return new Response("bridge down", { status: 503 })
    }

    const payload = await fetchBridgeJson(
      ["/v2/state", "/v2/ready"],
      ["http://127.0.0.1:9000", "http://127.0.0.1:9001"],
    )

    assert.equal(payload.source, "state")
    assert.deepEqual(attempts, [
      "http://127.0.0.1:9000/v2/state",
      "http://127.0.0.1:9001/v2/state",
    ])
  } finally {
    globalThis.fetch = originalFetch
  }
})
