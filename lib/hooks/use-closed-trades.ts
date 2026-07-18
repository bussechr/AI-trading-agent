"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"
import { mergeClosedTradePayload } from "@/lib/trading/closed-trades-normalize"

export interface ClosedTrade {
  ticket: number
  symbol: string
  broker_symbol: string
  side: string
  type: number
  lots: number
  open_price: number
  close_price: number
  open_time: string | null
  close_time: string | null
  close_time_epoch: number | null
  profit: number
  swap: number
  commission: number
  net_profit: number
  duration_secs: number | null
  report_ts: number
}

export interface ClosedTradeSummary {
  closedTrades: number
  wins: number
  losses: number
  winRate: number | null
  realizedNet: number
  averageNet: number | null
  closedTrades24h: number
  wins24h: number
  losses24h: number
  winRate24h: number | null
  realizedNet24h: number
  averageNet24h: number | null
}

export interface ClosedTradeSnapshot {
  trades: ClosedTrade[]
  summary: ClosedTradeSummary
  loading: boolean
  error: string | null
}

const EMPTY_SUMMARY: ClosedTradeSummary = {
  closedTrades: 0,
  wins: 0,
  losses: 0,
  winRate: null,
  realizedNet: 0,
  averageNet: null,
  closedTrades24h: 0,
  wins24h: 0,
  losses24h: 0,
  winRate24h: null,
  realizedNet24h: 0,
  averageNet24h: null,
}

const useSharedClosedTrades = createSharedPollingHook<ClosedTradeSnapshot>({
  initialSnapshot: {
    trades: [],
    summary: EMPTY_SUMMARY,
    loading: true,
    error: null,
  },
  poll: async (current) => {
    try {
      const response = await fetch("/api/trading/closed-trades?limit=300", { cache: "no-store" })
      const payload = await response.json()
      const merged = mergeClosedTradePayload<ClosedTrade, ClosedTradeSummary>(
        { trades: current.trades, summary: current.summary },
        payload,
        response.ok,
      )
      return {
        trades: merged.trades,
        summary: merged.summary,
        loading: false,
        error: merged.error,
      }
    } catch (error: any) {
      return {
        trades: current.trades,
        summary: current.summary,
        loading: false,
        error: error?.message || "Closed-trade polling error",
      }
    }
  },
})

export function useClosedTrades(refreshInterval = 10000): ClosedTradeSnapshot {
  return useSharedClosedTrades(refreshInterval)
}
