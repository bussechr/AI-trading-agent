"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"
import { mergePinnedTradingHistorySnapshot } from "@/lib/trading/history-normalize"

export interface TradingCommand {
  command_id: string
  session_id: string
  proto: string
  cmd: string
  symbol: string
  lots: number
  tp_price?: number | null
  sl_price?: number | null
  intent: string
  status: string
  created_at: number
  updated_at: number
  delivered_count: number
  reason: string
  ack?: {
    status?: string
    ticket?: number
    error_code?: number
    message?: string
  }
}

export interface GovernanceEvent {
  time: number
  event_type: string
  reason: string
  payload?: Record<string, any>
}

export interface CommandLifecycleEvent {
  command_id: string
  status: string
  reason: string
  time: number
  payload?: Record<string, any>
}

export interface TradingHistory {
  metrics: Record<string, any>
  reports: Array<Record<string, any>>
  commands: TradingCommand[]
  commandEvents: CommandLifecycleEvent[]
  governanceEvents: GovernanceEvent[]
}

async function fetchJson(path: string): Promise<any> {
  const response = await fetch(path, { cache: "no-store" })
  const payload = await response.json()
  if (!response.ok) {
    const reason = String(payload?.error || payload?.detail || "").trim()
    throw new Error(`${path} -> HTTP ${response.status}${reason ? `: ${reason}` : ""}`)
  }
  if (payload?.status === "error") {
    throw new Error(`${path} -> ${String(payload.error || "error response")}`)
  }
  return payload
}

const EMPTY_HISTORY: TradingHistory = {
  metrics: {},
  reports: [],
  commands: [],
  commandEvents: [],
  governanceEvents: [],
}

const useSharedTradingHistory = createSharedPollingHook<{
  history: TradingHistory
  bridgeUrl: string | null
  error: string | null
  loading: boolean
}>({
  initialSnapshot: {
    history: EMPTY_HISTORY,
    bridgeUrl: null,
    error: null,
    loading: true,
  },
  poll: async (current) => {
    try {
      const payload = await fetchJson(
        "/api/trading/history?reports_limit=5000&commands_limit=500&events_limit=500&governance_limit=500",
      )
      const merged = mergePinnedTradingHistorySnapshot(
        { history: current.history, bridgeUrl: current.bridgeUrl },
        payload,
      )

      return {
        history: merged.history as TradingHistory,
        bridgeUrl: merged.bridgeUrl,
        error: merged.error,
        loading: false,
      }
    } catch (err: any) {
      return {
        history: current.history,
        bridgeUrl: current.bridgeUrl,
        error: err?.message || "Trading history polling error",
        loading: false,
      }
    }
  },
})

export function useTradingHistory(refreshInterval = 2500) {
  return useSharedTradingHistory(refreshInterval)
}
