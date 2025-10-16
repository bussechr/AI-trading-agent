"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { ArrowUp, ArrowDown } from "lucide-react"

export function SignalsTable() {
  const signals = [
    {
      id: 1,
      symbol: "EUR/USD",
      direction: "LONG",
      entry: 1.085,
      exit: 1.092,
      pnl: 70,
      score: 0.72,
      status: "closed",
      time: "2024-01-15 14:30",
    },
    {
      id: 2,
      symbol: "GBP/JPY",
      direction: "SHORT",
      entry: 185.2,
      exit: 184.5,
      pnl: 70,
      score: 0.65,
      status: "closed",
      time: "2024-01-15 13:15",
    },
    {
      id: 3,
      symbol: "USD/CHF",
      direction: "LONG",
      entry: 0.865,
      exit: null,
      pnl: 15,
      score: 0.58,
      status: "open",
      time: "2024-01-15 15:45",
    },
    {
      id: 4,
      symbol: "AUD/USD",
      direction: "SHORT",
      entry: 0.672,
      exit: 0.668,
      pnl: 40,
      score: 0.61,
      status: "closed",
      time: "2024-01-15 12:00",
    },
    {
      id: 5,
      symbol: "NZD/USD",
      direction: "LONG",
      entry: 0.615,
      exit: 0.611,
      pnl: -40,
      score: 0.55,
      status: "closed",
      time: "2024-01-15 11:30",
    },
  ]

  return (
    <Card className="p-6">
      <div className="overflow-x-auto">
        <table className="w-full">
          <thead>
            <tr className="border-b border-border">
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Symbol</th>
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Direction</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">Entry</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">Exit</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">P&L</th>
              <th className="text-right py-3 px-4 text-sm font-medium text-muted-foreground">Score</th>
              <th className="text-center py-3 px-4 text-sm font-medium text-muted-foreground">Status</th>
              <th className="text-left py-3 px-4 text-sm font-medium text-muted-foreground">Time</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((signal) => (
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
                <td className="py-3 px-4 text-right text-foreground">{signal.entry}</td>
                <td className="py-3 px-4 text-right text-foreground">{signal.exit || "-"}</td>
                <td
                  className={`py-3 px-4 text-right font-medium ${signal.pnl >= 0 ? "text-green-500" : "text-red-500"}`}
                >
                  {signal.pnl >= 0 ? "+" : ""}
                  {signal.pnl} pips
                </td>
                <td className="py-3 px-4 text-right text-foreground">{signal.score}</td>
                <td className="py-3 px-4 text-center">
                  <Badge variant={signal.status === "open" ? "default" : "secondary"}>{signal.status}</Badge>
                </td>
                <td className="py-3 px-4 text-sm text-muted-foreground">{signal.time}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  )
}
