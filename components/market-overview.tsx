"use client"

import { Card } from "@/components/ui/card"
import { TrendingUp, Activity, Zap, DollarSign } from "lucide-react"
import { useTradingState } from "@/lib/hooks/use-trading-state"

export function MarketOverview() {
  const { state, loading } = useTradingState()

  const activeSignals = state?.agentDecisions?.length || 0
  const avgScore = state?.agentDecisions?.length
    ? (state.agentDecisions.reduce((sum, d) => sum + d.score, 0) / state.agentDecisions.length).toFixed(2)
    : "0.00"

  const equity = state?.equity || 0
  const cycleProgress =
    state?.cycleActive && state?.cycleStartEquity
      ? (((equity - state.cycleStartEquity) / state.cycleStartEquity) * 100).toFixed(1)
      : "0.0"

  const metrics = [
    {
      label: "Active Signals",
      value: loading ? "..." : activeSignals.toString(),
      change: state?.signalsSent ? `${state.signalsSent} sent` : "—",
      trend: "neutral",
      icon: Activity,
    },
    {
      label: "Avg EL Score",
      value: loading ? "..." : avgScore,
      change: `${activeSignals} active`,
      trend: "neutral",
      icon: Zap,
    },
    {
      label: "Account Equity",
      value: loading ? "..." : `$${equity.toLocaleString()}`,
      change: state?.cycleActive ? `Cycle: ${cycleProgress}%` : "No cycle",
      trend: Number.parseFloat(cycleProgress) > 0 ? "up" : "neutral",
      icon: DollarSign,
    },
    {
      label: "Trades Executed",
      value: loading ? "..." : (state?.tradesExecuted || 0).toString(),
      change: state?.systemStatus || "unknown",
      trend: state?.isRunning ? "up" : "neutral",
      icon: TrendingUp,
    },
  ]

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {metrics.map((metric) => (
        <Card key={metric.label} className="p-6">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2">
              <div className="rounded-lg bg-primary/10 p-2">
                <metric.icon className="h-4 w-4 text-primary" />
              </div>
            </div>
            <div
              className={`text-sm font-medium ${
                metric.trend === "up"
                  ? "text-green-500"
                  : metric.trend === "down"
                    ? "text-red-500"
                    : "text-muted-foreground"
              }`}
            >
              {metric.change}
            </div>
          </div>
          <div className="mt-4">
            <div className="text-2xl font-bold text-foreground">{metric.value}</div>
            <div className="text-sm text-muted-foreground">{metric.label}</div>
          </div>
        </Card>
      ))}
    </div>
  )
}
