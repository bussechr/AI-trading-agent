"use client"

import { Card } from "@/components/ui/card"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

function fmtPct(v: number): string {
  return `${(v * 100).toFixed(2)}%`
}

export function PipelineHealth() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const metrics = telemetry.metrics || {}
  const pipeline = metrics.decision_pipeline || {}
  const taxonomy = pipeline.rejection_taxonomy || {}
  const attribution = pipeline.stage_attribution || {}
  const rows = Array.isArray(attribution.pipeline_rows) ? attribution.pipeline_rows : []

  const entries = Object.entries(taxonomy as Record<string, number>).sort((a, b) => Number(b[1]) - Number(a[1]))
  const accepted = rows.filter((r: any) => Boolean(r?.outcome?.execution_ready)).length
  const total = rows.length

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Decision Pipeline Health</h3>

      <div className="grid gap-3 md:grid-cols-3 mb-4">
        <div className="rounded-lg border border-border p-3">
          <div className="text-xs text-muted-foreground">Snapshots (5m)</div>
          <div className="text-xl font-semibold text-foreground">{loading ? "..." : Number(pipeline.snapshots_5m || 0)}</div>
        </div>
        <div className="rounded-lg border border-border p-3">
          <div className="text-xs text-muted-foreground">Entry Ready</div>
          <div className="text-xl font-semibold text-foreground">{loading ? "..." : `${accepted}/${total}`}</div>
        </div>
        <div className="rounded-lg border border-border p-3">
          <div className="text-xs text-muted-foreground">Acceptance Rate</div>
          <div className="text-xl font-semibold text-foreground">{loading || total === 0 ? "..." : fmtPct(accepted / total)}</div>
        </div>
      </div>

      <div className="space-y-2">
        <div className="text-sm font-medium text-foreground">Rejection Taxonomy</div>
        {entries.length === 0 ? (
          <div className="text-sm text-muted-foreground">No rejection taxonomy available yet</div>
        ) : (
          entries.slice(0, 8).map(([reason, count]) => (
            <div key={reason} className="flex items-center justify-between text-sm rounded-md bg-accent/40 px-3 py-2">
              <span className="text-foreground">{reason}</span>
              <span className="text-muted-foreground">{Number(count)}</span>
            </div>
          ))
        )}
      </div>
    </Card>
  )
}
