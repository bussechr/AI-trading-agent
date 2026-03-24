import { DashboardLayout } from "@/components/dashboard-layout"
import { EquityCurve } from "@/components/equity-curve"
import { TradeStatistics } from "@/components/trade-statistics"
import { ClosedTradePerformance } from "@/components/closed-trade-performance"
import { DrawdownChart } from "@/components/drawdown-chart"
import { GovernanceTimeline } from "@/components/governance-timeline"
import { PipelineHealth } from "@/components/pipeline-health"
import { CommandLifecycleTimeline } from "@/components/command-lifecycle-timeline"

export default function PerformancePage() {
  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-foreground">Performance Analytics</h1>
          <p className="text-muted-foreground">Live account performance first, with execution telemetry separated below.</p>
        </div>

        <TradeStatistics />
        <ClosedTradePerformance />

        <div className="grid gap-6">
          <EquityCurve />
          <DrawdownChart />
          <PipelineHealth />
          <GovernanceTimeline />
          <CommandLifecycleTimeline />
        </div>
      </div>
    </DashboardLayout>
  )
}
