"use client"

import { useMemo } from "react"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import {
  buildEquitySamples,
  computeDrawdownStats,
  findLookbackEquity,
  formatDeltaPct,
  sumOpenLots,
  sumOpenProfit,
} from "@/lib/trading/performance"
import { bridgeStatusLabel, formatAgeSeconds } from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

function formatCurrency(value: number | null | undefined): string {
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "N/A"
}

function formatPct(value: number | null | undefined): string {
  const pct = Number(value)
  return Number.isFinite(pct) ? `${pct.toFixed(2)}%` : "—"
}

export function TradeStatistics() {
  const { state, loading: liveLoading } = useLiveBridgeState(3000)
  const { history, loading: historyLoading } = useTradingHistory(3000)
  const loading = liveLoading || historyLoading
  const lastHeartbeat = state?.lastHeartbeat ?? null

  const positions = Array.isArray(state?.positions) ? (state?.positions ?? []) : []
  const openProfit = useMemo(() => sumOpenProfit(positions), [positions])
  const grossLots = useMemo(() => sumOpenLots(positions), [positions])
  const equitySamples = useMemo(
    () =>
      buildEquitySamples(Array.isArray(history.reports) ? history.reports : [], {
        equity: state?.displayEquity,
        ts: lastHeartbeat,
      }),
    [history.reports, state?.displayEquity, lastHeartbeat],
  )

  const latestEquity = equitySamples[equitySamples.length - 1]?.equity ?? state?.displayEquity ?? null
  const baseline1h = findLookbackEquity(equitySamples, 60 * 60 * 1000)
  const baseline24h = findLookbackEquity(equitySamples, 24 * 60 * 60 * 1000)
  const delta1h = latestEquity !== null && baseline1h !== null ? latestEquity - baseline1h : null
  const delta24h = latestEquity !== null && baseline24h !== null ? latestEquity - baseline24h : null
  const delta1hPct = formatDeltaPct(latestEquity, baseline1h)
  const delta24hPct = formatDeltaPct(latestEquity, baseline24h)
  const drawdown = computeDrawdownStats(equitySamples)

  const stats = [
    {
      label: "Live Equity",
      value: loading ? "..." : formatCurrency(state?.displayEquity ?? latestEquity),
      subtext: state?.displayEquity != null ? `Bridge truth via ${String(state?.equitySource || "live")}` : "Live equity hidden while stale",
      accent: "text-foreground",
    },
    {
      label: "Open P/L",
      value: loading ? "..." : formatCurrency(openProfit),
      subtext: `${Number(state?.openPositionsCount || positions.length || 0)} open positions`,
      accent: openProfit >= 0 ? "text-emerald-400" : "text-rose-400",
    },
    {
      label: "Gross Lots",
      value: loading ? "..." : grossLots.toFixed(2),
      subtext: `${Number(state?.readyEntriesCount || 0)} ready | ${Number(state?.queuedEntriesCount || 0)} queued`,
      accent: "text-foreground",
    },
    {
      label: "Bridge Freshness",
      value: loading ? "..." : bridgeStatusLabel(state?.statusTier),
      subtext: `${formatAgeSeconds(state?.heartbeatAgeSecs)} heartbeat | ${formatAgeSeconds(state?.runtimeCycleAgeSecs)} runtime`,
      accent: state?.statusTier === "bridge_up_mt4_live" ? "text-emerald-400" : "text-amber-300",
    },
    {
      label: "1h Equity Change",
      value: loading ? "..." : `${formatCurrency(delta1h)} ${delta1hPct !== null ? `(${formatPct(delta1hPct)})` : ""}`.trim(),
      subtext: baseline1h !== null ? `baseline ${formatCurrency(baseline1h)}` : "No 1h baseline yet",
      accent: (delta1h || 0) >= 0 ? "text-emerald-400" : "text-rose-400",
    },
    {
      label: "24h Equity Change",
      value: loading ? "..." : `${formatCurrency(delta24h)} ${delta24hPct !== null ? `(${formatPct(delta24hPct)})` : ""}`.trim(),
      subtext: baseline24h !== null ? `baseline ${formatCurrency(baseline24h)}` : "No 24h baseline yet",
      accent: (delta24h || 0) >= 0 ? "text-emerald-400" : "text-rose-400",
    },
    {
      label: "Current Drawdown",
      value: loading ? "..." : `${formatCurrency(drawdown.latest)} (${formatPct(drawdown.latestPct)})`,
      subtext: `peak ${formatCurrency(drawdown.peak)}`,
      accent: drawdown.latest >= 0 ? "text-foreground" : "text-rose-400",
    },
    {
      label: "Max Drawdown",
      value: loading ? "..." : `${formatCurrency(drawdown.max)} (${formatPct(drawdown.maxPct)})`,
      subtext: `${equitySamples.length} equity samples in view`,
      accent: "text-rose-400",
    },
  ]

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
      {stats.map((stat) => (
        <Card key={stat.label} className="p-6">
          <div className="mb-1 text-sm text-muted-foreground">{stat.label}</div>
          <div className={cn("mb-1 text-2xl font-bold", stat.accent)}>{stat.value}</div>
          <div className="text-xs text-muted-foreground">{stat.subtext}</div>
        </Card>
      ))}
    </div>
  )
}
