"use client"

import { useMemo } from "react"
import { Card } from "@/components/ui/card"
import { TimeSeriesAreaChart } from "@/components/time-series-area-chart"
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

  const chartData = useMemo(
    () => drawdown.map((sample) => ({ ts: sample.ts, value: sample.drawdown })),
    [drawdown],
  )

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
          <TimeSeriesAreaChart
            accessibleLabel="Rolling drawdown history"
            color="var(--color-destructive)"
            data={chartData}
            formatTimestamp={formatChartTimestamp}
            formatValue={(value) => `$${value.toFixed(2)}`}
            formatYAxis={(value) => `$${value.toFixed(0)}`}
            gradientId="drawdown-gradient"
            height={220}
            seriesLabel="Drawdown"
          />
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
