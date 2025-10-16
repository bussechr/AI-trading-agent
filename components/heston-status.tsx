"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { CheckCircle2, AlertCircle } from "lucide-react"

export function HestonStatus() {
  const calibrations = [
    {
      symbol: "EUR/USD",
      status: "calibrated",
      v0: 0.0012,
      theta: 0.0015,
      kappa: 2.5,
      sigma: 0.3,
      rho: -0.7,
      lastUpdate: "5 min ago",
    },
    {
      symbol: "GBP/JPY",
      status: "calibrated",
      v0: 0.0018,
      theta: 0.002,
      kappa: 2.2,
      sigma: 0.35,
      rho: -0.65,
      lastUpdate: "8 min ago",
    },
    {
      symbol: "USD/CHF",
      status: "stale",
      v0: 0.001,
      theta: 0.0012,
      kappa: 2.8,
      sigma: 0.28,
      rho: -0.72,
      lastUpdate: "45 min ago",
    },
  ]

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-foreground">Heston Model Calibration</h3>
        <Badge variant="outline" className="bg-green-500/10 text-green-500 border-green-500/20">
          Guard Active
        </Badge>
      </div>

      <div className="space-y-3">
        {calibrations.map((cal, i) => (
          <div key={i} className="p-4 rounded-lg bg-accent/50 border border-border">
            <div className="flex items-center justify-between mb-3">
              <div className="flex items-center gap-2">
                <span className="font-medium text-foreground">{cal.symbol}</span>
                {cal.status === "calibrated" ? (
                  <CheckCircle2 className="h-4 w-4 text-green-500" />
                ) : (
                  <AlertCircle className="h-4 w-4 text-yellow-500" />
                )}
              </div>
              <span className="text-xs text-muted-foreground">{cal.lastUpdate}</span>
            </div>

            <div className="grid grid-cols-5 gap-3 text-sm">
              <div>
                <div className="text-xs text-muted-foreground">v₀</div>
                <div className="font-medium text-foreground">{cal.v0}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">θ</div>
                <div className="font-medium text-foreground">{cal.theta}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">κ</div>
                <div className="font-medium text-foreground">{cal.kappa}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">σ</div>
                <div className="font-medium text-foreground">{cal.sigma}</div>
              </div>
              <div>
                <div className="text-xs text-muted-foreground">ρ</div>
                <div className="font-medium text-foreground">{cal.rho}</div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </Card>
  )
}
