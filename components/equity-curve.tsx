"use client"

import { useMemo } from "react"
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Card } from "@/components/ui/card"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"

type EquityPoint = {
  ts: number
  label: string
  equity: number
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

export function EquityCurve() {
  const { history, loading } = useTradingHistory(5000)

  const data = useMemo(() => {
    const reports = Array.isArray(history.reports) ? history.reports : []
    const points: EquityPoint[] = []
    for (const row of reports) {
      const equity = parseHeartbeatEquity(row)
      const ts = Number(row?.time || row?.ts || 0)
      if (equity !== null && Number.isFinite(ts) && ts > 0) {
        points.push({
          ts,
          label: new Date(ts * 1000).toLocaleTimeString(),
          equity,
        })
      }
    }

    points.sort((a, b) => a.ts - b.ts)
    const deduped: EquityPoint[] = []
    let last = Number.NaN
    for (const point of points) {
      if (!Number.isFinite(last) || Math.abs(point.equity - last) > 1e-9) {
        deduped.push(point)
        last = point.equity
      }
    }
    return deduped.slice(-200)
  }, [history.reports])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground">Equity Curve</h3>
      <p className="mt-1 text-sm text-muted-foreground">Historical heartbeat samples only. No live-state fallback.</p>

      {loading && data.length === 0 ? (
        <div className="flex h-[300px] items-center justify-center text-muted-foreground">Loading equity history…</div>
      ) : data.length < 2 ? (
        <div className="flex h-[300px] items-center justify-center text-muted-foreground">Not enough heartbeat samples yet.</div>
      ) : (
        <div className="mt-5">
          <ResponsiveContainer width="100%" height={300}>
            <AreaChart data={data}>
              <defs>
                <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--color-chart-2)" stopOpacity={0.32} />
                  <stop offset="95%" stopColor="var(--color-chart-2)" stopOpacity={0.02} />
                </linearGradient>
              </defs>
              <XAxis dataKey="label" stroke="var(--color-muted-foreground)" fontSize={12} tickLine={false} axisLine={false} />
              <YAxis
                stroke="var(--color-muted-foreground)"
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickFormatter={(value) => `$${(value / 1000).toFixed(1)}k`}
              />
              <Tooltip
                contentStyle={{
                  backgroundColor: "var(--color-card)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "16px",
                }}
              />
              <Area type="monotone" dataKey="equity" stroke="var(--color-chart-2)" strokeWidth={2} fill="url(#equityGradient)" />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </Card>
  )
}
