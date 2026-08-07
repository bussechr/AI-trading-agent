export const BRIDGE_STATUS_TIERS = [
  "bridge_down",
  "bridge_up_db_unhealthy",
  "bridge_up_mt4_stale",
  "bridge_up_runtime_stale",
  "bridge_up_runtime_starting",
  "bridge_up_runtime_stalled",
  "bridge_up_runtime_failed",
  "bridge_up_runtime_ready_mt4_stale",
  "bridge_up_mt4_live",
] as const

export type BridgeStatusTier = (typeof BRIDGE_STATUS_TIERS)[number]

export interface LiveStateEnvelopeValidation {
  ok: boolean
  data: Record<string, unknown> | null
  error: string | null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null
  return value as Record<string, unknown>
}

export function validateLiveStateEnvelope(payload: unknown, responseOk: boolean): LiveStateEnvelopeValidation {
  const envelope = asRecord(payload)
  const data = asRecord(envelope?.data)
  const statusTier = String(data?.statusTier || "")
  const bridgeState = String(data?.bridgeState || "")
  const validSuccess = Boolean(
    responseOk &&
      envelope?.status === "success" &&
      data &&
      typeof data.isRunning === "boolean" &&
      typeof data.databaseOk === "boolean" &&
      typeof data.signalDataFresh === "boolean" &&
      (bridgeState === "bridge_up" || bridgeState === "bridge_down") &&
      (BRIDGE_STATUS_TIERS as readonly string[]).includes(statusTier) &&
      Array.isArray(data.agentDecisions),
  )

  if (validSuccess) return { ok: true, data, error: null }
  const reportedError = String(envelope?.error || "").trim()
  return {
    ok: false,
    data,
    error: reportedError || (responseOk ? "Malformed live-state response" : "Live-state request failed"),
  }
}

interface BridgeStatusContext {
  databaseOk?: boolean | null
  mt4Fresh: boolean
  ticksFresh: boolean
  runtimeSignalFresh: boolean
  runtimeStatus: string
}

export interface LiveRunningContext {
  databaseOk: boolean
  mt4Connected: boolean
  mt4Fresh: boolean
  ticksFresh: boolean
  runtimeSignalFresh: boolean
}

export function isLiveStateRunning(context: LiveRunningContext): boolean {
  return Boolean(
    context.databaseOk &&
      context.mt4Connected &&
      context.mt4Fresh &&
      context.ticksFresh &&
      context.runtimeSignalFresh,
  )
}

export function normalizeBridgeStatusTier(raw: unknown, context: BridgeStatusContext): BridgeStatusTier {
  const reported = String(raw || "").trim().toLowerCase()
  const runtimeStatus = String(context.runtimeStatus || "").trim().toLowerCase()

  if (context.databaseOk === false || reported === "bridge_up_db_unhealthy") {
    return "bridge_up_db_unhealthy"
  }
  if (runtimeStatus === "failed" || reported === "bridge_up_runtime_failed") {
    return "bridge_up_runtime_failed"
  }
  if (runtimeStatus === "stalled" || reported === "bridge_up_runtime_stalled") {
    return "bridge_up_runtime_stalled"
  }
  if (runtimeStatus === "starting" || reported === "bridge_up_runtime_starting") {
    return "bridge_up_runtime_starting"
  }
  if (context.mt4Fresh && context.ticksFresh) {
    return context.runtimeSignalFresh ? "bridge_up_mt4_live" : "bridge_up_runtime_stale"
  }
  return context.runtimeSignalFresh ? "bridge_up_runtime_ready_mt4_stale" : "bridge_up_mt4_stale"
}
