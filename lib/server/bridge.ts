// AGENT: ROLE: Server-side bridge fetch helper used by dashboard API routes.
// AGENT: ENTRYPOINT: imported by `app/api/trading/*` routes.
// AGENT: PRIMARY INPUTS: bridge-relative paths and optional env overrides.
// AGENT: PRIMARY OUTPUTS: parsed bridge JSON payloads.
// AGENT: DEPENDS ON: global `fetch` and env.
// AGENT: CALLED BY: `app/api/trading/state/route.ts` and sibling dashboard routes.
// AGENT: STATE / SIDE EFFECTS: HTTP fetch only.
// AGENT: HANDSHAKES: bridge `/v2/*` route access with optional API key header.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `app/api/trading/state/route.ts` -> `docs/agents/bridge-and-api-handshakes.md`
const DEFAULT_BRIDGE_URL = "http://127.0.0.1:58710"

// AGENT HANDSHAKE: Bridge wire-protocol version this dashboard build expects.
// Keep in sync with fx-quant-stack/src/fxstack/api/wire.py::BRIDGE_PROTOCOL_VERSION
// and MQL4/Experts/BridgeEA.mq4::EA_EXPECTED_PROTOCOL_VERSION. On mismatch the
// dashboard refuses incompatible major versions and servers whose
// `min_compatible` excludes this build. Compatible minor/patch drift is logged.
export const BRIDGE_EXPECTED_PROTOCOL_VERSION = "v2.1.0"

const HANDSHAKE_TIMEOUT_MS = 5000
const BRIDGE_REQUEST_TIMEOUT_MS = 5000
export const BRIDGE_HANDSHAKE_CACHE_TTL_MS = 30_000

type BridgeHandshakeCacheEntry = {
  promise: Promise<boolean>
  checkedAtMs: number | null
}

const _bridgeHandshakeCache = new Map<string, BridgeHandshakeCacheEntry>()

export interface BridgeJsonResult<T = any> {
  payload: T
  baseUrl: string
}

export interface BridgePinnedBatchRequest {
  key: string
  paths: string[]
}

export type BridgePinnedBatchItem =
  | { ok: true; payload: unknown }
  | { ok: false; error: string }

export interface BridgePinnedBatchResult {
  baseUrl: string
  results: Record<string, BridgePinnedBatchItem>
}

type ProtocolVersion = {
  major: number
  minor: number
  patch: number
}

type BridgeHandshakePayload = {
  protocol_version?: string
  min_compatible?: string
  build?: string
}

export class BridgeProtocolCompatibilityError extends Error {
  constructor(message: string) {
    super(message)
    this.name = "BridgeProtocolCompatibilityError"
  }
}

function parseProtocolVersion(value: string): ProtocolVersion | null {
  const match = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(value || "").trim())
  if (!match) return null
  return {
    major: Number.parseInt(match[1], 10),
    minor: Number.parseInt(match[2], 10),
    patch: Number.parseInt(match[3], 10),
  }
}

function compareProtocolVersions(left: ProtocolVersion, right: ProtocolVersion): number {
  if (left.major !== right.major) return left.major - right.major
  if (left.minor !== right.minor) return left.minor - right.minor
  return left.patch - right.patch
}

export function assertBridgeProtocolCompatible(data: BridgeHandshakePayload, baseUrl: string): void {
  const expected = parseProtocolVersion(BRIDGE_EXPECTED_PROTOCOL_VERSION)
  const bridgeVersionText = String(data.protocol_version || "").trim()
  const bridgeVersion = parseProtocolVersion(bridgeVersionText)
  if (!expected || !bridgeVersion) {
    throw new BridgeProtocolCompatibilityError(
      `Bridge ${baseUrl} returned an invalid protocol version: ${bridgeVersionText || "missing"}`,
    )
  }

  if (expected.major !== bridgeVersion.major) {
    throw new BridgeProtocolCompatibilityError(
      `Bridge protocol major mismatch: dashboard expects ${BRIDGE_EXPECTED_PROTOCOL_VERSION} ` +
        `but ${baseUrl} reports ${bridgeVersionText}`,
    )
  }

  const minimumText = String(data.min_compatible || "").trim()
  if (minimumText) {
    const minimum = parseProtocolVersion(minimumText)
    if (!minimum) {
      throw new BridgeProtocolCompatibilityError(
        `Bridge ${baseUrl} returned an invalid min_compatible version: ${minimumText}`,
      )
    }
    if (compareProtocolVersions(expected, minimum) < 0) {
      throw new BridgeProtocolCompatibilityError(
        `Bridge ${baseUrl} requires protocol ${minimumText} or newer; ` +
          `dashboard is ${BRIDGE_EXPECTED_PROTOCOL_VERSION}`,
      )
    }
  }

  if (bridgeVersionText !== BRIDGE_EXPECTED_PROTOCOL_VERSION) {
    console.warn(
      `[bridge] compatible protocol drift: dashboard=${BRIDGE_EXPECTED_PROTOCOL_VERSION} ` +
        `bridge=${bridgeVersionText} base=${baseUrl}`,
    )
  }
}

