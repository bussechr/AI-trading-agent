"use client"

import { useMemo } from "react"
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { buildEquitySamples, formatChartTimestamp } from "@/lib/trading/performance"

export function EquityCurve() {
  const { state } = useLiveBridgeState(5000)
  const { history, loading } = useTradingHistory(5000)

  const data = useMemo(() => {
    return buildEquitySamples(Array.isArray(history.reports) ? history.reports : [], {
      equity: state?.displayEquity,
      ts: state?.lastHeartbeat,
    }).slice(-240)
  }, [history.reports, state?.displayEquity, state?.lastHeartbeat])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground">Equity Curve</h3>
      <p className="mt-1 text-sm text-muted-foreground">Heartbeat equity history with the live MT4 tail appended when fresh.</p>

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
              <CartesianGrid stroke="var(--color-border)" strokeDasharray="3 3" vertical={false} />
              <XAxis
                dataKey="ts"
                type="number"
                scale="time"
                domain={["dataMin", "dataMax"]}
                stroke="var(--color-muted-foreground)"
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickFormatter={(value) => formatChartTimestamp(Number(value))}
                minTickGap={32}
              />
              <YAxis
                stroke="var(--color-muted-foreground)"
                fontSize={12}
                tickLine={false}
                axisLine={false}
                tickFormatter={(value) => `$${(value / 1000).toFixed(1)}k`}
              />
              <Tooltip
                labelFormatter={(value) => formatChartTimestamp(Number(value))}
                formatter={(value) => {
                  const amount = Number(value ?? 0)
                  return [`$${amount.toFixed(2)}`, "Equity"]
                }}
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
