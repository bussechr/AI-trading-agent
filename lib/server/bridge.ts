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
// dashboard logs a warning to the server console; it does not refuse to serve.
export const BRIDGE_EXPECTED_PROTOCOL_VERSION = "v2.1.0"

const HANDSHAKE_TIMEOUT_MS = 5000

let _bridgeHandshakePromise: Promise<void> | null = null

async function _verifyBridgeHandshakeOnce(baseUrl: string): Promise<void> {
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
      return
    }
    const data = (await response.json()) as { protocol_version?: string; build?: string }
    const bridgeVersion = String(data.protocol_version || "")
    if (bridgeVersion && bridgeVersion !== BRIDGE_EXPECTED_PROTOCOL_VERSION) {
      console.warn(
        `[bridge] protocol mismatch: dashboard expects ${BRIDGE_EXPECTED_PROTOCOL_VERSION} ` +
          `but bridge ${baseUrl} reports ${bridgeVersion}`,
      )
    } else if (bridgeVersion) {
      console.info(
        `[bridge] handshake OK protocol=${bridgeVersion} build=${String(data.build || "dev")}`,
      )
    }
  } catch (err: unknown) {
    const reason = err instanceof Error ? err.message : String(err)
    console.warn(`[bridge] handshake unreachable from ${baseUrl}: ${reason}`)
  } finally {
    clearTimeout(timer)
  }
}

// Memoized so the handshake fires exactly once per Node process lifetime.
export function ensureBridgeHandshakeChecked(baseUrl: string): Promise<void> {
  if (!_bridgeHandshakePromise) {
    _bridgeHandshakePromise = _verifyBridgeHandshakeOnce(baseUrl)
  }
  return _bridgeHandshakePromise
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

// AGENT HANDSHAKE: Tries multiple bridge paths in order so dashboard routes can degrade cleanly across equivalent bridge endpoints.
export async function fetchBridgeJson(paths: string[], baseUrls: string[] = resolveBridgeBaseUrls()): Promise<any> {
  let lastError: Error | null = null

  const apiKey = process.env.FXSTACK_BRIDGE_API_KEY || ""
  const headers: Record<string, string> = {}
  if (apiKey) {
    headers["X-API-Key"] = apiKey
  }

  // Fire the once-per-process protocol-version handshake against the first
  // candidate base URL. Awaited so the warning, if any, lands on the server
  // console before the first downstream request — but with a hard timeout so
  // a slow/unresponsive bridge never blocks a real request.
  const firstBase = baseUrls.find((value) => Boolean(normalizeBridgeUrl(value)))
  if (firstBase) {
    try {
      await ensureBridgeHandshakeChecked(normalizeBridgeUrl(firstBase) as string)
    } catch {
      // Handshake errors are already logged inside ensureBridgeHandshakeChecked.
    }
  }

  const normalizedPaths = Array.from(new Set(paths.map((path) => String(path || "").trim()).filter(Boolean)))
  for (const path of normalizedPaths) {
    const normalizedPath = path.startsWith("/") ? path : `/${path}`
    for (const baseUrl of baseUrls) {
      const normalizedBaseUrl = normalizeBridgeUrl(baseUrl)
      if (!normalizedBaseUrl) continue
      try {
        const response = await fetch(`${normalizedBaseUrl}${normalizedPath}`, {
          cache: "no-store",
          headers,
        })
        if (!response.ok) {
          lastError = new Error(`Bridge returned ${response.status} for ${normalizedPath} via ${normalizedBaseUrl}`)
          continue
        }
        return await response.json()
      } catch (error: any) {
        lastError = error instanceof Error ? error : new Error(String(error))
      }
    }
  }

  throw lastError || new Error("Bridge request failed")
}
