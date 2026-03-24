"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"

export type BridgeStatusTier =
  | "bridge_down"
  | "bridge_up_mt4_stale"
  | "bridge_up_runtime_stale"
  | "bridge_up_runtime_starting"
  | "bridge_up_runtime_stalled"
  | "bridge_up_runtime_failed"
  | "bridge_up_runtime_ready_mt4_stale"
  | "bridge_up_mt4_live"

export interface LiveBridgeDecision {
  symbol: string
  side: string
  score: number | null
  price: number | null
  target_pct: number | null
  expected_edge_bps?: number | null
  spread_bps?: number | null
  max_spread_bps?: number | null
  reason?: string
  execution_ready?: boolean
  enqueue_status?: string
  enqueue_action?: string
  position_open?: boolean
  position_side?: string
  position_lots?: number | null
  position_profit?: number | null
  position_open_price?: number | null
}

export interface LiveBridgeState {
  isRunning: boolean
  bridgeState: "bridge_up" | "bridge_down"
  statusTier: BridgeStatusTier
  mt4Connected?: boolean
  mt4Fresh?: boolean
  isStale?: boolean
  signalDataFresh?: boolean
  runtimeSignalFresh?: boolean
  runtimePhase?: string
  runtimePhasePair?: string
  runtimePhaseIndex?: number
  runtimePhaseTotal?: number
  runtimeLastProgressAgeSecs?: number | null
  runtimeFailureReason?: string
  runtimeBootId?: string
  signalDataReason?: string
  tickStatus?: string
  tickReason?: string
  tickSymbolsCount?: number
  tickMaxAgeSecs?: number | null
  lastHeartbeat: string | null
  heartbeatAgeSecs?: number | null
  heartbeatStaleAfterSecs?: number
  runtimeCycleAgeSecs?: number | null
  runtimeCycleStaleAfterSecs?: number
  equity: number
  displayEquity?: number | null
  cachedEquity?: number | null
  positions: any[]
  openPositionsCount?: number
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
  runtimeDiag?: any
  runtimeStatus?: string
  equitySource?: string
  agentDecisions: LiveBridgeDecision[]
  readyEntriesCount?: number
  queuedEntriesCount?: number
  suppressedEntriesCount?: number
  systemStatus: string
}

export interface UseLiveBridgeStateResult {
  state: LiveBridgeState | null
  error: string | null
  loading: boolean
  updatedAt: number | null
}

const DISCONNECTED_FALLBACK: LiveBridgeState = {
  isRunning: false,
  bridgeState: "bridge_down",
  statusTier: "bridge_down",
  mt4Connected: false,
  mt4Fresh: false,
  isStale: true,
  signalDataFresh: false,
  runtimePhase: "",
  runtimePhasePair: "",
  runtimePhaseIndex: 0,
  runtimePhaseTotal: 0,
  runtimeLastProgressAgeSecs: null,
  runtimeFailureReason: "",
  runtimeBootId: "",
  signalDataReason: "state_fetch_error",
  tickStatus: "unknown",
  tickReason: "state_fetch_error",
  tickSymbolsCount: 0,
  tickMaxAgeSecs: null,
  lastHeartbeat: null,
  heartbeatAgeSecs: null,
  heartbeatStaleAfterSecs: 30,
  equity: 0,
  displayEquity: null,
  cachedEquity: null,
  positions: [],
  openPositionsCount: 0,
  cycleActive: false,
  cycleStartEquity: 0,
  cycleTarget: 0,
  signalsSent: 0,
  tradesExecuted: 0,
  lastSignal: null,
  runtimeStatus: "error",
  equitySource: "state_fetch_error",
  agentDecisions: [],
  readyEntriesCount: 0,
  queuedEntriesCount: 0,
  suppressedEntriesCount: 0,
  systemStatus: "error",
}

const useSharedLiveBridgeState = createSharedPollingHook<UseLiveBridgeStateResult>({
  initialSnapshot: {
    state: null,
    error: null,
    loading: true,
    updatedAt: null,
  },
  poll: async () => {
    try {
      const response = await fetch("/api/trading/state", { cache: "no-store" })
      const result = await response.json()

      if (result.status === "success") {
        return {
          state: result.data as LiveBridgeState,
          error: null,
          loading: false,
          updatedAt: Date.now(),
        }
      }

      return {
        state: (result.data as LiveBridgeState) || DISCONNECTED_FALLBACK,
        error: result.error || "Failed to fetch state",
        loading: false,
        updatedAt: Date.now(),
      }
    } catch (err) {
      console.error("[live-bridge-state] fetch error:", err)
      return {
        state: DISCONNECTED_FALLBACK,
        error: "Connection error",
        loading: false,
        updatedAt: Date.now(),
      }
    }
  },
})

export function useLiveBridgeState(refreshInterval = 2000): UseLiveBridgeStateResult {
  return useSharedLiveBridgeState(refreshInterval)
}
