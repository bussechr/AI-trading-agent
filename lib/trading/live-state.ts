import type { BridgeStatusTier } from "@/lib/trading/status-tier"

export function formatAgeSeconds(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return "n/a"
  const numeric = Number(seconds)
  if (!Number.isFinite(numeric) || numeric < 0) return "n/a"
  const value = Math.floor(numeric)
  if (value < 60) return `${value}s ago`
  const minutes = Math.floor(value / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

function finiteTelemetryNumber(value: unknown): number | null {
  if (value === null || value === undefined) return null
  if (typeof value === "string" && !value.trim()) return null
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric : null
}

export function formatFiniteNumber(value: unknown, digits = 2, fallback = "n/a"): string {
  const numeric = finiteTelemetryNumber(value)
  return numeric === null ? fallback : numeric.toFixed(digits)
}

export function formatRatioPercent(value: unknown, digits = 1, fallback = "n/a"): string {
  const numeric = finiteTelemetryNumber(value)
  return numeric === null ? fallback : `${(numeric * 100).toFixed(digits)}%`
}

export function formatNonNegativeInteger(value: unknown, fallback = "n/a"): string {
  const numeric = finiteTelemetryNumber(value)
  return numeric !== null && numeric >= 0 ? String(Math.floor(numeric)) : fallback
}

export function formatSignedBps(value: unknown, digits = 2, fallback = "n/a"): string {
  const numeric = finiteTelemetryNumber(value)
  if (numeric === null) return fallback
  return `${numeric >= 0 ? "+" : ""}${numeric.toFixed(digits)} bps`
}

export function bridgeStatusLabel(statusTier: BridgeStatusTier | string | null | undefined): string {
  switch (statusTier) {
    case "bridge_up_mt4_live":
      return "Live"
    case "bridge_up_runtime_starting":
      return "Runtime Starting"
    case "bridge_up_runtime_stalled":
      return "Runtime Stalled"
    case "bridge_up_runtime_failed":
      return "Runtime Failed"
    case "bridge_up_db_unhealthy":
      return "Database Unhealthy"
    case "bridge_up_runtime_stale":
      return "Runtime Stale"
    case "bridge_up_runtime_ready_mt4_stale":
      return "Runtime Ready, MT4 Stale"
    case "bridge_up_mt4_stale":
      return "Bridge Up, MT4 Stale"
    case "bridge_down":
    default:
      return "Disconnected"
  }
}

export function bridgeStatusClasses(statusTier: BridgeStatusTier | string | null | undefined): string {
  switch (statusTier) {
    case "bridge_up_mt4_live":
      return "border-emerald-500/30 bg-emerald-500/10 text-emerald-300"
    case "bridge_up_runtime_starting":
      return "border-sky-500/30 bg-sky-500/10 text-sky-200"
    case "bridge_up_runtime_stalled":
      return "border-orange-500/30 bg-orange-500/10 text-orange-200"
    case "bridge_up_runtime_failed":
    case "bridge_up_db_unhealthy":
      return "border-rose-500/30 bg-rose-500/10 text-rose-200"
    case "bridge_up_runtime_stale":
    case "bridge_up_runtime_ready_mt4_stale":
      return "border-amber-500/30 bg-amber-500/10 text-amber-200"
    case "bridge_up_mt4_stale":
      return "border-amber-500/30 bg-amber-500/10 text-amber-200"
    case "bridge_down":
    default:
      return "border-rose-500/30 bg-rose-500/10 text-rose-200"
  }
}

export function bridgeStatusDotClasses(statusTier: BridgeStatusTier | string | null | undefined): string {
  switch (statusTier) {
    case "bridge_up_mt4_live":
      return "bg-emerald-400"
    case "bridge_up_runtime_starting":
      return "bg-sky-300"
    case "bridge_up_runtime_stalled":
      return "bg-orange-300"
    case "bridge_up_runtime_failed":
    case "bridge_up_db_unhealthy":
      return "bg-rose-400"
    case "bridge_up_runtime_stale":
    case "bridge_up_runtime_ready_mt4_stale":
      return "bg-amber-300"
    case "bridge_up_mt4_stale":
      return "bg-amber-300"
    case "bridge_down":
    default:
      return "bg-rose-400"
  }
}
