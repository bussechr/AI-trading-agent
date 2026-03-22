"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"

export function GovernanceTimeline() {
  const { history, loading } = useTradingHistory(3000)
  const events = Array.isArray(history.governanceEvents) ? history.governanceEvents.slice().reverse().slice(0, 12) : []

  return (
    <Card className="p-6">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold text-foreground">Governance Timeline</h3>
        <Badge variant="outline">{events.length} events</Badge>
      </div>

      {loading && events.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">Loading governance events...</div>
      ) : events.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">No governance transitions yet</div>
      ) : (
        <div className="space-y-3">
          {events.map((ev, idx) => {
            const t = Number(ev.time || 0)
            const eventType = String(ev.event_type || "state_update")
            const reason = String(ev.reason || "")
            const paused = Boolean(ev?.payload?.governance?.paused)
            const riskScale = Number(ev?.payload?.governance?.risk_scale || 1)
            return (
              <div key={`${t}-${idx}`} className="rounded-lg border border-border bg-accent/40 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <Badge variant={paused ? "destructive" : "secondary"}>{eventType}</Badge>
                    <span className="text-sm text-foreground">{reason || "governance_update"}</span>
                  </div>
                  <span className="text-xs text-muted-foreground">{t > 0 ? new Date(t * 1000).toLocaleString() : "—"}</span>
                </div>
                <div className="mt-2 text-xs text-muted-foreground">
                  paused={String(paused)} | risk_scale={riskScale.toFixed(2)}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