async function _verifyBridgeHandshakeOnce(baseUrl: string): Promise<boolean> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), HANDSHAKE_TIMEOUT_MS)
  try {
    const response = await fetch(`${baseUrl}/v2/handshake`, {
      cache: "no-store",
      signal: controller.signal,
    })
    if (!response.ok) {
      console.warn(
        `[bridge] handshake non-200 from ${baseUrl}: ${response.status} ${response.statusText}`,
      )
      return false
    }
    const data = (await response.json()) as BridgeHandshakePayload
    const bridgeVersion = String(data.protocol_version || "")
    assertBridgeProtocolCompatible(data, baseUrl)
    if (bridgeVersion === BRIDGE_EXPECTED_PROTOCOL_VERSION) {
      console.info(
        `[bridge] handshake OK protocol=${bridgeVersion} build=${String(data.build || "dev")}`,
      )
    }
    return true
  } catch (err: unknown) {
    if (err instanceof BridgeProtocolCompatibilityError) throw err
    const reason = err instanceof Error ? err.message : String(err)
    console.warn(`[bridge] handshake unreachable from ${baseUrl}: ${reason}`)
    return false
  } finally {
    clearTimeout(timer)
  }
}

// Memoized per reachable bridge so failover does not permanently associate the
// dashboard process with an unavailable first-choice URL.
export function ensureBridgeHandshakeChecked(
  baseUrl: string,
  nowMs = Date.now(),
): Promise<boolean> {
  const normalizedBaseUrl = normalizeBridgeUrl(baseUrl)
  if (!normalizedBaseUrl) return Promise.resolve(false)

  const existing = _bridgeHandshakeCache.get(normalizedBaseUrl)
  if (
    existing &&
    (existing.checkedAtMs === null || nowMs - existing.checkedAtMs < BRIDGE_HANDSHAKE_CACHE_TTL_MS)
  ) {
    return existing.promise
  }

  const pending = _verifyBridgeHandshakeOnce(normalizedBaseUrl)
  const entry: BridgeHandshakeCacheEntry = { promise: pending, checkedAtMs: null }
  _bridgeHandshakeCache.set(normalizedBaseUrl, entry)
  void pending.then(
    (verified) => {
      if (_bridgeHandshakeCache.get(normalizedBaseUrl) !== entry) return
      if (verified) {
        entry.checkedAtMs = nowMs
      } else {
        _bridgeHandshakeCache.delete(normalizedBaseUrl)
      }
    },
    () => {
      if (_bridgeHandshakeCache.get(normalizedBaseUrl) === entry) {
        _bridgeHandshakeCache.delete(normalizedBaseUrl)
      }
    },
  )
  return pending
}

function normalizeBridgeUrl(value: string | null | undefined): string | null {
  const txt = String(value || "").trim()
  if (!txt) return null
  return txt.replace(/\/+$/, "")
}

function bridgeUrlFromPort(port: string | null | undefined): string | null {
  const parsed = Number.parseInt(String(port || ""), 10)
  if (!Number.isFinite(parsed) || parsed <= 0) return null
  return `http://127.0.0.1:${parsed}`
}

export function resolveBridgeBaseUrls(env: NodeJS.ProcessEnv = process.env): string[] {
  const candidates = [
    normalizeBridgeUrl(env.BRIDGE_URL),
    normalizeBridgeUrl(env.MT4_BRIDGE_URL),
    normalizeBridgeUrl(env.TRADER_BRIDGE_URL),
    bridgeUrlFromPort(env.BRIDGE_PORT),
    bridgeUrlFromPort(env.TRADER_BRIDGE_PORT),
    bridgeUrlFromPort(env.FXSTACK_CANDIDATE_BRIDGE_PORT),
    DEFAULT_BRIDGE_URL,
  ]
  return Array.from(new Set(candidates.filter((value): value is string => Boolean(value))))
}

export const BRIDGE_URL = resolveBridgeBaseUrls()[0] || DEFAULT_BRIDGE_URL

export function parseBoundedInt(value: string | null, defaultValue: number, minValue: number, maxValue: number): number {
  const parsed = Number.parseInt(value || String(defaultValue), 10)
  if (!Number.isFinite(parsed)) return defaultValue
  return Math.min(Math.max(parsed, minValue), maxValue)
}

export function requireBridgeObject(payload: unknown, label = "payload"): Record<string, any> {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error(`Bridge ${label} must be an object`)
  }
  return payload as Record<string, any>
}

export function requireBridgeArrayField(payload: unknown, field: string): any[] {
  const objectPayload = requireBridgeObject(payload)
  const value = objectPayload[field]
  if (!Array.isArray(value)) {
    throw new Error(`Bridge payload is missing required array field '${field}'`)
  }
  return value
}

