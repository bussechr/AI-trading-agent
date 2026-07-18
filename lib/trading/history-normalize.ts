export interface TradingHistoryLike {
  metrics: Record<string, any>
  reports: any[]
  commands: any[]
  commandEvents: any[]
  governanceEvents: any[]
}

export interface TradingHistoryPayloads {
  metrics: any
  reports: any
  commands: any
  commandEvents: any
  governance: any
}

export interface TradingHistorySnapshotLike {
  history: TradingHistoryLike
  bridgeUrl: string | null
}

export interface TradingHistoryMergeResult extends TradingHistorySnapshotLike {
  error: string | null
}

export function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value))
}

export function isRecordArray(value: unknown): value is Record<string, any>[] {
  return Array.isArray(value) && value.every((item) => isRecord(item))
}

function emptyTradingHistory(): TradingHistoryLike {
  return {
    metrics: {},
    reports: [],
    commands: [],
    commandEvents: [],
    governanceEvents: [],
  }
}

function successfulObject(payload: any, key: string, current: Record<string, any>): Record<string, any> {
  if (payload?.status !== "success") return current
  const value = payload?.[key]
  return isRecord(value) ? value : current
}

function successfulArray(payload: any, key: string, current: any[]): any[] {
  if (payload?.status !== "success") return current
  return isRecordArray(payload?.[key]) ? payload[key] : current
}

function sourceError(
  label: string,
  payload: any,
  key: string,
  expected: "object" | "array",
): string | null {
  if (payload?.status !== "success") {
    return `${label}: ${String(payload?.error || "unavailable")}`
  }
  const value = payload?.[key]
  const valid = expected === "array" ? isRecordArray(value) : isRecord(value)
  const detail = expected === "array" ? "array of objects" : "object"
  return valid ? null : `${label}: malformed success payload (expected ${detail} '${key}')`
}

export function mergeTradingHistoryPayloads(
  current: TradingHistoryLike,
  payloads: TradingHistoryPayloads,
): { history: TradingHistoryLike; error: string | null } {
  const errors = [
    sourceError("metrics", payloads.metrics, "data", "object"),
    sourceError("reports", payloads.reports, "reports", "array"),
    sourceError("commands", payloads.commands, "commands", "array"),
    sourceError("command events", payloads.commandEvents, "events", "array"),
    sourceError("governance", payloads.governance, "events", "array"),
  ].filter((error): error is string => Boolean(error))

  return {
    history: {
      metrics: successfulObject(payloads.metrics, "data", current.metrics),
      reports: successfulArray(payloads.reports, "reports", current.reports),
      commands: successfulArray(payloads.commands, "commands", current.commands),
      commandEvents: successfulArray(payloads.commandEvents, "events", current.commandEvents),
      governanceEvents: successfulArray(payloads.governance, "events", current.governanceEvents),
    },
    error: errors.length > 0 ? errors.join("; ") : null,
  }
}

export function mergePinnedTradingHistorySnapshot(
  current: TradingHistorySnapshotLike,
  payload: any,
): TradingHistoryMergeResult {
  if (payload?.status !== "success") {
    return {
      ...current,
      error: String(payload?.error || "Trading history snapshot unavailable"),
    }
  }

  const bridgeUrl = String(payload?.bridgeUrl || "").trim()
  const sources = payload?.sources
  if (!bridgeUrl || !isRecord(sources)) {
    return {
      ...current,
      error: "Trading history snapshot returned malformed source metadata",
    }
  }

  const currentSource = String(current.bridgeUrl || "").trim().replace(/\/+$/, "")
  const nextSource = bridgeUrl.replace(/\/+$/, "")
  const retentionBase = currentSource && currentSource === nextSource ? current.history : emptyTradingHistory()
  const merged = mergeTradingHistoryPayloads(retentionBase, {
    metrics: sources.metrics,
    reports: sources.reports,
    commands: sources.commands,
    commandEvents: sources.commandEvents,
    governance: sources.governance,
  })

  return {
    history: merged.history,
    bridgeUrl: nextSource,
    error: merged.error,
  }
}
