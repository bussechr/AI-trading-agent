"use client"

import { Card } from "@/components/ui/card"

export function TradeStatistics() {
  const stats = [
    { label: "Total Trades", value: "247", subtext: "Last 30 days" },
    { label: "Win Rate", value: "54.2%", subtext: "134 wins / 113 losses" },
    { label: "Profit Factor", value: "1.42", subtext: "Gross profit / loss" },
    { label: "Sharpe Ratio", value: "1.18", subtext: "Risk-adjusted return" },
    { label: "Avg Win", value: "+$87", subtext: "Per winning trade" },
    { label: "Avg Loss", value: "-$62", subtext: "Per losing trade" },
    { label: "Best Trade", value: "+$340", subtext: "GBP/JPY Long" },
    { label: "Worst Trade", value: "-$215", subtext: "EUR/USD Short" },
  ]

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {stats.map((stat) => (
        <Card key={stat.label} className="p-6">
          <div className="text-sm text-muted-foreground mb-1">{stat.label}</div>
          <div className="text-2xl font-bold text-foreground mb-1">{stat.value}</div>
          <div className="text-xs text-muted-foreground">{stat.subtext}</div>
        </Card>
      ))}
    </div>
  )
}