export function requireBridgeRecordArrayField(payload: unknown, field: string): Record<string, any>[] {
  const values = requireBridgeArrayField(payload, field)
  if (values.some((value) => !value || typeof value !== "object" || Array.isArray(value))) {
    throw new Error(`Bridge array field '${field}' must contain only objects`)
  }
  return values as Record<string, any>[]
}

// AGENT HANDSHAKE: Tries multiple bridge paths in order, proves protocol
// compatibility before consuming a payload, and returns the exact serving base
// so compound callers can pin all dependent reads to one bridge instance.
export async function fetchBridgeJsonWithSource<T = any>(
  paths: string[],
  baseUrls: string[] = resolveBridgeBaseUrls(),
  requestTimeoutMs = BRIDGE_REQUEST_TIMEOUT_MS,
): Promise<BridgeJsonResult<T>> {
  let lastError: Error | null = null
  const boundedTimeoutMs = Math.max(1, Math.min(Math.floor(requestTimeoutMs), 30_000))

  const apiKey = process.env.FXSTACK_BRIDGE_API_KEY || ""
  const headers: Record<string, string> = {}
  if (apiKey) {
    headers["X-API-Key"] = apiKey
  }

  const normalizedPaths = Array.from(new Set(paths.map((path) => String(path || "").trim()).filter(Boolean)))
  for (const path of normalizedPaths) {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`
    for (const baseUrl of baseUrls) {
      const normalizedBaseUrl = normalizeBridgeUrl(baseUrl)
      if (!normalizedBaseUrl) continue

      try {
        const verified = await ensureBridgeHandshakeChecked(normalizedBaseUrl)
        if (!verified) {
          lastError = new Error(`Bridge handshake could not be verified via ${normalizedBaseUrl}`)
          continue
        }
      } catch (error: unknown) {
        lastError = error instanceof Error ? error : new Error(String(error))
        continue
      }

      const controller = new AbortController()
      const timer = setTimeout(() => controller.abort(), boundedTimeoutMs)
      try {
        const response = await fetch(`${normalizedBaseUrl}${normalizedPath}`, {
          cache: "no-store",
          headers,
          signal: controller.signal,
        })
        if (!response.ok) {
          lastError = new Error(`Bridge returned ${response.status} for ${normalizedPath} via ${normalizedBaseUrl}`)
          continue
        }
        const payload = (await response.json()) as T
        return { payload, baseUrl: normalizedBaseUrl }
      } catch (error: any) {
        lastError = controller.signal.aborted
          ? new Error(`Bridge request timed out after ${boundedTimeoutMs}ms for ${normalizedPath} via ${normalizedBaseUrl}`)
          : error instanceof Error
            ? error
            : new Error(String(error))
      } finally {
        clearTimeout(timer)
      }
    }
  }

  throw lastError || new Error("Bridge request failed")
}

export async function fetchBridgeJson<T = any>(
  paths: string[],
  baseUrls: string[] = resolveBridgeBaseUrls(),
  requestTimeoutMs = BRIDGE_REQUEST_TIMEOUT_MS,
): Promise<T> {
  const result = await fetchBridgeJsonWithSource<T>(paths, baseUrls, requestTimeoutMs)
  return result.payload
}

export async function fetchBridgeJsonBatchPinned(
  sourcePaths: string[],
  requests: readonly BridgePinnedBatchRequest[],
  baseUrls: string[] = resolveBridgeBaseUrls(),
  requestTimeoutMs = BRIDGE_REQUEST_TIMEOUT_MS,
): Promise<BridgePinnedBatchResult> {
  const source = await fetchBridgeObjectWithSource(
    sourcePaths,
    "history source payload",
    baseUrls,
    requestTimeoutMs,
  )
  const pinnedBase = [source.baseUrl]
  const items = await Promise.all(
    requests.map(async (request): Promise<[string, BridgePinnedBatchItem]> => {
      const key = String(request.key || "").trim()
      if (!key) return ["", { ok: false, error: "Pinned bridge request is missing a key" }]
      try {
        const payload = await fetchBridgeJson<unknown>(request.paths, pinnedBase, requestTimeoutMs)
        return [key, { ok: true, payload }]
      } catch (error: unknown) {
        const reason = error instanceof Error ? error.message : String(error)
        return [key, { ok: false, error: reason || "Pinned bridge request failed" }]
      }
    }),
  )

  const results: Record<string, BridgePinnedBatchItem> = {}
  for (const [key, item] of items) {
    if (key) results[key] = item
  }
  return { baseUrl: source.baseUrl, results }
}

export async function fetchBridgeObjectWithSource(
  paths: string[],
  label = "payload",
  baseUrls: string[] = resolveBridgeBaseUrls(),
  requestTimeoutMs = BRIDGE_REQUEST_TIMEOUT_MS,
): Promise<BridgeJsonResult<Record<string, any>>> {
  const result = await fetchBridgeJsonWithSource<unknown>(paths, baseUrls, requestTimeoutMs)
  return {
    payload: requireBridgeObject(result.payload, label),
    baseUrl: result.baseUrl,
  }
}
