"use client"

import { useMemo } from "react"
import { Card } from "@/components/ui/card"
import { TimeSeriesAreaChart } from "@/components/time-series-area-chart"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { buildEquitySamples, downsampleEquitySamples, formatChartTimestamp } from "@/lib/trading/performance"

export function EquityCurve() {
  const { state } = useLiveBridgeState(5000)
  const { history, loading } = useTradingHistory(5000)

  const data = useMemo(() => {
    const samples = buildEquitySamples(Array.isArray(history.reports) ? history.reports : [], {
      equity: state?.displayEquity,
      ts: state?.lastHeartbeat,
    })
    return downsampleEquitySamples(samples, 240).map((sample) => ({ ts: sample.ts, value: sample.equity }))
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
          <TimeSeriesAreaChart
            accessibleLabel="Equity history"
            color="var(--color-chart-2)"
            data={data}
            formatTimestamp={formatChartTimestamp}
            formatValue={(value) => `$${value.toFixed(2)}`}
            formatYAxis={(value) => `$${(value / 1000).toFixed(1)}k`}
            gradientId="equity-gradient"
            height={300}
            seriesLabel="Equity"
          />
        </div>
      )}
    </Card>
  )
}
