export interface BridgeSourceDescription {
  activeUrl: string | null
  primaryUrl: string | null
  endpointLabel: string
  isNonPrimary: boolean
}

function normalizeBridgeSource(value: unknown): string | null {
  const text = String(value || "").trim()
  if (!text) return null
  try {
    const url = new URL(text)
    const path = url.pathname.replace(/\/+$/, "")
    return `${url.protocol.toLowerCase()}//${url.host.toLowerCase()}${path}`
  } catch {
    return text.replace(/\/+$/, "").toLowerCase()
  }
}

function endpointLabel(value: string | null): string {
  if (!value) return "unknown"
  try {
    return new URL(value).host || value
  } catch {
    return value
  }
}

export function describeBridgeSource(active: unknown, primary: unknown): BridgeSourceDescription {
  const activeUrl = normalizeBridgeSource(active)
  const primaryUrl = normalizeBridgeSource(primary)
  return {
    activeUrl,
    primaryUrl,
    endpointLabel: endpointLabel(activeUrl),
    isNonPrimary: Boolean(activeUrl && primaryUrl && activeUrl !== primaryUrl),
  }
}
