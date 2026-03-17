"use client"

import { Card } from "@/components/ui/card"
import { useMemo } from "react"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

export function OptionsChain() {
  const { telemetry, loading } = useTradingTelemetry(3000)

  const rows = useMemo(() => {
    const map = new Map<string, { symbol: string; total: number; acked: number; failed: number; lotsSum: number }>()
    for (const cmd of telemetry.commands || []) {
      const symbol = String(cmd.symbol || "UNKNOWN")
      const cur = map.get(symbol) || { symbol, total: 0, acked: 0, failed: 0, lotsSum: 0 }
      cur.total += 1
      if (String(cmd.status) === "acked") cur.acked += 1
      if (String(cmd.status) === "failed") cur.failed += 1
      cur.lotsSum += Number(cmd.lots || 0)
      map.set(symbol, cur)
    }
    return Array.from(map.values())
      .sort((a, b) => b.total - a.total)
      .slice(0, 12)
      .map((r) => ({
        symbol: r.symbol,
        total: r.total,
        ackRate: r.acked / Math.max(r.total, 1),
        failRate: r.failed / Math.max(r.total, 1),
        avgLots: r.lotsSum / Math.max(r.total, 1),
      }))
  }, [telemetry.commands])

  return (
    <Card className="p-6">
      <h3 className="text-lg font-semibold text-foreground mb-4">Execution Chain (By Symbol)</h3>

      {loading && rows.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">Loading symbol execution stats...</div>
      ) : rows.length === 0 ? (
        <div className="text-muted-foreground py-8 text-center">No command history yet</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-border">
                <th className="text-left py-2 text-xs font-medium text-muted-foreground">Symbol</th>
                <th className="text-right py-2 text-xs font-medium text-muted-foreground">Total</th>
                <th className="text-right py-2 text-xs font-medium text-muted-foreground">Ack Rate</th>
                <th className="text-right py-2 text-xs font-medium text-muted-foreground">Fail Rate</th>
                <th className="text-right py-2 text-xs font-medium text-muted-foreground">Avg Lots</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((opt, i) => (
                <tr key={`${opt.symbol}-${i}`} className="border-b border-border">
                  <td className="py-2 font-medium text-foreground">{opt.symbol}</td>
                  <td className="py-2 text-right text-foreground">{opt.total}</td>
                  <td className="py-2 text-right text-foreground">{(opt.ackRate * 100).toFixed(1)}%</td>
                  <td className="py-2 text-right text-foreground">{(opt.failRate * 100).toFixed(1)}%</td>
                  <td className="py-2 text-right text-foreground">{opt.avgLots.toFixed(3)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  )
}
