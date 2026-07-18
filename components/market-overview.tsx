"use client"

import { Activity, AlertTriangle, DollarSign, Zap } from "lucide-react"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { bridgeStatusClasses, bridgeStatusLabel, formatRatioPercent } from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

function formatCurrency(value: number | null | undefined): string {
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "N/A"
}

function formatScore(value: number | null | undefined): string {
  const score = Number(value)
  return Number.isFinite(score) ? score.toFixed(2) : "—"
}

export function MarketOverview() {
  const { state, loading } = useLiveBridgeState(3000)
  const { history } = useTradingHistory(5000)

  const decisions = state?.agentDecisions
  const signals = Array.isArray(decisions) ? decisions : []
  const activeSignals = state?.signalDataFresh ? signals.length : 0
  const scoreValues = signals
    .map((signal) => Number(signal.score))
    .filter((score) => Number.isFinite(score))
  const avgScore =
    scoreValues.length > 0 ? scoreValues.reduce((sum, score) => sum + score, 0) / scoreValues.length : null

  const pending = Number(history.metrics?.pending?.count || 0)
  const timeoutRate = Number(history.metrics?.timeouts?.ack_timeout_rate_5m || 0)
  const staleSignals = !state?.signalDataFresh

  const metrics = [
    {
      label: "Live Status",
      value: loading ? "…" : bridgeStatusLabel(state?.statusTier),
      detail:
        state?.statusTier === "bridge_up_mt4_live"
          ? `${state?.tickSymbolsCount || 0} pairs fresh`
          : String(state?.signalDataReason || state?.tickReason || "waiting for feed"),
      icon: Activity,
      accent: "text-emerald-300",
      badgeClass: bridgeStatusClasses(state?.statusTier),
    },
    {
      label: "Active Signals",
      value: loading ? "…" : String(activeSignals),
      detail: staleSignals
        ? "hidden until MT4 heartbeat and ticks are fresh"
        : `${Number(state?.openPositionsCount || state?.positions?.length || 0)} open | ${Number(state?.readyEntriesCount || 0)} ready`,
      icon: Zap,
      accent: "text-sky-300",
    },
    {
      label: "Display Equity",
      value: loading ? "…" : formatCurrency(state?.displayEquity ?? null),
      detail:
        state?.statusTier === "bridge_up_mt4_live"
          ? `source ${String(state?.equitySource || "mt4")}`
          : "truth-first live equity disabled while stale",
      icon: DollarSign,
      accent: "text-emerald-300",
    },
    {
      label: "Queue Pressure",
      value: loading ? "…" : String(pending),
      detail: `${formatRatioPercent(timeoutRate, 2)} timeout rate`,
      icon: AlertTriangle,
      accent: timeoutRate > 0.05 ? "text-amber-300" : "text-slate-300",
    },
  ]

  return (
    <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
      {metrics.map((metric) => (
        <Card key={metric.label} className="overflow-hidden p-0">
          <div className="border-b border-border/70 px-5 py-4">
            <div className="flex items-center justify-between gap-3">
              <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">{metric.label}</div>
              {"badgeClass" in metric && metric.badgeClass ? (
                <span className={cn("rounded-full border px-2.5 py-1 text-[11px] font-medium", metric.badgeClass)}>
                  {metric.value}
                </span>
              ) : (
                <metric.icon className={cn("h-4 w-4", metric.accent)} />
              )}
            </div>
          </div>
          <div className="space-y-3 px-5 py-5">
            <div className="flex items-end justify-between gap-3">
              <div className="text-3xl font-semibold text-foreground">{metric.value}</div>
              {!("badgeClass" in metric) && <metric.icon className={cn("h-5 w-5", metric.accent)} />}
            </div>
            <div className="text-sm text-muted-foreground">{metric.detail}</div>
            {metric.label === "Active Signals" && (
              <div className="rounded-2xl border border-border/70 bg-background/50 px-3 py-2 text-xs text-muted-foreground">
                Avg score <span className="ml-2 font-mono text-foreground">{staleSignals ? "—" : formatScore(avgScore)}</span>
              </div>
            )}
          </div>
        </Card>
      ))}
    </div>
  )
}
