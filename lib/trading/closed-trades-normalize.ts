export interface ClosedTradeHistoryLike<TTrade = Record<string, any>, TSummary = Record<string, any>> {
  trades: TTrade[]
  summary: TSummary
}

export interface ClosedTradeMergeResult<TTrade = Record<string, any>, TSummary = Record<string, any>>
  extends ClosedTradeHistoryLike<TTrade, TSummary> {
  error: string | null
}

function isRecord(value: unknown): value is Record<string, any> {
  return Boolean(value && typeof value === "object" && !Array.isArray(value))
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value)
}

function isNullableFiniteNumber(value: unknown): boolean {
  return value === null || isFiniteNumber(value)
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === "string"
}

export function isClosedTradeRecord(value: unknown): value is Record<string, any> {
  if (!isRecord(value)) return false
  for (const field of [
    "ticket",
    "type",
    "lots",
    "open_price",
    "close_price",
    "profit",
    "swap",
    "commission",
    "net_profit",
    "report_ts",
  ]) {
    if (!isFiniteNumber(value[field])) return false
  }
  return (
    typeof value.symbol === "string" &&
    typeof value.broker_symbol === "string" &&
    typeof value.side === "string" &&
    isNullableString(value.open_time) &&
    isNullableString(value.close_time) &&
    isNullableFiniteNumber(value.close_time_epoch) &&
    isNullableFiniteNumber(value.duration_secs)
  )
}

export function isClosedTradeSummary(value: unknown): value is Record<string, any> {
  if (!isRecord(value)) return false
  for (const field of ["closedTrades", "wins", "losses", "closedTrades24h", "wins24h", "losses24h"]) {
    const count = value[field]
    if (!Number.isInteger(count) || count < 0) return false
  }
  for (const field of ["realizedNet", "realizedNet24h"]) {
    if (!isFiniteNumber(value[field])) return false
  }
  for (const field of ["winRate", "averageNet", "winRate24h", "averageNet24h"]) {
    if (!isNullableFiniteNumber(value[field])) return false
  }
  return true
}

export function mergeClosedTradePayload<TTrade, TSummary>(
  current: ClosedTradeHistoryLike<TTrade, TSummary>,
  payload: any,
  responseOk = true,
): ClosedTradeMergeResult<TTrade, TSummary> {
  if (!responseOk || payload?.status !== "success") {
    return {
      ...current,
      error: String(payload?.error || "Closed-trade history unavailable"),
    }
  }

  if (!Array.isArray(payload?.trades) || !payload.trades.every((trade: unknown) => isClosedTradeRecord(trade))) {
    return {
      ...current,
      error: "Closed-trade history returned malformed trade records; retained last known good data",
    }
  }
  if (!isClosedTradeSummary(payload?.summary)) {
    return {
      ...current,
      error: "Closed-trade history returned a malformed summary; retained last known good data",
    }
  }

  return {
    trades: payload.trades as TTrade[],
    summary: payload.summary as TSummary,
    error: null,
  }
}
