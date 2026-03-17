"use client"

import { useEffect, useState } from "react"

export interface TradingState {
  isRunning: boolean
  lastHeartbeat: string | null
  equity: number
  positions: any[]
  vol?: number
  cycleActive: boolean
  cycleStartEquity: number
  cycleTarget: number
  signalsSent: number
  tradesExecuted: number
  lastSignal: any
  lastAck?: any
  monitor?: any
  governance?: any
  riskEnvelope?: any
  agent_diagnostics?: any
  agentDecisions: Array<{
    symbol: string
    side: string
    score: number
    price: number
    target_pct: number
  }>
  systemStatus: string
}

export function useTradingState(refreshInterval = 2000) {
  const [state, setState] = useState<TradingState | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    const fetchState = async () => {
      try {
        const response = await fetch("/api/trading/state")
        const result = await response.json()

        if (result.status === "success") {
          setState(result.data)
          setError(null)
        } else {
          setError(result.error || "Failed to fetch state")
          setState(result.data)
        }
      } catch (err) {
        console.error("[v0] Trading state fetch error:", err)
        setError("Connection error")
      } finally {
        setLoading(false)
      }
    }

    fetchState()
    const interval = setInterval(fetchState, refreshInterval)

    return () => clearInterval(interval)
  }, [refreshInterval])

  return { state, error, loading }
}
