"use client"

import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useClosedTrades } from "@/lib/hooks/use-closed-trades"
import { cn } from "@/lib/utils"

function formatCurrency(value: number | null | undefined): string {
  const amount = Number(value)
  return Number.isFinite(amount) ? `$${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}` : "—"
}

function formatPct(value: number | null | undefined): string {
  const pct = Number(value)
  return Number.isFinite(pct) ? `${pct.toFixed(2)}%` : "—"
}

export function ClosedTradePerformance() {
  const { trades, summary, loading, error } = useClosedTrades(10000)
  const recent = Array.isArray(trades) ? trades.slice(0, 10) : []

  const statCards = [
    {
      label: "Closed Trades",
      value: loading ? "..." : String(summary.closedTrades),
      detail: `${summary.wins} wins | ${summary.losses} losses`,
      accent: "text-foreground",
    },
    {
      label: "Win Rate",
      value: loading ? "..." : formatPct(summary.winRate),
      detail: `${summary.closedTrades24h} trades in last 24h`,
      accent: "text-foreground",
    },
    {
      label: "Realized Net",
      value: loading ? "..." : formatCurrency(summary.realizedNet),
      detail: `24h ${formatCurrency(summary.realizedNet24h)}`,
      accent: summary.realizedNet >= 0 ? "text-emerald-400" : "text-rose-400",
    },
    {
      label: "Avg Net / Trade",
      value: loading ? "..." : formatCurrency(summary.averageNet),
      detail: `24h ${formatCurrency(summary.averageNet24h)}`,
      accent: (summary.averageNet || 0) >= 0 ? "text-emerald-400" : "text-rose-400",
    },
  ]

  return (
    <Card className="p-6">
      <div className="mb-5 flex items-center justify-between gap-4">
        <div>
          <h3 className="text-lg font-semibold text-foreground">Closed Trade Performance</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            Broker-realized history from MT4 account closes, not command acknowledgements.
          </p>
        </div>
        <Badge variant="outline">{recent.length} recent</Badge>
      </div>

      <div className="mb-6 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        {statCards.map((card) => (
          <div key={card.label} className="rounded-2xl border border-border/70 bg-background/50 p-4">
            <div className="text-xs uppercase tracking-[0.18em] text-muted-foreground">{card.label}</div>
            <div className={cn("mt-2 text-2xl font-semibold", card.accent)}>{card.value}</div>
            <div className="mt-1 text-xs text-muted-foreground">{card.detail}</div>
          </div>
        ))}
      </div>

      {error && recent.length === 0 ? (
        <div className="rounded-xl border border-border/70 bg-background/40 px-4 py-6 text-sm text-muted-foreground">
          {error}
        </div>
      ) : loading && recent.length === 0 ? (
        <div className="rounded-xl border border-border/70 bg-background/40 px-4 py-6 text-sm text-muted-foreground">
          Loading closed-trade history...
        </div>
      ) : recent.length === 0 ? (
        <div className="rounded-xl border border-border/70 bg-background/40 px-4 py-6 text-sm text-muted-foreground">
          No closed trades have been published yet. The bridge will populate this after the updated EA emits MT4 account-history closes.
        </div>
      ) : (
        <div className="space-y-3">
          {recent.map((trade) => (
            <div key={`${trade.ticket}-${trade.close_time_epoch}-${trade.lots}`} className="rounded-xl border border-border/70 bg-background/40 px-4 py-3">
              <div className="flex items-center justify-between gap-4">
                <div className="flex items-center gap-3">
                  <Badge variant={trade.side === "BUY" ? "secondary" : "outline"}>{trade.side}</Badge>
                  <div>
                    <div className="font-medium text-foreground">{trade.symbol}</div>
                    <div className="text-xs text-muted-foreground">
                      {trade.lots.toFixed(2)} lots · ticket {trade.ticket}
                    </div>
                  </div>
                </div>
                <div className={cn("text-right text-sm font-medium", trade.net_profit >= 0 ? "text-emerald-400" : "text-rose-400")}>
                  {formatCurrency(trade.net_profit)}
                </div>
              </div>
              <div className="mt-2 flex items-center justify-between gap-4 text-xs text-muted-foreground">
                <span>
                  {trade.open_price.toFixed(5)} → {trade.close_price.toFixed(5)}
                </span>
                <span>{trade.close_time ? new Date(trade.close_time).toLocaleString() : "—"}</span>
              </div>
            </div>
          ))}
        </div>
      )}
    </Card>
  )
}
