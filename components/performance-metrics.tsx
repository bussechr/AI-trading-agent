"use client"

import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { bridgeStatusClasses, bridgeStatusLabel, formatAgeSeconds } from "@/lib/trading/live-state"
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
  const cycleActive = Boolean(state?.cycleActive && state?.statusTier === "bridge_up_mt4_live")
  const cycleStartEquity = Number(state?.cycleStartEquity || 0)
  const cycleTarget = Number(state?.cycleTarget || 0)
  const cycleProgress =
    cycleActive && cycleStartEquity > 0 && displayEquity !== null
      ? ((displayEquity - cycleStartEquity) / cycleStartEquity) * 100
      : null

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
            <div className="text-xs uppercase tracking-[0.22em] text-muted-foreground">System Contract</div>
            <div className="mt-3 space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Bridge tier</span>
                <span className="font-medium text-foreground">{bridgeStatusLabel(state?.statusTier)}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Runtime</span>
                <span className="font-medium text-foreground">{String(state?.runtimeStatus || "unknown")}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Tick feed</span>
                <span className="font-medium text-foreground">{String(state?.tickStatus || "unknown")}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Execution state</span>
                <span className="font-medium text-foreground">{state?.isRunning ? "ready" : "degraded"}</span>
              </div>
            </div>
          </div>
        </div>

        <div className="grid gap-4 md:grid-cols-2">
          <div className="rounded-3xl border border-border/70 bg-background/50 p-5">
            <div className="text-sm font-medium text-foreground">Active Cycle</div>
            {cycleActive ? (
              <div className="mt-3 space-y-2 text-sm">
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Start equity</span>
                  <span className="font-mono text-foreground">{formatCurrency(cycleStartEquity)}</span>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Target</span>
                  <span className="font-mono text-foreground">{formatCurrency(cycleTarget)}</span>
                </div>
                <div className="flex justify-between gap-4">
                  <span className="text-muted-foreground">Progress</span>
                  <span className={cn("font-mono", (cycleProgress || 0) >= 0 ? "text-emerald-400" : "text-rose-400")}>
                    {formatPct(cycleProgress)}
                  </span>
                </div>
              </div>
            ) : (
              <div className="mt-3 text-sm text-muted-foreground">
                {state?.statusTier === "bridge_up_mt4_live" ? "No active profit cycle." : "Cycle analytics pause when live equity is unavailable."}
              </div>
            )}
          </div>

          <div className="rounded-3xl border border-border/70 bg-background/50 p-5">
            <div className="text-sm font-medium text-foreground">Risk and Governance</div>
            <div className="mt-3 space-y-2 text-sm">
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Signals sent</span>
                <span className="font-mono text-foreground">{loading ? "…" : String(state?.signalsSent || 0)}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Trades executed</span>
                <span className="font-mono text-foreground">{loading ? "…" : String(state?.tradesExecuted || 0)}</span>
              </div>
              <div className="flex justify-between gap-4">
                <span className="text-muted-foreground">Risk envelope</span>
                <span className="font-mono text-foreground">
                  soft {formatPct(Number(riskEnvelope?.soft_dd_pct || 0) * 100)} | hard {formatPct(Number(riskEnvelope?.hard_dd_pct || 0) * 100)}
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
