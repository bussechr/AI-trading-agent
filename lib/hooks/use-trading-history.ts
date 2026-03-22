"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"

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
  return response.json()
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
  error: string | null
  loading: boolean
}>({
  initialSnapshot: {
    history: EMPTY_HISTORY,
    error: null,
    loading: true,
  },
  poll: async (current) => {
    try {
      const [metricsRes, reportsRes, commandsRes, commandEventsRes, governanceRes] = await Promise.allSettled([
        fetchJson("/api/trading/metrics"),
        fetchJson("/api/trading/reports"),
        fetchJson("/api/trading/commands?limit=500"),
        fetchJson("/api/trading/command-events?limit=500"),
        fetchJson("/api/trading/governance?limit=500"),
      ])

      const metricsPayload = metricsRes.status === "fulfilled" ? metricsRes.value : { status: "error", data: {} }
      const reportsPayload = reportsRes.status === "fulfilled" ? reportsRes.value : { status: "error", reports: [] }
      const commandsPayload = commandsRes.status === "fulfilled" ? commandsRes.value : { status: "error", commands: [] }
      const commandEventsPayload =
        commandEventsRes.status === "fulfilled" ? commandEventsRes.value : { status: "error", events: [] }
      const governancePayload =
        governanceRes.status === "fulfilled" ? governanceRes.value : { status: "error", events: [] }

      return {
        history: {
          metrics: metricsPayload?.status === "success" ? metricsPayload.data || {} : current.history.metrics,
          reports: Array.isArray(reportsPayload?.reports) ? reportsPayload.reports : current.history.reports,
          commands: Array.isArray(commandsPayload?.commands) ? commandsPayload.commands : current.history.commands,
          commandEvents: Array.isArray(commandEventsPayload?.events)
            ? commandEventsPayload.events
            : current.history.commandEvents,
          governanceEvents: Array.isArray(governancePayload?.events)
            ? governancePayload.events
            : current.history.governanceEvents,
        },
        error: metricsPayload?.status === "error" ? metricsPayload?.error || "Metrics unavailable" : null,
        loading: false,
      }
    } catch (err: any) {
      return {
        history: current.history,
        error: err?.message || "Trading history polling error",
        loading: false,
      }
    }
  },
})

export function useTradingHistory(refreshInterval = 2500) {
  return useSharedTradingHistory(refreshInterval)
}
