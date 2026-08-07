export interface ClosedTradeHistoryLike<TTrade = Record<string, any>, TSummary = Record<string, any>> {
  trades: TTrade[]
  summary: TSummary
  bridgeUrl: string | null
}

export interface ClosedTradeMergeResult<TTrade = Record<string, any>, TSummary = Record<string, any>>
  extends ClosedTradeHistoryLike<TTrade, TSummary> {
  error: string | null
}

export interface ClosedTradeAggregateSummary {
  closedTrades: number
  wins: number
  losses: number
  winRate: number | null
  realizedNet: number
  averageNet: number | null
  closedTrades24h: number
  wins24h: number
  losses24h: number
  winRate24h: number | null
  realizedNet24h: number
  averageNet24h: number | null
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

function nearlyEqual(left: number, right: number): boolean {
  const scale = Math.max(1, Math.abs(left), Math.abs(right))
  return Math.abs(left - right) <= scale * 1e-12
}

function aggregateMathMatches(
  total: number,
  wins: number,
  realizedNet: number,
  winRate: number | null,
  averageNet: number | null,
): boolean {
  if (total === 0) return winRate === null && averageNet === null
  return (
    winRate !== null &&
    averageNet !== null &&
    nearlyEqual(winRate, (wins / total) * 100) &&
    nearlyEqual(averageNet, realizedNet / total)
  )
}

function finiteNumberOrZero(value: unknown): number {
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : 0
}

export function summarizeClosedTrades(
  trades: readonly Record<string, any>[],
  isInLast24Hours: (trade: Record<string, any>) => boolean,
): ClosedTradeAggregateSummary {
  let wins = 0
  let losses = 0
  let realizedNet = 0
  let closedTrades24h = 0
  let wins24h = 0
  let losses24h = 0
  let realizedNet24h = 0

  for (const trade of trades) {
    const net = finiteNumberOrZero(trade.net_profit)
    realizedNet += net
    if (net > 0) wins += 1
    else if (net < 0) losses += 1

    if (isInLast24Hours(trade)) {
      closedTrades24h += 1
      realizedNet24h += net
      if (net > 0) wins24h += 1
      else if (net < 0) losses24h += 1
    }
  }

  const closedTrades = trades.length
  return {
    closedTrades,
    wins,
    losses,
    winRate: closedTrades > 0 ? (wins / closedTrades) * 100 : null,
    realizedNet,
    averageNet: closedTrades > 0 ? realizedNet / closedTrades : null,
    closedTrades24h,
    wins24h,
    losses24h,
    winRate24h: closedTrades24h > 0 ? (wins24h / closedTrades24h) * 100 : null,
    realizedNet24h,
    averageNet24h: closedTrades24h > 0 ? realizedNet24h / closedTrades24h : null,
  }
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
  return (
    value.wins + value.losses <= value.closedTrades &&
    value.closedTrades24h <= value.closedTrades &&
    value.wins24h + value.losses24h <= value.closedTrades24h &&
    value.wins24h <= value.wins &&
    value.losses24h <= value.losses &&
    (value.winRate === null || (value.winRate >= 0 && value.winRate <= 100)) &&
    (value.winRate24h === null || (value.winRate24h >= 0 && value.winRate24h <= 100)) &&
    aggregateMathMatches(
      value.closedTrades,
      value.wins,
      value.realizedNet,
      value.winRate,
      value.averageNet,
    ) &&
    aggregateMathMatches(
      value.closedTrades24h,
      value.wins24h,
      value.realizedNet24h,
      value.winRate24h,
      value.averageNet24h,
    )
  )
}

export function mergeClosedTradePayload<TTrade, TSummary>(
  current: ClosedTradeHistoryLike<TTrade, TSummary>,
  payload: any,
  responseOk: boolean,
  emptyForNewSource: ClosedTradeHistoryLike<TTrade, TSummary>,
): ClosedTradeMergeResult<TTrade, TSummary> {
  if (!responseOk || payload?.status !== "success") {
    return {
      ...current,
      error: String(payload?.error || "Closed-trade history unavailable"),
    }
  }

  const bridgeUrl = typeof payload?.bridgeUrl === "string"
    ? payload.bridgeUrl.trim().replace(/\/+$/, "")
    : ""
  if (!bridgeUrl) {
    return {
      ...current,
      error: "Closed-trade history returned malformed source metadata",
    }
  }

  const currentSource = String(current.bridgeUrl || "").trim().replace(/\/+$/, "")
  const retainsCurrentSource = Boolean(currentSource && currentSource === bridgeUrl)
  const retentionBase = retainsCurrentSource
    ? current
    : { ...emptyForNewSource, bridgeUrl }
  const malformedSuffix = retainsCurrentSource
    ? "retained last known good data"
    : "did not carry data across bridge sources"

  if (!Array.isArray(payload?.trades) || !payload.trades.every((trade: unknown) => isClosedTradeRecord(trade))) {
    return {
      ...retentionBase,
      error: `Closed-trade history returned malformed trade records; ${malformedSuffix}`,
    }
  }
  if (!isClosedTradeSummary(payload?.summary)) {
    return {
      ...retentionBase,
      error: `Closed-trade history returned a malformed summary; ${malformedSuffix}`,
    }
  }

  return {
    trades: payload.trades as TTrade[],
    summary: payload.summary as TSummary,
    bridgeUrl,
    error: null,
  }
}
