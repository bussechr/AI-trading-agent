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
  execution_mode?: string
  execution_entry_ready?: boolean
  execution_blocking_reasons?: string[]
  execution_rejection_reason?: string
  position_count_pair?: number | null
  position_lots?: number | null
  position_profit?: number | null
  position_open_price?: number | null
  strict_entry_ready?: boolean
  strict_entry_blocking_reasons?: string[]
  strict_rejection_reason?: string
  entry_ready?: boolean
  entry_blocking_reasons?: string[]
  reversal_context_active?: boolean
  reversal_ready?: boolean
  reversal_blocking_reasons?: string[]
  reversal_failure_prob?: number | null
  reversal_opportunity_prob?: number | null
  reversal_should_exit?: boolean
  exit_action_selected?: string
  exit_action_score?: number | null
  exit_action_probs?: Record<string, number>
  lifecycle_action?: string
  lifecycle_reason?: string
  lifecycle_activation_mode?: string
  lifecycle_inference_error?: string
  uncertainty_score?: number | null
  directional_swing_confidence?: number | null
  entry_margin?: number | null
  meta_margin?: number | null
  model_disagreement_score?: number | null
  htf_alignment_score?: number | null
  pullback_quality_score?: number | null
  resume_trigger_score?: number | null
  extension_penalty_score?: number | null
  structure_timing_score?: number | null
  structure_bonus_bps?: number | null
  chase_penalty_bps?: number | null
  calibrated_ev_bps_shadow?: number | null
  entry_quality_score_shadow?: number | null
  structure_rescue_active?: boolean
  portfolio_rank_shadow?: number | null
  shadow_floor_ok?: boolean
  shadow_floor_rejection_reason?: string
  shadow_would_trade?: boolean
  shadow_rejection_reason?: string
  shadow_live_divergence?: string
  adaptive_environment_state?: string
  adaptive_trend_persistence_score?: number | null
  adaptive_compression_score?: number | null
  adaptive_expansion_score?: number | null
  adaptive_range_score?: number | null
  adaptive_hostility_score?: number | null
  adaptive_macro_coherence_score?: number | null
  adaptive_pair_strength_score?: number | null
  adaptive_playbook?: string
  adaptive_playbook_score?: number | null
  adaptive_location_score?: number | null
  adaptive_trigger_score?: number | null
  adaptive_entry_quality?: number | null
  adaptive_currency_crowding_penalty?: number | null
  adaptive_playbook_diversification_penalty?: number | null
  adaptive_aggressive_fallback_used?: boolean
  adaptive_shadow_allowed?: boolean
  adaptive_portfolio_rank_shadow?: number | null
  adaptive_shadow_would_trade?: boolean
  adaptive_shadow_rejection_reason?: string
  adaptive_shadow_live_divergence?: string
  regime_prob?: number | null
  swing_prob?: number | null
  entry_prob?: number | null
  trade_prob?: number | null
}

export interface RuntimeStartupFailure {
  eventType: string
  reason: string
  bootId: string
  phase: string
  phasePair: string
  failedAt: string | null
  failedAgeSecs: number | null
}

export interface ShadowPolicySummary {
  enabled: boolean
  candidateCount: number
  rankedCount: number
  wouldTradeCount: number
  remainingSlots: number
  maxNewEntries: number
  structureRescueCount: number
  structureRescuesByPair: Record<string, number>
  divergenceCounts: {
    agreeReady: number
    agreeBlocked: number
    liveOnly: number
    shadowOnly: number
    openPosition: number
  }
  dominantRejectionReason: string
  rejectionReasonCounts: Record<string, number>
  rejectionsByPair: Record<string, string>
  tierSummary: Record<
    string,
    {
      total: number
      blocked: number
      candidates: number
      wouldTrade: number
    }
  >
  spreadDiagnostics: {
    rejectCount: number
    dominantPair: string
    dominantSession: string
    byPair: Record<
      string,
      {
        count: number
        avg_spread_bps: number
        avg_max_spread_bps: number
        avg_excess_bps: number
        session: string
      }
    >
    bySession: Record<
      string,
      {
        count: number
        avg_spread_bps: number
        avg_max_spread_bps: number
        avg_excess_bps: number
        pairs: string[]
      }
    >
  }
  secondarySpreadDiagnostics: {
    rejectCount: number
    dominantPair: string
    dominantSession: string
    byPair: Record<
      string,
      {
        count: number
        avg_spread_bps: number
        avg_max_spread_bps: number
        avg_excess_bps: number
        session: string
      }
    >
    bySession: Record<
      string,
      {
        count: number
        avg_spread_bps: number
        avg_max_spread_bps: number
        avg_excess_bps: number
        pairs: string[]
      }
    >
  }
}

export interface AdaptiveShadowPolicySummary {
  enabled: boolean
  candidateCount: number
  rankedCount: number
  wouldTradeCount: number
  remainingSlots: number
  maxNewEntries: number
  aggressiveFallbackCount: number
  divergenceCounts: {
    agreeReady: number
    agreeBlocked: number
    liveOnly: number
    adaptiveOnly: number
    openPosition: number
  }
  dominantRejectionReason: string
  rejectionReasonCounts: Record<string, number>
  rejectionsByPair: Record<string, string>
  playbookCounts: Record<string, number>
  environmentCounts: Record<string, number>
}

export interface EntryExecutionPolicySummary {
  executionMode: string
  adaptiveExecutionEnabled: boolean
  pendingEntryCount: number
  approvedEntryCount: number
  blockedEntryCount: number
  submittedEntryCount: number
  duplicateEntryCount: number
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
  lastRuntimeStartupFailure?: RuntimeStartupFailure | null
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
  shadowPolicy?: ShadowPolicySummary
  adaptiveShadowPolicy?: AdaptiveShadowPolicySummary
  entryExecutionPolicy?: EntryExecutionPolicySummary
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
  lastRuntimeStartupFailure: null,
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
  entryExecutionPolicy: {
    executionMode: "",
    adaptiveExecutionEnabled: false,
    pendingEntryCount: 0,
    approvedEntryCount: 0,
    blockedEntryCount: 0,
    submittedEntryCount: 0,
    duplicateEntryCount: 0,
  },
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
