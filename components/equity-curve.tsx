"use client"

import { Card } from "@/components/ui/card"
import { Area, AreaChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { useMemo } from "react"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

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
  const msg = String(row?.message || "")
  const m = msg.match(/\\beq=([0-9]+(?:\\.[0-9]+)?)/i)
  if (!m) return null
  const eq = Number(m[1])
  return Number.isFinite(eq) && eq > 0 ? eq : null
}

export function EquityCurve() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const data = useMemo(() => {
    const reports = Array.isArray(telemetry.reports) ? telemetry.reports : []
    const points: EquityPoint[] = []
    for (const row of reports) {
      const eq = parseHeartbeatEquity(row)
      const ts = Number(row?.time || 0)
      if (eq !== null && Number.isFinite(ts) && ts > 0) {
        points.push({
          ts,
          label: new Date(ts * 1000).toLocaleTimeString(),
          equity: eq,
        })
      }
    }

    points.sort((a, b) => a.ts - b.ts)
    const deduped: EquityPoint[] = []
    let last = -1
    for (const p of points) {
      if (Math.abs(p.equity - last) > 1e-9) {
        deduped.push(p)
        last = p.equity
      }
    }
    const tail = deduped.slice(-200)
    if (tail.length === 0) {
      const eq = Number(telemetry.state?.equity || 0)
      if (eq > 0) {
        return [
          {
            ts: Date.now() / 1000,
            label: new Date().toLocaleTimeString(),
            equity: eq,
          },
        ]
      }
    }
    return tail
  }, [telemetry.reports, telemetry.state?.equity])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Equity Curve (Live)</h3>

      {loading && data.length === 0 ? (
        <div className="h-[300px] flex items-center justify-center text-muted-foreground">Loading equity data...</div>
      ) : data.length < 2 ? (
        <div className="h-[300px] flex items-center justify-center text-muted-foreground">
          Not enough heartbeat samples yet
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={300}>
          <AreaChart data={data}>
            <defs>
              <linearGradient id="equityGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor="hsl(var(--primary))" stopOpacity={0.3} />
                <stop offset="95%" stopColor="hsl(var(--primary))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <XAxis dataKey="label" stroke="hsl(var(--muted-foreground))" fontSize={12} tickLine={false} axisLine={false} />
            <YAxis
              stroke="hsl(var(--muted-foreground))"
              fontSize={12}
              tickLine={false}
              axisLine={false}
              tickFormatter={(value) => `$${(value / 1000).toFixed(1)}k`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: "hsl(var(--card))",
                border: "1px solid hsl(var(--border))",
                borderRadius: "8px",
              }}
            />
            <Area
              type="monotone"
              dataKey="equity"
              stroke="hsl(var(--primary))"
              strokeWidth={2}
              fill="url(#equityGradient)"
            />
          </AreaChart>
        </ResponsiveContainer>
      )}
    </Card>
  )
}
