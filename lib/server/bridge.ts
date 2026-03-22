const DEFAULT_BRIDGE_URL = "http://127.0.0.1:58710"

export const BRIDGE_URL = process.env.BRIDGE_URL || DEFAULT_BRIDGE_URL

export function parseBoundedInt(value: string | null, defaultValue: number, minValue: number, maxValue: number): number {
  const parsed = Number.parseInt(value || String(defaultValue), 10)
  if (!Number.isFinite(parsed)) return defaultValue
  return Math.min(Math.max(parsed, minValue), maxValue)
}

export async function fetchBridgeJson(paths: string[]): Promise<any> {
  let lastError: Error | null = null

  const apiKey = process.env.FXSTACK_BRIDGE_API_KEY || ""
  const headers: Record<string, string> = {}
  if (apiKey) {
    headers["X-API-Key"] = apiKey
  }

  for (const path of paths) {
    try {
      const response = await fetch(`${BRIDGE_URL}${path}`, {
        cache: "no-store",
        headers
      })
      if (!response.ok) {
        lastError = new Error(`Bridge returned ${response.status} for ${path}`)
        continue
      }
      return await response.json()
    } catch (error: any) {
      lastError = error instanceof Error ? error : new Error(String(error))
    }
  }

  throw lastError || new Error("Bridge request failed")
}
