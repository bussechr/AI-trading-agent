"use client"

import { BridgeStatusBanner } from "@/components/bridge-status-banner"
import { LiveSignals } from "@/components/live-signals"
import { MarketOverview } from "@/components/market-overview"
import { PerformanceMetrics } from "@/components/performance-metrics"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

export function DashboardHome() {
  const { error } = useTradingTelemetry(3000)

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-3xl font-bold text-foreground">Trading Dashboard</h1>
        <p className="text-muted-foreground">Live runtime status from bridge v2 state and metrics endpoints</p>
      </div>

      <BridgeStatusBanner error={error} bridgeUrl="http://127.0.0.1:58710" />

      <MarketOverview />

      <div className="grid gap-6 lg:grid-cols-2">
        <LiveSignals />
        <PerformanceMetrics />
      </div>
    </div>
  )
}
