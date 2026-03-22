"use client"

import { useMemo } from "react"
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Card } from "@/components/ui/card"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"

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
  const reportJson = row?.report_json
  if (reportJson && typeof reportJson === "object") {
    const eq = Number(reportJson.equity || reportJson.eq || 0)
    if (Number.isFinite(eq) && eq > 0) return eq
  }
  const msg = String(row?.message || row?.report_text || "")
  const match = msg.match(/\beq=([0-9]+(?:\.[0-9]+)?)/i)
  if (!match) return null
  const eq = Number(match[1])
  return Number.isFinite(eq) && eq > 0 ? eq : null
}

export function DrawdownChart() {
  const { history, loading } = useTradingHistory(5000)

  const drawdown = useMemo(() => {
    const reports = Array.isArray(history.reports) ? history.reports : []
    const samples: Array<{ ts: number; equity: number }> = []
    for (const row of reports) {
      const equity = parseHeartbeatEquity(row)
      const ts = Number(row?.time || row?.ts || 0)
      if (equity !== null && Number.isFinite(ts) && ts > 0) {
        samples.push({ ts, equity })
      }
    }
    samples.sort((a, b) => a.ts - b.ts)
    if (samples.length === 0) return [] as DrawdownPoint[]

    let peak = samples[0].equity
    const points: DrawdownPoint[] = []
    for (const sample of samples.slice(-120)) {
      peak = Math.max(peak, sample.equity)
      points.push({
        label: new Date(sample.ts * 1000).toLocaleTimeString(),
        drawdown: sample.equity - peak,
      })
    }
    return points
  }, [history.reports])

  const stats = useMemo(() => {
    if (drawdown.length === 0) return { max: 0, avg: 0, latest: 0 }
    const values = drawdown.map((point) => Number(point.drawdown || 0))
    const maxLoss = Math.min(...values)
    const avgLoss = values.reduce((sum, value) => sum + value, 0) / values.length
    const latest = values[values.length - 1] || 0
    return { max: maxLoss, avg: avgLoss, latest }
  }, [drawdown])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground">Drawdown Analysis</h3>
      <p className="mt-1 text-sm text-muted-foreground">Historical drawdown from stored heartbeat samples only.</p>

      {loading && drawdown.length === 0 ? (
        <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading drawdown history…</div>
      ) : drawdown.length < 2 ? (
        <div className="flex h-[220px] items-center justify-center text-muted-foreground">Not enough history yet.</div>
      ) : (
        <div className="mt-5">
          <ResponsiveContainer width="100%" height={220}>
            <BarChart data={drawdown}>
              <XAxis dataKey="label" stroke="var(--color-muted-foreground)" fontSize={12} tickLine={false} axisLine={false} />
              <YAxis
                stroke="var(--color-muted-foreground)"
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickFormatter={(value) => `$${value}`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "var(--color-card)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "16px",
                }}
              />
              <Bar dataKey="drawdown" fill="var(--color-destructive)" radius={[8, 8, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="mt-5 grid grid-cols-3 gap-4 border-t border-border/70 pt-5">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Max Drawdown</div>
          <div className="mt-1 text-lg font-semibold text-rose-500">${stats.max.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Average</div>
          <div className="mt-1 text-lg font-semibold text-foreground">${stats.avg.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Latest</div>
          <div className="mt-1 text-lg font-semibold text-foreground">${stats.latest.toFixed(2)}</div>
        </div>
      </div>
    </Card>
  )
}
