"use client"

import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"

export function VolatilitySurface() {
  const { state, loading: liveLoading } = useLiveBridgeState(3000)
  const { history, loading: historyLoading } = useTradingHistory(3000)
  const loading = liveLoading || historyLoading
  const envelope = history.metrics?.risk_envelope || state?.riskEnvelope || {}
  const vol = Number(envelope?.volatility || state?.vol || 0)
  const regime = String(envelope?.regime || "unknown")
  const soft = Number(envelope?.soft_dd_pct || 0)
  const hard = Number(envelope?.hard_dd_pct || 0)
  const daily = Number(envelope?.daily_breaker_pct || 0)

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Adaptive Risk Surface</h3>

      <div className="aspect-video bg-accent/30 rounded-lg flex items-center justify-center border border-border">
        <div className="text-center text-muted-foreground">
          {loading ? (
            <>
              <div className="text-sm">Loading volatility regime...</div>
              <div className="text-xs mt-1">Waiting for runtime metrics</div>
            </>
          ) : (
            <>
              <div className="text-sm">Regime: {regime}</div>
              <div className="text-xs mt-1">Volatility: {vol.toFixed(6)}</div>
            </>
          )}
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-border">
        <div>
          <div className="text-xs text-muted-foreground">Soft DD</div>
          <div className="text-lg font-semibold text-foreground">{(soft * 100).toFixed(2)}%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Hard DD</div>
          <div className="text-lg font-semibold text-foreground">{(hard * 100).toFixed(2)}%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Daily Breaker</div>
          <div className="text-lg font-semibold text-foreground">{(daily * 100).toFixed(2)}%</div>
        </div>
      </div>
    </Card>
  )
}
