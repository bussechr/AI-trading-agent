"use client"

import { Card } from "@/components/ui/card"
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { useMemo } from "react"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

type DrawdownPoint = {
  label: string
  drawdown: number
}

function parseHeartbeatEquity(row: Record<string, any>): number | null {
  const js = row?.json
  if (js && typeof js === "object") {
    const typ = String(js.type || "").toUpperCase()
    if (typ === "HEARTBEAT") {
      const eq = Number(js.equity || 0)
      if (Number.isFinite(eq) && eq > 0) return eq
    }
  }
  const msg = String(row?.message || "")
  const m = msg.match(/\\beq=([0-9]+(?:\\.[0-9]+)?)/i)
  if (!m) return null
  const eq = Number(m[1])
  return Number.isFinite(eq) && eq > 0 ? eq : null
}

export function DrawdownChart() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const drawdown = useMemo(() => {
    const reports = Array.isArray(telemetry.reports) ? telemetry.reports : []
    const samples: Array<{ ts: number; equity: number }> = []
    for (const row of reports) {
      const eq = parseHeartbeatEquity(row)
      const ts = Number(row?.time || 0)
      if (eq !== null && Number.isFinite(ts) && ts > 0) {
        samples.push({ ts, equity: eq })
      }
    }
    samples.sort((a, b) => a.ts - b.ts)
    if (samples.length === 0) return [] as DrawdownPoint[]

    let peak = samples[0].equity
    const points: DrawdownPoint[] = []
    for (const s of samples.slice(-120)) {
      peak = Math.max(peak, s.equity)
      const dd = s.equity - peak
      points.push({
        label: new Date(s.ts * 1000).toLocaleTimeString(),
        drawdown: dd,
      })
    }
    return points
  }, [telemetry.reports])

  const stats = useMemo(() => {
    if (drawdown.length === 0) {
      return { max: 0, avg: 0, latest: 0 }
    }
    const vals = drawdown.map((x) => Number(x.drawdown || 0))
    const maxLoss = Math.min(...vals)
    const avgLoss = vals.reduce((acc, x) => acc + x, 0) / vals.length
    const latest = vals[vals.length - 1] || 0
    return { max: maxLoss, avg: avgLoss, latest }
  }, [drawdown])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Drawdown Analysis</h3>

      {loading && drawdown.length === 0 ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground">Loading drawdown...</div>
      ) : drawdown.length < 2 ? (
        <div className="h-[200px] flex items-center justify-center text-muted-foreground">Not enough history yet</div>
      ) : (
        <ResponsiveContainer width="100%" height={200}>
          <BarChart data={drawdown}>
            <XAxis
              dataKey="label"
              stroke="hsl(var(--muted-foreground))"
              fontSize={12}
              tickLine={false}
              axisLine={false}
            />
            <YAxis
              stroke="hsl(var(--muted-foreground))"
              fontSize={12}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => `$${value}`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: "8px",
              }}
            />
            <Bar dataKey="drawdown" fill="hsl(var(--destructive))" radius={[4, 4, 0, 0]} />
          </BarChart>
        </ResponsiveContainer>
      )}

      <div className="grid grid-cols-3 gap-4 mt-4 pt-4 border-t border-border">
        <div>
          <div className="text-xs text-muted-foreground">Max Drawdown</div>
          <div className="text-lg font-semibold text-destructive">${stats.max.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Avg Drawdown</div>
          <div className="text-lg font-semibold text-foreground">${stats.avg.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-xs text-muted-foreground">Latest Drawdown</div>
          <div className="text-lg font-semibold text-foreground">${stats.latest.toFixed(2)}</div>
        </div>
      </div>
    </Card>
  )
}
