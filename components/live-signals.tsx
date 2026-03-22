"use client"

import { ArrowDownRight, ArrowUpRight, Minus, ShieldCheck } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { bridgeStatusClasses, bridgeStatusLabel } from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

function formatNumber(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—"
}

function formatPercent(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(2)}%` : "—"
}

function formatBps(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? `${numeric.toFixed(2)} bps` : "—"
}

function signalTone(side: string): { wrap: string; icon: string; Icon: typeof ArrowUpRight } {
  if (side === "BUY") {
    return {
      wrap: "bg-emerald-500/12",
      icon: "text-emerald-400",
      Icon: ArrowUpRight,
    }
  }
  if (side === "SELL") {
    return {
      wrap: "bg-rose-500/12",
      icon: "text-rose-400",
      Icon: ArrowDownRight,
    }
  }
  return {
    wrap: "bg-slate-500/12",
    icon: "text-slate-400",
    Icon: Minus,
  }
}

export function LiveSignals() {
  const { state, loading } = useLiveBridgeState(3000)
  const decisions = state?.agentDecisions
  const signals = Array.isArray(decisions) ? decisions : []
  const live = Boolean(state?.signalDataFresh && state?.statusTier === "bridge_up_mt4_live")

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border/70 px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Live Signals</div>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">MT4-fed candidate stream</h3>
          </div>
          <Badge variant="outline" className={cn("rounded-full px-3 py-1 text-xs", bridgeStatusClasses(state?.statusTier))}>
            {loading ? "Loading" : bridgeStatusLabel(state?.statusTier)}
          </Badge>
        </div>
      </div>

      <div className="space-y-3 px-6 py-6">
        {loading ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            Loading live bridge snapshot…
          </div>
        ) : !live ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center">
            <div className="text-lg font-medium text-foreground">No live MT4-fed signals</div>
            <div className="mt-2 text-sm text-muted-foreground">
              Signals render only when heartbeat and tick freshness are both live. Current reason: {String(state?.signalDataReason || state?.tickReason || "waiting")}
            </div>
          </div>
        ) : signals.length === 0 ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            No active signals in the current evaluation cycle.
          </div>
        ) : (
          signals.map((signal) => {
            const tone = signalTone(signal.side)
            return (
              <div
                key={`${signal.symbol}-${signal.side}-${signal.reason || "signal"}`}
                className="rounded-3xl border border-border/70 bg-background/55 p-4"
              >
                <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                  <div className="flex items-center gap-4">
                    <div className={cn("rounded-2xl p-3", tone.wrap)}>
                      <tone.Icon className={cn("h-5 w-5", tone.icon)} />
                    </div>
                    <div>
                      <div className="text-xl font-semibold text-foreground">{signal.symbol}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                        <span>{signal.side}</span>
                        <span className="text-border">•</span>
                        <span>@ {formatNumber(signal.price, 5)}</span>
                        <span className="text-border">•</span>
                        <span>
                          spread {formatBps(signal.spread_bps)} / max {formatBps(signal.max_spread_bps)}
                        </span>
                        <span className="text-border">•</span>
                        <span>{signal.reason || "no reason"}</span>
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-2 sm:grid-cols-3 lg:min-w-[360px]">
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Score</div>
                      <div className="mt-2 font-mono text-lg text-foreground">{formatNumber(signal.score, 2)}</div>
                    </div>
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Target</div>
                      <div className="mt-2 font-mono text-lg text-foreground">{formatPercent(signal.target_pct)}</div>
                    </div>
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Gate</div>
                      <div className="mt-2 inline-flex items-center gap-2 font-medium text-foreground">
                        <ShieldCheck className={cn("h-4 w-4", signal.execution_ready ? "text-emerald-400" : "text-amber-400")} />
                        {signal.execution_ready ? "ready" : "blocked"}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>
    </Card>
  )
}
