import type { BridgeStatusTier } from "@/lib/hooks/use-live-bridge-state"

export function formatAgeSeconds(seconds: number | null | undefined): string {
  if (!Number.isFinite(Number(seconds))) return "n/a"
  const value = Math.max(0, Math.floor(Number(seconds)))
  if (value < 60) return `${value}s ago`
  const minutes = Math.floor(value / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  return `${days}d ago`
}

export function bridgeStatusLabel(statusTier: BridgeStatusTier | string | null | undefined): string {
  switch (statusTier) {
    case "bridge_up_mt4_live":
      return "Live"
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
    case "bridge_up_mt4_stale":
      return "bg-amber-300"
    case "bridge_down":
    default:
      return "bg-rose-400"
  }
}
