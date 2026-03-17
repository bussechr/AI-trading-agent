"use client"

import { useEffect, useState } from "react"
import type { TradingState } from "@/lib/hooks/use-trading-state"

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

export interface TradingTelemetry {
  state: TradingState | null
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

export function useTradingTelemetry(refreshInterval = 2500) {
  const [telemetry, setTelemetry] = useState<TradingTelemetry>({
    state: null,
    metrics: {},
    reports: [],
    commands: [],
    commandEvents: [],
    governanceEvents: [],
  })
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let active = true

    const poll = async () => {
      try {
        const [stateRes, metricsRes, reportsRes, commandsRes, commandEventsRes, governanceRes] = await Promise.allSettled([
          fetchJson("/api/trading/state"),
          fetchJson("/api/trading/metrics"),
          fetchJson("/api/trading/reports"),
          fetchJson("/api/trading/commands?limit=500"),
          fetchJson("/api/trading/command-events?limit=500"),
          fetchJson("/api/trading/governance?limit=500"),
        ])

        if (!active) return

        const statePayload = stateRes.status === "fulfilled" ? stateRes.value : { status: "error", data: null }
        const metricsPayload =
          metricsRes.status === "fulfilled" ? metricsRes.value : { status: "error", data: {} }
        const reportsPayload =
          reportsRes.status === "fulfilled" ? reportsRes.value : { status: "error", reports: [] }
        const commandsPayload =
          commandsRes.status === "fulfilled" ? commandsRes.value : { status: "error", commands: [] }
        const commandEventsPayload =
          commandEventsRes.status === "fulfilled" ? commandEventsRes.value : { status: "error", events: [] }
        const governancePayload =
          governanceRes.status === "fulfilled" ? governanceRes.value : { status: "error", events: [] }

        setTelemetry((prev) => ({
          state: statePayload?.status === "success" ? (statePayload.data as TradingState) : prev.state,
          metrics: metricsPayload?.status === "success" ? metricsPayload.data || {} : prev.metrics,
          reports: Array.isArray(reportsPayload?.reports) ? reportsPayload.reports : prev.reports,
          commands: Array.isArray(commandsPayload?.commands) ? commandsPayload.commands : prev.commands,
          commandEvents: Array.isArray(commandEventsPayload?.events) ? commandEventsPayload.events : prev.commandEvents,
          governanceEvents: Array.isArray(governancePayload?.events)
            ? governancePayload.events
            : prev.governanceEvents,
        }))

        const err =
          statePayload?.status === "error"
            ? statePayload?.error || "Failed to load trading state"
            : metricsPayload?.status === "error"
              ? metricsPayload?.error || null
              : null
        setError(err)
      } catch (err: any) {
        if (!active) return
        setError(err?.message || "Telemetry polling error")
      } finally {
        if (active) setLoading(false)
      }
    }

    poll()
    const id = setInterval(poll, refreshInterval)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [refreshInterval])

  return { telemetry, error, loading }
}
