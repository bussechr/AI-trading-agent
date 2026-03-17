"use client"

import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

function statusVariant(status: string): "default" | "secondary" | "destructive" | "outline" {
  const s = String(status || "").toLowerCase()
  if (s === "acked") return "default"
  if (s === "failed" || s === "expired") return "destructive"
  if (s === "delivered") return "secondary"
  return "outline"
}

export function CommandLifecycleTimeline() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const events = Array.isArray(telemetry.commandEvents) ? telemetry.commandEvents.slice().reverse().slice(0, 16) : []

  return (
    <Card className="p-6">
      <div className="mb-4 flex items-center justify-between">
        <h3 className="text-lg font-semibold text-foreground">Command Lifecycle</h3>
        <Badge variant="outline">{events.length} events</Badge>
      </div>

      {loading && events.length === 0 ? (
        <div className="py-8 text-center text-muted-foreground">Loading lifecycle events...</div>
      ) : events.length === 0 ? (
        <div className="py-8 text-center text-muted-foreground">No lifecycle events yet</div>
      ) : (
        <div className="space-y-3">
          {events.map((event, idx) => {
            const ts = Number(event.time || 0)
            const status = String(event.status || "unknown")
            const commandId = String(event.command_id || "")
            const reason = String(event.reason || "")
            return (
              <div key={`${commandId}-${ts}-${idx}`} className="rounded-lg border border-border bg-accent/40 p-3">
                <div className="flex items-center justify-between gap-3">
                  <div className="flex items-center gap-2">
                    <Badge variant={statusVariant(status)}>{status}</Badge>
                    <span className="font-mono text-xs text-foreground">{commandId.slice(0, 12)}</span>
                  </div>
                  <span className="text-xs text-muted-foreground">
                    {ts > 0 ? new Date(ts * 1000).toLocaleString() : "—"}
                  </span>
                </div>
                <div className="mt-2 text-xs text-muted-foreground">{reason || "no_reason"}</div>
              </div>
            )
          })}
        </div>
      )}
    </Card>
  )
}
