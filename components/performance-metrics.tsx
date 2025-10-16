"use client"

import { Card } from "@/components/ui/card"
import { useTradingState } from "@/lib/hooks/use-trading-state"

export function PerformanceMetrics() {
  const { state, loading } = useTradingState()

  const equity = state?.equity || 0
  const cycleActive = state?.cycleActive || false
  const cycleProgress =
    cycleActive && state?.cycleStartEquity ? ((equity - state.cycleStartEquity) / state.cycleStartEquity) * 100 : 0

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Performance</h3>

      <div className="space-y-4">
        <div>
          <div className="text-sm text-muted-foreground">Current Equity</div>
          <div className="text-3xl font-bold text-foreground">{loading ? "..." : `$${equity.toLocaleString()}`}</div>
        </div>

        {cycleActive && (
          <div className="p-4 rounded-lg bg-primary/5 border border-primary/20">
            <div className="text-sm font-medium text-foreground mb-2">Active Cycle</div>
            <div className="space-y-2">
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Start Equity</span>
                <span className="text-foreground font-medium">${state.cycleStartEquity.toLocaleString()}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Target</span>
                <span className="text-foreground font-medium">${state.cycleTarget.toLocaleString()}</span>
              </div>
              <div className="flex justify-between text-sm">
                <span className="text-muted-foreground">Progress</span>
                <span className={`font-medium ${cycleProgress > 0 ? "text-green-500" : "text-muted-foreground"}`}>
                  {cycleProgress.toFixed(2)}%
                </span>
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-2 gap-4 pt-4 border-t border-border">
          <div>
            <div className="text-xs text-muted-foreground">Signals Sent</div>
            <div className="text-lg font-semibold text-foreground">{loading ? "..." : state?.signalsSent || 0}</div>
          </div>
          <div>
            <div className="text-xs text-muted-foreground">Trades Executed</div>
            <div className="text-lg font-semibold text-foreground">{loading ? "..." : state?.tradesExecuted || 0}</div>
          </div>
        </div>

        <div className="pt-4 border-t border-border">
          <div className="flex items-center justify-between">
            <span className="text-sm text-muted-foreground">System Status</span>
            <span className={`text-sm font-medium ${state?.isRunning ? "text-green-500" : "text-red-500"}`}>
              {loading ? "..." : state?.systemStatus || "unknown"}
            </span>
          </div>
          {state?.lastHeartbeat && (
            <div className="text-xs text-muted-foreground mt-1">
              Last heartbeat: {new Date(state.lastHeartbeat).toLocaleTimeString()}
            </div>
          )}
        </div>
      </div>
    </Card>
  )
}
