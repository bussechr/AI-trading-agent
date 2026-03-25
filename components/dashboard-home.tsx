"use client"

import { BridgeStatusBanner } from "@/components/bridge-status-banner"
import { OpenPositionsSignals } from "@/components/live-signals"
import { LiveStatusRail } from "@/components/live-status-rail"
import { MarketOverview } from "@/components/market-overview"
import { PerformanceMetrics } from "@/components/performance-metrics"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"

export function DashboardHome() {
  const { state, error } = useLiveBridgeState(3000)

  return (
    <div className="space-y-6">
      <div className="max-w-3xl">
        <div className="text-xs uppercase tracking-[0.24em] text-muted-foreground">FX Trader</div>
        <h1 className="mt-3 text-4xl font-semibold text-foreground lg:text-5xl">Live control surface for bridge truth, runtime health, and AI ops</h1>
        <p className="mt-4 text-base text-muted-foreground">
          Stable on <span className="font-mono text-foreground">http://127.0.0.1:3000</span>. Live status is driven by bridge heartbeat and tick freshness, not cached runtime activity.
        </p>
      </div>

      <LiveStatusRail />
      <BridgeStatusBanner state={state} error={error} bridgeUrl="http://127.0.0.1:58710" />
      <MarketOverview />

      <div className="grid gap-6 xl:grid-cols-[1.25fr_0.95fr]">
        <OpenPositionsSignals />
        <PerformanceMetrics />
      </div>
    </div>
  )
}
