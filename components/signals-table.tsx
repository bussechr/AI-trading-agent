"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ArrowUp, ArrowDown } from "lucide-react"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

export function SignalsTable() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const commands = Array.isArray(telemetry.commands) ? telemetry.commands.slice(0, 200) : []

  const rows = commands.map((cmd) => {
    const side = String(cmd.cmd || "").toUpperCase() === "BUY" ? "LONG" : String(cmd.cmd || "").toUpperCase() === "SELL" ? "SHORT" : String(cmd.cmd || "").toUpperCase()
    const ticket = Number(cmd.ack?.ticket || -1)
    const errorCode = Number(cmd.ack?.error_code || 0)
    const reason = String(cmd.reason || cmd.ack?.message || "")
    return {
      id: String(cmd.command_id),
      symbol: String(cmd.symbol || "—"),
      direction: side,
      entry: Number(cmd.tp_price || 0),
      stop: Number(cmd.sl_price || 0),
      lots: Number(cmd.lots || 0),
      status: String(cmd.status || "unknown"),
      ticket,
      errorCode,
      reason,
      time: Number(cmd.created_at || 0),
      updatedAt: Number(cmd.updated_at || 0),
      deliveredCount: Number(cmd.delivered_count || 0),
    }
  })

  return (
    <Card className="p-6">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Symbol</th>
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Direction</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">Lots</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">TP</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">SL</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">Ticket</th>
              <th className="text-center py-3 px-4 text-sm font-medium text-muted-foreground">Status</th>
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Reason</th>
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Time</th>
            </tr>
          </thead>
          <tbody>
            {loading ? (
              <tr>
                <td className="py-8 px-4 text-center text-muted-foreground" colSpan={9}>
                  Loading command history...
                </td>
              </tr>
            ) : rows.length === 0 ? (
              <tr>
                <td className="py-8 px-4 text-center text-muted-foreground" colSpan={9}>
                  No command history yet
                </td>
              </tr>
            ) : (
              rows.map((signal) => (
                <tr key={signal.id} className="border-b border-border hover:bg-accent/50 transition-colors">
                  <td className="py-3 px-4 font-medium text-foreground">{signal.symbol}</td>
                <td className="py-3 px-4">
                  <div className="flex items-center gap-2">
                    {signal.direction === "LONG" ? (
                      <ArrowUp className="h-4 w-4 text-green-500" />
                    ) : (
                      <ArrowDown className="h-4 w-4 text-red-500" />
                    )}
                    <span className={signal.direction === "LONG" ? "text-green-500" : "text-red-500"}>
                      {signal.direction}
                    </span>
                  </div>
                </td>
                  <td className="py-3 px-4 text-right text-foreground">{signal.lots.toFixed(2)}</td>
                  <td className="py-3 px-4 text-right text-foreground">{signal.entry > 0 ? signal.entry.toFixed(5) : "-"}</td>
                  <td className="py-3 px-4 text-right text-foreground">{signal.stop > 0 ? signal.stop.toFixed(5) : "-"}</td>
                  <td className="py-3 px-4 text-right text-foreground">{signal.ticket > 0 ? signal.ticket : "-"}</td>
                <td className="py-3 px-4 text-center">
                    <Badge
                      variant={signal.status === "acked" ? "default" : signal.status === "failed" ? "destructive" : "secondary"}
                    >
                      {signal.status}
                    </Badge>
                </td>
                  <td className="py-3 px-4 text-xs text-muted-foreground">{signal.reason || "—"}</td>
                  <td className="py-3 px-4 text-sm text-muted-foreground">
                    {signal.time > 0 ? new Date(signal.time * 1000).toLocaleString() : "—"}
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
