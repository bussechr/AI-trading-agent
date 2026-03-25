"use client"

import { useMemo } from "react"
import { Area, AreaChart, CartesianGrid, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import {
  buildDrawdownSamples,
  buildEquitySamples,
  downsampleDrawdownSamples,
  formatChartTimestamp,
} from "@/lib/trading/performance"

export function DrawdownChart() {
  const { state } = useLiveBridgeState(5000)
  const { history, loading } = useTradingHistory(5000)

  const drawdown = useMemo(() => {
    const samples = buildEquitySamples(Array.isArray(history.reports) ? history.reports : [], {
      equity: state?.displayEquity,
      ts: state?.lastHeartbeat,
    })
    return downsampleDrawdownSamples(buildDrawdownSamples(samples), 240)
  }, [history.reports, state?.displayEquity, state?.lastHeartbeat])

  const stats = useMemo(() => {
    if (drawdown.length === 0) return { max: 0, avg: 0, latest: 0, maxPct: 0, latestPct: 0 }
    const values = drawdown.map((point) => Number(point.drawdown || 0))
    const pctValues = drawdown.map((point) => Number(point.drawdownPct || 0))
    const maxLoss = Math.min(...values)
    const avgLoss = values.reduce((sum, value) => sum + value, 0) / values.length
    const latest = values[values.length - 1] || 0
    const maxPct = Math.min(...pctValues)
    const latestPct = pctValues[pctValues.length - 1] || 0
    return { max: maxLoss, avg: avgLoss, latest, maxPct, latestPct }
  }, [drawdown])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground">Drawdown Analysis</h3>
      <p className="mt-1 text-sm text-muted-foreground">Rolling peak-to-equity drawdown rebuilt from the broader heartbeat history and live tail.</p>

      {loading && drawdown.length === 0 ? (
        <div className="flex h-[220px] items-center justify-center text-muted-foreground">Loading drawdown history…</div>
      ) : drawdown.length < 2 ? (
        <div className="flex h-[220px] items-center justify-center text-muted-foreground">Not enough history yet.</div>
      ) : (
        <div className="mt-5">
          <ResponsiveContainer width="100%" height={220}>
            <AreaChart data={drawdown}>
              <defs>
                <linearGradient id="drawdownGradient" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%" stopColor="var(--color-destructive)" stopOpacity={0.22} />
                  <stop offset="95%" stopColor="var(--color-destructive)" stopOpacity={0.03} />
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
                tickFormatter={(value) => `$${value}`}
              />
              <ReferenceLine y={0} stroke="var(--color-border)" strokeDasharray="4 4" />
              <Tooltip
                labelFormatter={(value) => formatChartTimestamp(Number(value))}
                formatter={(value, name) => {
                  const amount = Number(value ?? 0)
                  if (name === "drawdownPct") return [`${amount.toFixed(2)}%`, "Drawdown %"]
                  if (name === "peak") return [`$${amount.toFixed(2)}`, "Rolling Peak"]
                  return [`$${amount.toFixed(2)}`, "Drawdown"]
                }}
                contentStyle={{
                  backgroundColor: "var(--color-card)",
                  border: "1px solid var(--color-border)",
                  borderRadius: "16px",
                }}
              />
              <Area
                type="monotone"
                dataKey="drawdown"
                stroke="var(--color-destructive)"
                strokeWidth={2}
                fill="url(#drawdownGradient)"
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      <div className="mt-5 grid grid-cols-3 gap-4 border-t border-border/70 pt-5">
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Max Drawdown</div>
          <div className="mt-1 text-lg font-semibold text-rose-500">
            ${stats.max.toFixed(2)} <span className="text-sm text-muted-foreground">({stats.maxPct.toFixed(2)}%)</span>
          </div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Average</div>
          <div className="mt-1 text-lg font-semibold text-foreground">${stats.avg.toFixed(2)}</div>
        </div>
        <div>
          <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Latest</div>
          <div className="mt-1 text-lg font-semibold text-foreground">
            ${stats.latest.toFixed(2)} <span className="text-sm text-muted-foreground">({stats.latestPct.toFixed(2)}%)</span>
          </div>
        </div>
      </div>
    </Card>
  )
}
