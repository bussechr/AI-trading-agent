"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ArrowUp, ArrowDown } from "lucide-react"
import { useTradingState } from "@/lib/hooks/use-trading-state"

export function LiveSignals() {
  const { state, loading } = useTradingState()

  const signals = state?.agentDecisions || []

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-foreground">Live Signals</h3>
        <Badge
          variant="outline"
          className={
            state?.isRunning
              ? "bg-green-500/10 text-green-500 border-green-500/20"
              : "bg-red-500/10 text-red-500 border-red-500/20"
          }
        >
          {loading ? "Loading..." : state?.isRunning ? "Active" : "Disconnected"}
        </Badge>
      </div>

      <div className="space-y-3">
        {loading ? (
          <div className="text-center text-muted-foreground py-8">Loading signals...</div>
        ) : signals.length === 0 ? (
          <div className="text-center text-muted-foreground py-8">No active signals</div>
        ) : (
          signals.map((signal, i) => (
            <div
              key={i}
              className="flex items-center justify-between p-3 rounded-lg bg-accent/50 hover:bg-accent transition-colors"
            >
              <div className="flex items-center gap-3">
                <div className={`rounded-full p-2 ${signal.side === "BUY" ? "bg-green-500/10" : "bg-red-500/10"}`}>
                  {signal.side === "BUY" ? (
                    <ArrowUp className="h-4 w-4 text-green-500" />
                  ) : (
                    <ArrowDown className="h-4 w-4 text-red-500" />
                  )}
                </div>
                <div>
                  <div className="font-medium text-foreground">{signal.symbol}</div>
                  <div className="text-xs text-muted-foreground">@ {signal.price?.toFixed(5) || "—"}</div>
                </div>
              </div>
              <div className="text-right">
                <div className="text-sm font-medium text-foreground">Score: {signal.score.toFixed(2)}</div>
                <div className="text-xs text-muted-foreground">Target: {(signal.target_pct * 100).toFixed(2)}%</div>
              </div>
            </div>
          ))
        )}
      </div>

      {state?.lastSignal && (
        <div className="mt-4 pt-4 border-t border-border">
          <div className="text-xs text-muted-foreground">
            Last signal: {new Date(state.lastSignal.time).toLocaleTimeString()}
          </div>
        </div>
      )}
    </Card>
  )
}
