"use client"

import { useMemo } from "react"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { bridgeStatusClasses, bridgeStatusLabel, formatAgeSeconds } from "@/lib/trading/live-state"
import {
  buildEquitySamples,
  computeDrawdownStats,
  findLookbackEquity,
  formatDeltaPct,
  sumOpenLots,
  sumOpenProfit,
} from "@/lib/trading/performance"
import { cn } from "@/lib/utils"

function formatCurrency(value: number | null | undefined): string {
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "N/A"
}

function formatPct(value: number | null | undefined): string {
  const pct = Number(value)
  return Number.isFinite(pct) ? `${pct.toFixed(2)}%` : "—"
}

export function PerformanceMetrics() {
  const { state, loading } = useLiveBridgeState(3000)
  const { history } = useTradingHistory(5000)

  const riskEnvelope = state?.riskEnvelope || history.metrics?.risk_envelope || {}
  const governance = state?.governance || {}
  const displayEquity = state?.displayEquity ?? null
  const lastHeartbeat = state?.lastHeartbeat ?? null
  const positions = Array.isArray(state?.positions) ? (state?.positions ?? []) : []
  const openPositionsCount = Number(state?.openPositionsCount || positions.length || 0)
  const readyEntriesCount = Number(state?.readyEntriesCount || 0)
  const openProfit = useMemo(() => sumOpenProfit(positions), [positions])
  const openLots = useMemo(() => sumOpenLots(positions), [positions])
  const equitySamples = useMemo(
    () =>
      buildEquitySamples(Array.isArray(history.reports) ? history.reports : [], {
        equity: displayEquity,
        ts: lastHeartbeat,
      }),
    [displayEquity, history.reports, lastHeartbeat],
  )
  const latestEquity = equitySamples[equitySamples.length - 1]?.equity ?? displayEquity
  const baseline1h = findLookbackEquity(equitySamples, 60 * 60 * 1000)
  const baseline24h = findLookbackEquity(equitySamples, 24 * 60 * 60 * 1000)
  const delta1h = latestEquity !== null && baseline1h !== null ? latestEquity - baseline1h : null
  const delta24h = latestEquity !== null && baseline24h !== null ? latestEquity - baseline24h : null
  const delta1hPct = formatDeltaPct(latestEquity, baseline1h)
  const delta24hPct = formatDeltaPct(latestEquity, baseline24h)
  const drawdown = computeDrawdownStats(equitySamples.slice(-240))

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border/70 px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Performance</div>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">Truth-first account view</h3>
          </div>
          <span className={cn("rounded-full border px-3 py-1 text-xs font-medium", bridgeStatusClasses(state?.statusTier))}>
            {loading ? "Loading" : bridgeStatusLabel(state?.statusTier)}
          </span>
        </div>
      </div>

      <div className="space-y-6 px-6 py-6">
        <div className="grid gap-4 md:grid-cols-[1.1fr_0.9fr]">
          <div className="rounded-3xl border border-border/70 bg-background/60 p-5">
            <div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">Current Equity</div>
            <div className="mt-3 text-4xl font-semibold text-foreground">
              {loading ? "…" : displayEquity === null ? "N/A" : formatCurrency(displayEquity)}
            </div>
            <div className="mt-3 text-sm text-muted-foreground">
              {state?.statusTier === "bridge_up_mt4_live"
                ? `Live MT4 equity, ${formatAgeSeconds(state?.heartbeatAgeSecs)} heartbeat age`
                : `Live equity hidden while ${bridgeStatusLabel(state?.statusTier).toLowerCase()}`}
            </div>
            {displayEquity === null && Number.isFinite(Number(state?.cachedEquity)) && (
              <div className="mt-3 text-sm text-muted-foreground">
                Cached diagnostic snapshot <span className="font-mono text-foreground">{formatCurrency(state?.cachedEquity)}</span>
              </div>
            )}
          </div>

          <div className="rounded-3xl border border-border/70 bg-background/60 p-5">
            <div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">Open Risk</div>
            <div className="mt-3 space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Open P/L</span>
                <span className={cn("font-mono", openProfit >= 0 ? "text-emerald-400" : "text-rose-400")}>
                  {formatCurrency(openProfit)}
                </span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Gross lots</span>
                <span className="font-mono text-foreground">{openLots.toFixed(2)}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Open positions</span>
                <span className="font-mono text-foreground">{String(openPositionsCount)}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Ready entries</span>
                <span className="font-mono text-foreground">{String(readyEntriesCount)}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-3xl border border-border/70 bg-background/50 p-5">
            <div className="text-sm font-medium text-foreground">Recent Equity Window</div>
            {equitySamples.length > 0 ? (
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">1 hour</span>
                  <span className={cn("font-mono", (delta1h || 0) >= 0 ? "text-emerald-400" : "text-rose-400")}>
                    {formatCurrency(delta1h)} {delta1hPct !== null ? `(${formatPct(delta1hPct)})` : ""}
                  </span>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">24 hours</span>
                  <span className={cn("font-mono", (delta24h || 0) >= 0 ? "text-emerald-400" : "text-rose-400")}>
                    {formatCurrency(delta24h)} {delta24hPct !== null ? `(${formatPct(delta24hPct)})` : ""}
                  </span>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Samples</span>
                  <span className="font-mono text-foreground">{String(equitySamples.length)}</span>
                </div>
              </div>
            ) : (
              <div className="mt-3 text-sm text-muted-foreground">
                Equity history is not available yet.
              </div>
            )}
          </div>

          <div className="rounded-3xl border border-border/70 bg-background/50 p-5">
            <div className="text-sm font-medium text-foreground">Risk and Freshness</div>
            <div className="mt-3 space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Current drawdown</span>
                <span className={cn("font-mono", drawdown.latest >= 0 ? "text-foreground" : "text-rose-400")}>
                  {formatCurrency(drawdown.latest)} ({formatPct(drawdown.latestPct)})
                </span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Max drawdown</span>
                <span className="font-mono text-foreground">
                  {formatCurrency(drawdown.max)} ({formatPct(drawdown.maxPct)})
                </span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Risk envelope</span>
                <span className="font-mono text-foreground">
                  soft {formatPct(Number(riskEnvelope?.soft_dd_pct || 0) * 100)} | hard {formatPct(Number(riskEnvelope?.hard_dd_pct || 0) * 100)}
                </span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Heartbeat / runtime</span>
                <span className="font-mono text-foreground">
                  {formatAgeSeconds(state?.heartbeatAgeSecs)} / {formatAgeSeconds(state?.runtimeCycleAgeSecs)}
                </span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Governance</span>
                <span className="font-medium text-foreground">
                  {governance?.paused ? "paused" : "active"} ({String((governance?.reasons || [])[0] || "none")})
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Card>
  )
}
