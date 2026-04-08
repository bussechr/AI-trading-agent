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
