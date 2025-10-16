"use client"

import { Card } from "@/components/ui/card"

export function VolatilitySurface() {
  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Volatility Surface</h3>

      <div className="aspect-video bg-accent/30 rounded-lg flex items-center justify-center border border-border">
        <div className="text-center text-muted-foreground">
          <div className="text-sm">3D Volatility Surface</div>
          <div className="text-xs mt-1">EUR/USD Implied Volatility</div>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-border">
        <div>
          <div className="text-xs text-muted-foreground">ATM Vol</div>
          <div className="text-lg font-semibold text-foreground">12.5%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">25Δ Skew</div>
          <div className="text-lg font-semibold text-foreground">-2.3%</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">25Δ RR</div>
          <div className="text-lg font-semibold text-foreground">1.8%</div>
        </div>
      </div>
    </Card>
  )
}
