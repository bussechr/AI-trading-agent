"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { CheckCircle2, AlertCircle } from "lucide-react"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

export function HestonStatus() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const diagnostics = telemetry.state?.agent_diagnostics || {}
  const lastDiag = diagnostics?.last_diag || {}
  const top = diagnostics?.top_candidate || {}

  const symbol = String(top?.symbol || "N/A")
  const hestonScale = Number(lastDiag?.heston_scale || 1)
  const vol = Number(lastDiag?.vol || 0)
  const pTrend = Number(lastDiag?.p_trend || 0.5)
  const modelAgeSecs = Number(diagnostics?.model_age_secs || 0)

  const active = Math.abs(hestonScale - 1.0) > 1e-6
  const stale = modelAgeSecs > 1800

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-foreground">Heston Guard Status</h3>
        <Badge variant="outline" className={active ? "bg-green-500/10 text-green-500 border-green-500/20" : ""}>
          {active ? "Guard Active" : "Guard Neutral"}
        </Badge>
      </div>

      {loading ? (
        <div className="text-muted-foreground">Loading model diagnostics...</div>
      ) : (
        <div className="space-y-3">
          <div className="p-4 rounded-lg bg-accent/50 border border-border">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="font-medium text-foreground">{symbol}</span>
                {!stale ? (
                  <CheckCircle2 className="h-4 w-4 text-green-500" />
                ) : (
                  <AlertCircle className="h-4 w-4 text-yellow-500" />
                )}
              </div>
              <span className="text-xs text-muted-foreground">model age: {modelAgeSecs.toFixed(0)}s</span>
            </div>

            <div className="grid grid-cols-4 gap-3 text-sm">
              <div>
                <div className="text-xs text-muted-foreground">Scale</div>
                <div className="font-medium text-foreground">{hestonScale.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Vol</div>
                <div className="font-medium text-foreground">{vol.toFixed(5)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">p(trend)</div>
                <div className="font-medium text-foreground">{pTrend.toFixed(3)}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">Regime</div>
                <div className="font-medium text-foreground">{String(telemetry.metrics?.risk_envelope?.regime || "unknown")}</div>
              </div>
            </div>
          </div>
        </div>
      )}
    </Card>
  )
}
