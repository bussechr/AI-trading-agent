"use client"

export type EquitySample = {
  ts: number
  label: string
  equity: number
}

export type DrawdownSample = {
  ts: number
  label: string
  equity: number
  peak: number
  drawdown: number
  drawdownPct: number
}

function asFiniteNumber(value: any): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

export function toEpochMs(value: any): number | null {
  if (value === null || value === undefined || value === "") return null
  if (typeof value === "number") {
    if (!Number.isFinite(value) || value <= 0) return null
    return value > 10_000_000_000 ? value : value * 1000
  }
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : null
}

export function parseHeartbeatEquity(row: Record<string, any>): number | null {
  const js = row?.json
  if (js && typeof js === "object") {
    const typ = String(js.type || "").toUpperCase()
    if (typ === "HEARTBEAT") {
      const eq = asFiniteNumber(js.equity)
      if (eq !== null && eq > 0) return eq
    }
  }

  const reportJson = row?.report_json
  if (reportJson && typeof reportJson === "object") {
    const eq = asFiniteNumber(reportJson.equity ?? reportJson.eq)
    if (eq !== null && eq > 0) return eq
  }

  const msg = String(row?.message || row?.report_text || "")
  const match = msg.match(/\beq=([0-9]+(?:\.[0-9]+)?)/i)
  if (!match) return null
  const eq = Number(match[1])
  return Number.isFinite(eq) && eq > 0 ? eq : null
}

export function parseReportTs(row: Record<string, any>): number | null {
  return (
    toEpochMs(row?.time) ??
    toEpochMs(row?.ts) ??
    toEpochMs(row?.created_at) ??
    toEpochMs(row?.updated_at) ??
    null
  )
}

export function buildEquitySamples(
  reports: Array<Record<string, any>>,
  liveTail?: {
    equity?: number | null
    ts?: string | number | null
  },
): EquitySample[] {
  const points: EquitySample[] = []

  for (const row of Array.isArray(reports) ? reports : []) {
    const equity = parseHeartbeatEquity(row)
    const ts = parseReportTs(row)
    if (equity === null || ts === null || ts <= 0) continue
    points.push({
      ts,
      label: new Date(ts).toLocaleTimeString(),
      equity,
    })
  }

  const liveEquity = asFiniteNumber(liveTail?.equity)
  const liveTs = toEpochMs(liveTail?.ts)
  if (liveEquity !== null && liveEquity > 0 && liveTs !== null && liveTs > 0) {
    points.push({
      ts: liveTs,
      label: new Date(liveTs).toLocaleTimeString(),
      equity: liveEquity,
    })
  }

  points.sort((a, b) => a.ts - b.ts)

  const deduped: EquitySample[] = []
  for (const point of points) {
    const prev = deduped[deduped.length - 1]
    if (!prev) {
      deduped.push(point)
      continue
    }
    if (point.ts === prev.ts) {
      deduped[deduped.length - 1] = point
      continue
    }
    if (Math.abs(point.equity - prev.equity) < 1e-9 && point.ts - prev.ts < 15_000) {
      deduped[deduped.length - 1] = point
      continue
    }
    deduped.push(point)
  }

  return deduped
}

export function formatChartTimestamp(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return ""
  const dt = new Date(value)
  return dt.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  })
}

export function buildDrawdownSamples(samples: EquitySample[]): DrawdownSample[] {
  if (!Array.isArray(samples) || samples.length === 0) return []

  let peak = samples[0].equity
  return samples.map((sample) => {
    peak = Math.max(peak, sample.equity)
    const drawdown = sample.equity - peak
    const drawdownPct = peak > 0 ? (drawdown / peak) * 100 : 0
    return {
      ts: sample.ts,
      label: formatChartTimestamp(sample.ts),
      equity: sample.equity,
      peak,
      drawdown,
      drawdownPct,
    }
  })
}

export function formatDeltaPct(current: number | null, baseline: number | null): number | null {
  if (current === null || baseline === null || baseline === 0) return null
  return ((current - baseline) / baseline) * 100
}

export function findLookbackEquity(samples: EquitySample[], lookbackMs: number): number | null {
  if (samples.length === 0) return null
  const latestTs = samples[samples.length - 1]?.ts || 0
  const targetTs = latestTs - lookbackMs
  let chosen: EquitySample | null = null
  for (const sample of samples) {
    if (sample.ts <= targetTs) chosen = sample
    else break
  }
  return chosen?.equity ?? samples[0]?.equity ?? null
}

export function computeDrawdownStats(samples: EquitySample[]) {
  if (samples.length === 0) {
    return {
      latest: 0,
      latestPct: 0,
      max: 0,
      maxPct: 0,
      peak: 0,
      trough: 0,
    }
  }

  let peak = samples[0].equity
  let trough = samples[0].equity
  let latest = 0
  let latestPct = 0
  let max = 0
  let maxPct = 0

  for (const sample of samples) {
    peak = Math.max(peak, sample.equity)
    trough = Math.min(trough, sample.equity)
    latest = sample.equity - peak
    latestPct = peak > 0 ? (latest / peak) * 100 : 0
    if (latest < max) max = latest
    if (latestPct < maxPct) maxPct = latestPct
  }

  return { latest, latestPct, max, maxPct, peak, trough }
}

export function sumOpenProfit(positions: Array<Record<string, any>>): number {
  return (Array.isArray(positions) ? positions : []).reduce((sum, position) => {
    const profit = asFiniteNumber(position?.profit)
    return sum + (profit ?? 0)
  }, 0)
}

export function sumOpenLots(positions: Array<Record<string, any>>): number {
  return (Array.isArray(positions) ? positions : []).reduce((sum, position) => {
    const lots = asFiniteNumber(position?.lots)
    return sum + Math.abs(lots ?? 0)
  }, 0)
}
