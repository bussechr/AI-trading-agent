import test from "node:test"
import assert from "node:assert/strict"

import {
  BRIDGE_HANDSHAKE_CACHE_TTL_MS,
  ensureBridgeHandshakeChecked,
  fetchBridgeJson,
  fetchBridgeJsonBatchPinned,
  fetchBridgeJsonWithSource,
  fetchBridgeObjectWithSource,
  requireBridgeArrayField,
  requireBridgeObject,
  requireBridgeRecordArrayField,
  resolveBridgeBaseUrls,
} from "../lib/server/bridge.ts"

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

test("bridge proxy shape guards reject malformed success payloads", () => {
  assert.deepEqual(requireBridgeArrayField({ events: [] }, "events"), [])
  assert.deepEqual(requireBridgeRecordArrayField({ events: [{ id: 1 }] }, "events"), [{ id: 1 }])
  assert.deepEqual(requireBridgeObject({ value: 1 }), { value: 1 })
  assert.throws(() => requireBridgeArrayField({ events: null }, "events"), /required array field 'events'/)
  assert.throws(() => requireBridgeRecordArrayField({ events: [null] }, "events"), /only objects/)
  assert.throws(() => requireBridgeObject([], "metrics payload"), /metrics payload must be an object/)
})

test("fetchBridgeJsonWithSource falls back and returns the exact serving bridge", async () => {
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
      if (url === "http://127.0.0.1:9001/v2/handshake") {
        return new Response(JSON.stringify({ protocol_version: "v2.1.0", build: "test" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      return new Response("bridge down", { status: 503 })
    }

    const result = await fetchBridgeJsonWithSource(
      ["/v2/state", "/v2/ready"],
      ["http://127.0.0.1:9000", "http://127.0.0.1:9001"],
    )

    assert.equal(result.payload.source, "state")
    assert.equal(result.baseUrl, "http://127.0.0.1:9001")
    assert.deepEqual(attempts, [
      "http://127.0.0.1:9000/v2/handshake",
      "http://127.0.0.1:9001/v2/handshake",
      "http://127.0.0.1:9001/v2/state",
    ])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("fetchBridgeJson rejects a delayed incompatible handshake before reading state", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []
  let releaseHandshake

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      attempts.push(url)
      if (url === "http://127.0.0.1:9010/v2/handshake") {
        return await new Promise((resolve) => {
          releaseHandshake = () => resolve(new Response(JSON.stringify({ protocol_version: "v3.0.0" }), {
            status: 200,
            headers: { "content-type": "application/json" },
          }))
        })
      }
      if (url === "http://127.0.0.1:9010/v2/state") {
        return new Response(JSON.stringify({ status: "ok", source: "state" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      return new Response("bridge down", { status: 503 })
    }

    const pending = fetchBridgeJson(["/v2/state"], ["http://127.0.0.1:9010"])
    await new Promise((resolve) => setImmediate(resolve))

    assert.deepEqual(attempts, ["http://127.0.0.1:9010/v2/handshake"])
    assert.equal(typeof releaseHandshake, "function")
    releaseHandshake()
    await assert.rejects(pending, /major mismatch/)
    assert.deepEqual(attempts, ["http://127.0.0.1:9010/v2/handshake"])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("fetchBridgeJson aborts a half-open bridge and falls back", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []

  try {
    globalThis.fetch = async (input, init = {}) => {
      const url = String(input)
      attempts.push(url)
      if (url.endsWith("/v2/handshake")) {
        return new Response(JSON.stringify({ protocol_version: "v2.1.0" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      if (url === "http://127.0.0.1:9020/v2/state") {
        return await new Promise((_, reject) => {
          init.signal?.addEventListener("abort", () => reject(new Error("aborted")), { once: true })
        })
      }
      if (url === "http://127.0.0.1:9021/v2/state") {
        return new Response(JSON.stringify({ status: "ok", source: "fallback" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      return new Response("bridge down", { status: 503 })
    }

    const payload = await fetchBridgeJson(
      ["/v2/state"],
      ["http://127.0.0.1:9020", "http://127.0.0.1:9021"],
      10,
    )

    assert.equal(payload.source, "fallback")
    assert.deepEqual(attempts, [
      "http://127.0.0.1:9020/v2/handshake",
      "http://127.0.0.1:9020/v2/state",
      "http://127.0.0.1:9021/v2/handshake",
      "http://127.0.0.1:9021/v2/state",
    ])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("fetchBridgeJson rejects a bridge with an incompatible major protocol", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      attempts.push(url)
      if (url.endsWith("/v2/state")) {
        return new Response(JSON.stringify({ source: url.includes(":9031") ? "compatible" : "incompatible" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      if (url === "http://127.0.0.1:9030/v2/handshake") {
        return new Response(JSON.stringify({ protocol_version: "v3.0.0", min_compatible: "v3.0.0" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      if (url === "http://127.0.0.1:9031/v2/handshake") {
        return new Response(JSON.stringify({ protocol_version: "v2.2.0", min_compatible: "v2.0.0" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      return new Response("not found", { status: 404 })
    }

    const payload = await fetchBridgeJson(
      ["/v2/state"],
      ["http://127.0.0.1:9030", "http://127.0.0.1:9031"],
    )

    assert.equal(payload.source, "compatible")
    assert.deepEqual(attempts, [
      "http://127.0.0.1:9030/v2/handshake",
      "http://127.0.0.1:9031/v2/handshake",
      "http://127.0.0.1:9031/v2/state",
    ])
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("fetchBridgeJson rejects a bridge whose minimum excludes this dashboard", async () => {
  const originalFetch = globalThis.fetch

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      if (url.endsWith("/v2/state")) {
        return new Response(JSON.stringify({ source: url.includes(":9041") ? "fallback" : "too-old" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      const protocol = url.includes(":9040")
        ? { protocol_version: "v2.2.0", min_compatible: "v2.2.0" }
        : { protocol_version: "v2.1.1", min_compatible: "v2.0.0" }
      return new Response(JSON.stringify(protocol), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    }

    const payload = await fetchBridgeJson(
      ["/v2/state"],
      ["http://127.0.0.1:9040", "http://127.0.0.1:9041"],
    )

    assert.equal(payload.source, "fallback")
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("transient handshake failures are retried after recovery", async () => {
  const originalFetch = globalThis.fetch
  let handshakeCalls = 0

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      if (url.endsWith("/v2/state")) {
        return new Response(JSON.stringify({ source: "state" }), {
          status: 200,
          headers: { "content-type": "application/json" },
        })
      }
      handshakeCalls += 1
      if (handshakeCalls === 1) return new Response("starting", { status: 503 })
      return new Response(JSON.stringify({ protocol_version: "v2.1.0", min_compatible: "v2.0.0" }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    }

    await assert.rejects(
      fetchBridgeJson(["/v2/state"], ["http://127.0.0.1:9050"]),
      /handshake could not be verified/,
    )
    await fetchBridgeJson(["/v2/state"], ["http://127.0.0.1:9050"])

    assert.equal(handshakeCalls, 2)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("successful handshakes expire and a replacement bridge is revalidated", async () => {
  const originalFetch = globalThis.fetch
  let handshakeCalls = 0

  try {
    globalThis.fetch = async () => {
      handshakeCalls += 1
      const protocolVersion = handshakeCalls === 1 ? "v2.1.0" : "v3.0.0"
      return new Response(JSON.stringify({ protocol_version: protocolVersion }), {
        status: 200,
        headers: { "content-type": "application/json" },
      })
    }

    const baseUrl = "http://127.0.0.1:9051"
    assert.equal(await ensureBridgeHandshakeChecked(baseUrl, 1_000), true)
    assert.equal(
      await ensureBridgeHandshakeChecked(baseUrl, 1_000 + BRIDGE_HANDSHAKE_CACHE_TTL_MS - 1),
      true,
    )
    assert.equal(handshakeCalls, 1)

    await assert.rejects(
      ensureBridgeHandshakeChecked(baseUrl, 1_000 + BRIDGE_HANDSHAKE_CACHE_TTL_MS),
      /major mismatch/,
    )
    assert.equal(handshakeCalls, 2)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("dependent reads pinned to the state source never fail over to another bridge", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      attempts.push(url)
      if (url === "http://127.0.0.1:9060/v2/handshake") {
        return new Response(JSON.stringify({ protocol_version: "v2.1.0" }), { status: 200 })
      }
      if (url === "http://127.0.0.1:9060/v2/state") {
        return new Response(JSON.stringify({ source: "primary" }), { status: 200 })
      }
      if (url === "http://127.0.0.1:9060/v2/market/ticks") {
        return new Response("ticks unavailable", { status: 503 })
      }
      if (url === "http://127.0.0.1:9061/v2/market/ticks") {
        return new Response(JSON.stringify({ EURUSD: { mid: 9 } }), { status: 200 })
      }
      return new Response("not found", { status: 404 })
    }

    const state = await fetchBridgeJsonWithSource(
      ["/v2/state"],
      ["http://127.0.0.1:9060", "http://127.0.0.1:9061"],
    )
    await assert.rejects(
      fetchBridgeJson(["/v2/market/ticks"], [state.baseUrl]),
      /returned 503/,
    )

    assert.equal(state.baseUrl, "http://127.0.0.1:9060")
    assert.equal(attempts.includes("http://127.0.0.1:9061/v2/market/ticks"), false)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("pinned history batch keeps every slice on the state-serving bridge", async () => {
  const originalFetch = globalThis.fetch
  const attempts = []

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      attempts.push(url)
      if (url === "http://127.0.0.1:9080/v2/handshake") {
        return new Response(JSON.stringify({ protocol_version: "v2.1.0" }), { status: 200 })
      }
      if (url === "http://127.0.0.1:9080/v2/state") {
        return new Response(JSON.stringify({ system_status: "connected" }), { status: 200 })
      }
      if (url === "http://127.0.0.1:9080/v2/metrics") {
        return new Response(JSON.stringify({ source: "primary-metrics" }), { status: 200 })
      }
      if (url === "http://127.0.0.1:9080/v2/reports?limit=5") {
        return new Response("primary reports unavailable", { status: 503 })
      }
      if (url.startsWith("http://127.0.0.1:9081")) {
        return new Response(JSON.stringify({ reports: [{ source: "candidate" }] }), { status: 200 })
      }
      return new Response("not found", { status: 404 })
    }

    const batch = await fetchBridgeJsonBatchPinned(
      ["/v2/state"],
      [
        { key: "metrics", paths: ["/v2/metrics"] },
        { key: "reports", paths: ["/v2/reports?limit=5"] },
      ],
      ["http://127.0.0.1:9080", "http://127.0.0.1:9081"],
    )

    assert.equal(batch.baseUrl, "http://127.0.0.1:9080")
    assert.equal(batch.results.metrics.ok, true)
    assert.equal(batch.results.reports.ok, false)
    assert.equal(attempts.some((url) => url.startsWith("http://127.0.0.1:9081")), false)
  } finally {
    globalThis.fetch = originalFetch
  }
})

test("state object fetch rejects malformed successful payloads", async () => {
  const originalFetch = globalThis.fetch

  try {
    globalThis.fetch = async (input) => {
      const url = String(input)
      if (url.endsWith("/v2/handshake")) {
        return new Response(JSON.stringify({ protocol_version: "v2.1.0" }), { status: 200 })
      }
      return new Response(JSON.stringify([]), { status: 200 })
    }

    await assert.rejects(
      fetchBridgeObjectWithSource(["/v2/state"], "state payload", ["http://127.0.0.1:9070"]),
      /state payload must be an object/,
    )
  } finally {
    globalThis.fetch = originalFetch
  }
})
