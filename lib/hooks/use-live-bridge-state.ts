// AGENT: ROLE: Shared polling hook exposing a typed, dashboard-facing view of bridge/runtime state.
// AGENT: ENTRYPOINT: imported by dashboard layout, home, status rail, and signals consumers.
// AGENT: PRIMARY INPUTS: `/api/trading/state` route payload plus polling interval.
// AGENT: PRIMARY OUTPUTS: typed `LiveBridgeState`, loading/error flags, derived status fields.
// AGENT: DEPENDS ON: `app/api/trading/state/route.ts`, `lib/hooks/shared-polling-hook`.
// AGENT: CALLED BY: `components/dashboard-home.tsx`, `components/live-signals.tsx`, `components/live-status-rail.tsx`, `components/dashboard-layout.tsx`.
// AGENT: STATE / SIDE EFFECTS: client polling cache only.
// AGENT: HANDSHAKES: dashboard route contract, polling cadence, disconnected fallback state.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `app/api/trading/state/route.ts` -> `components/live-status-rail.tsx`
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
  adaptive_sleeve?: string
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
  conviction_score?: number | null
  conviction_band?: string
  thesis_stage?: string
  portfolio_posture?: string
  sleeve_budget_target?: number | null
  sleeve_budget_used?: number | null
  replacement_urgency?: number | null
  policy_trace?: string[]
  overlay_metadata?: Record<string, any>
  overlay_diagnostics?: Record<string, any>
  allocator_score?: number | null
  allocator_rank?: number | null
  allocator_selected?: boolean
  allocator_rejection_reason?: string
  replacement_candidate?: boolean
  replacement_target_pair?: string
  sleeve_health_score?: number | null
  sleeve_health_state?: string
  thesis_id?: string
  campaign_state?: string
  campaign_state_reason?: string
  campaign_proof_score?: number | null
  campaign_maturity_score?: number | null
  campaign_reset_quality?: number | null
  campaign_priority_boost?: number | null
  campaign_reentry_blocked?: boolean
  belief_primary_side?: string
  belief_primary_scenario?: string
  belief_primary_thesis?: string
  belief_primary_score?: number | null
  belief_primary_rank_score?: number | null
  belief_primary_ev_above_hurdle_prob?: number | null
  belief_primary_expected_net_ev_bps?: number | null
  belief_primary_confirm_prob?: number | null
  belief_primary_fail_fast_prob?: number | null
  belief_no_edge?: boolean
  belief_opposing_side?: string
  belief_opposing_scenario?: string
  belief_opposing_thesis?: string
  belief_opposing_score?: number | null
  belief_gap?: number | null
  belief_fragility_score?: number | null
  belief_horizon_alignment_score?: number | null
  belief_short_up_prob?: number | null
  belief_trade_up_prob?: number | null
  belief_structural_up_prob?: number | null
  belief_regime_fit_score?: number | null
  belief_expected_confirmation_window_bars?: number | null
  belief_expected_path_shape?: string
  belief_invalidation_reason?: string
  belief_model_version?: string
  belief_source_mode?: string
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

export interface RuntimeStartupSummary {
  bootId: string
  bootedAt: string | null
  runtimePid: number | null
  phase: string
  phasePair: string
  phaseIndex: number
  phaseTotal: number
  lastProgressTs: string | number | null
  lastProgressAgeSecs: number | null
  failureReason: string
  failedAt: string | null
  pendingCommandPolicy: string
  modelLoadErrors: number
  modelLoadTimeouts: number
  startupInferenceFailures: number
  startupDisabledPairs: string[]
  warningCount: number
  status: string
  recovered: boolean
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

export interface AllocatorPolicySummary {
  candidateCount: number
  selectedCount: number
  rankedOutCount: number
  replacementCandidateCount: number
  replacementExitCount: number
  sleeveCandidateCounts: Record<string, number>
  sleeveSelectedCounts: Record<string, number>
  sleeveBudgetTargets: Record<string, number>
  sleeveBudgetUsed: Record<string, number>
}

export interface CampaignPolicySummary {
  enabled: boolean
  shadowOnly: boolean
  abandonCooldownBars: number
  pressProtectedBars: number
  reattackCooldownScale: number
}

export interface CampaignCycleSummary {
  stateCounts: Record<string, number>
  transitionCounts: Record<string, number>
  registrySize: number
  activePositionTheses: number
  reentryBlockedCount: number
}

export interface DirectionalBeliefPolicySummary {
  enabled: boolean
  runtimeRequired: boolean
  shortHorizonBars: number
  tradeHorizonBars: number
  structuralHorizonBars: number
}

export interface DirectionalBeliefCycleSummary {
  candidateCountWithBelief: number
  avgBeliefGap: number
  avgFragilityScore: number
  avgPrimaryRankScore: number
  avgPrimaryEvAboveHurdleProb: number
  avgPrimaryExpectedNetEvBps: number
  avgPrimaryFailFastProb: number
  noEdgeShare: number
  primaryScenarioCounts: Record<string, number>
  oppositionScenarioCounts: Record<string, number>
  oppositionSideCounts: Record<string, number>
  artifactVersions: Record<string, string>
}

export interface DirectionalBeliefMetricsSummary {
  decisionCount: number
  beliefLoadedShare: number
  avgBeliefGap: number
  avgFragilityScore: number
  avgPrimaryRankScore: number
  avgPrimaryEvAboveHurdleProb: number
  avgPrimaryExpectedNetEvBps: number
  avgPrimaryFailFastProb: number
  noEdgeShare: number
  primaryScenarioCounts: Record<string, number>
  oppositionScenarioCounts: Record<string, number>
  oppositionSideCounts: Record<string, number>
}

export interface OverlayCycleSummary {
  convictionScoreAvg: number | null
  convictionScoreMax: number | null
  convictionScoreMin: number | null
  convictionBandCounts: Record<string, number>
  thesisStageCounts: Record<string, number>
  postureCounts: Record<string, number>
  sleeveBudgetTargetTotal: number | null
  sleeveBudgetUsedTotal: number | null
  replacementUrgencyAvg: number | null
  policyTraceCount: number
  diagnostics: Record<string, any>
}

export interface ProviderHealthSummary {
  historyProvider: string
  marketDataProvider: string
  executionProvider: string
  primaryProvider: string
  venue: string
  assetClass: string
  sourceChain: string[]
  freshnessSecs: number | null
  stale: boolean
  fallbackActive: boolean
  fallbackReason: string
  missingRate: number | null
  duplicateRate: number | null
  qualityFlags: string[]
  bySymbol: Record<string, any>
  details: Record<string, any>
}

export interface PortfolioTelemetrySummary {
  grossExposure: number | null
  netExposure: number | null
  exposureUnit: string
  openPositionCount: number
  pendingEntryCount: number
  replacementPressure: number | null
  portfolioPosture: string
  concentration: Record<string, any>
  correlation: Record<string, any>
  budgetTargets: Record<string, any>
  budgetUsed: Record<string, any>
  bySymbol: Record<string, any>
  details: Record<string, any>
}

export interface CapitalGovernanceSummary {
  capitalBand: string
  releaseMode: string
  paused: boolean
  entriesOnly: boolean
  shadowOnly?: boolean
  riskScale: number
  rollbackArmed: boolean
  rollbackReason: string
  canaryActive: boolean
  observationWindowActive: boolean
  breachCounts: Record<string, number>
  activeTriggers: string[]
  eligibleForUpgrade?: boolean
  reasons?: string[]
  details: Record<string, any>
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
  runtimeStartup?: RuntimeStartupSummary
  runtimeStartupStatus?: string
  runtimeStartupWarningCount?: number
  modelLoadErrors?: number
  modelLoadTimeouts?: number
  startupInferenceFailures?: number
  startupDisabledPairs?: string[]
  lastRuntimeStartupFailure?: RuntimeStartupFailure | null
  runtimeStartupFailureHistory?: RuntimeStartupFailure[]
  startupInferenceByPair?: Record<string, any>
  featureServingByPair?: Record<string, any>
  pairReadiness?: Record<string, any>
  strategyEngineMode?: string
  supervisedFallback?: Record<string, any>
  challengerConflict?: Record<string, any>
  rlPortfolioProposal?: Record<string, any>
  rlExecutionPolicy?: Record<string, any>
  rlLifecycleSummary?: Record<string, any>
  rlRebalanceSummary?: Record<string, any>
  rlFlipIntent?: Record<string, any>
  rlArtifactReadiness?: Record<string, any>
  rlCheckpointLoaded?: boolean
  rlCheckpointPath?: string
  rlProposalSource?: string
  rlSupervisedFallbackUsed?: boolean
  rlFallbackReason?: string
  rlRoutedEntryCount?: number
  rlBlockedEntryCount?: number
  rlFallbackEntryCount?: number
  rlScaledEntryCount?: number
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
  allocatorPolicy?: AllocatorPolicySummary
  allocatorCycleSummary?: AllocatorPolicySummary
  campaignPolicy?: CampaignPolicySummary
  campaignCycleSummary?: CampaignCycleSummary
  campaignMetricsBySleeve?: Record<string, any>
  campaignStateCounts?: Record<string, number>
  directionalBeliefPolicy?: DirectionalBeliefPolicySummary
  directionalBeliefCycleSummary?: DirectionalBeliefCycleSummary
  directionalBeliefMetrics?: DirectionalBeliefMetricsSummary
  overlayCycleSummary?: OverlayCycleSummary
  providerHealth?: ProviderHealthSummary
  portfolioTelemetry?: PortfolioTelemetrySummary
  capitalGovernance?: CapitalGovernanceSummary
  sleeveMetrics?: Record<string, any>
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

// AGENT STATE: This fallback is the client-side shape guarantee when the dashboard route is unavailable or malformed.
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
  runtimeStartup: {
    bootId: "",
    bootedAt: null,
    runtimePid: null,
    phase: "",
    phasePair: "",
    phaseIndex: 0,
    phaseTotal: 0,
    lastProgressTs: null,
    lastProgressAgeSecs: null,
    failureReason: "",
    failedAt: null,
    pendingCommandPolicy: "",
    modelLoadErrors: 0,
    modelLoadTimeouts: 0,
    startupInferenceFailures: 0,
    startupDisabledPairs: [],
    warningCount: 0,
    status: "failed",
    recovered: false,
  },
  runtimeStartupStatus: "failed",
  runtimeStartupWarningCount: 0,
  modelLoadErrors: 0,
  modelLoadTimeouts: 0,
  startupInferenceFailures: 0,
  startupDisabledPairs: [],
  lastRuntimeStartupFailure: null,
  runtimeStartupFailureHistory: [],
  startupInferenceByPair: {},
  featureServingByPair: {},
  pairReadiness: {},
  strategyEngineMode: "supervised_legacy",
  supervisedFallback: {},
  challengerConflict: {},
  rlPortfolioProposal: {},
  rlExecutionPolicy: {},
  rlLifecycleSummary: {},
  rlRebalanceSummary: {},
  rlFlipIntent: {},
  rlArtifactReadiness: {},
  rlCheckpointLoaded: false,
  rlCheckpointPath: "",
  rlProposalSource: "",
  rlSupervisedFallbackUsed: false,
  rlFallbackReason: "",
  rlRoutedEntryCount: 0,
  rlBlockedEntryCount: 0,
  rlFallbackEntryCount: 0,
  rlScaledEntryCount: 0,
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
  campaignPolicy: {
    enabled: false,
    shadowOnly: true,
    abandonCooldownBars: 0,
    pressProtectedBars: 0,
    reattackCooldownScale: 0,
  },
  campaignCycleSummary: {
    stateCounts: {},
    transitionCounts: {},
    registrySize: 0,
    activePositionTheses: 0,
    reentryBlockedCount: 0,
  },
  campaignMetricsBySleeve: {},
  campaignStateCounts: {},
  directionalBeliefPolicy: {
    enabled: false,
    runtimeRequired: false,
    shortHorizonBars: 0,
    tradeHorizonBars: 0,
    structuralHorizonBars: 0,
  },
  directionalBeliefCycleSummary: {
    candidateCountWithBelief: 0,
    avgBeliefGap: 0,
    avgFragilityScore: 0,
    avgPrimaryRankScore: 0,
    avgPrimaryEvAboveHurdleProb: 0,
    avgPrimaryExpectedNetEvBps: 0,
    avgPrimaryFailFastProb: 0,
    noEdgeShare: 0,
    primaryScenarioCounts: {},
    oppositionScenarioCounts: {},
    oppositionSideCounts: {},
    artifactVersions: {},
  },
  directionalBeliefMetrics: {
    decisionCount: 0,
    beliefLoadedShare: 0,
    avgBeliefGap: 0,
    avgFragilityScore: 0,
    avgPrimaryRankScore: 0,
    avgPrimaryEvAboveHurdleProb: 0,
    avgPrimaryExpectedNetEvBps: 0,
    avgPrimaryFailFastProb: 0,
    noEdgeShare: 0,
    primaryScenarioCounts: {},
    oppositionScenarioCounts: {},
    oppositionSideCounts: {},
  },
  overlayCycleSummary: {
    convictionScoreAvg: null,
    convictionScoreMax: null,
    convictionScoreMin: null,
    convictionBandCounts: {},
    thesisStageCounts: {},
    postureCounts: {},
    sleeveBudgetTargetTotal: null,
    sleeveBudgetUsedTotal: null,
    replacementUrgencyAvg: null,
    policyTraceCount: 0,
    diagnostics: {},
  },
  providerHealth: {
    historyProvider: "",
    marketDataProvider: "",
    executionProvider: "",
    primaryProvider: "",
    venue: "",
    assetClass: "",
    sourceChain: [],
    freshnessSecs: null,
    stale: false,
    fallbackActive: false,
    fallbackReason: "",
    missingRate: null,
    duplicateRate: null,
    qualityFlags: [],
    bySymbol: {},
    details: {},
  },
  portfolioTelemetry: {
    grossExposure: null,
    netExposure: null,
    exposureUnit: "unknown",
    openPositionCount: 0,
    pendingEntryCount: 0,
    replacementPressure: null,
    portfolioPosture: "unknown",
    concentration: {},
    correlation: {},
    budgetTargets: {},
    budgetUsed: {},
    bySymbol: {},
    details: {},
  },
  capitalGovernance: {
    capitalBand: "unknown",
    releaseMode: "normal",
    paused: false,
    entriesOnly: false,
    shadowOnly: false,
    riskScale: 1,
    rollbackArmed: false,
    rollbackReason: "",
    canaryActive: false,
    observationWindowActive: false,
    breachCounts: {},
    activeTriggers: [],
    eligibleForUpgrade: false,
    reasons: [],
    details: {},
  },
  readyEntriesCount: 0,
  queuedEntriesCount: 0,
  suppressedEntriesCount: 0,
  systemStatus: "error",
}

// AGENT HOT PATH: Shared polling keeps one network cadence for all consumers instead of fan-out polling per component.
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

// AGENT FLOW: Public hook wrapper keeps the route contract local to this file; components should consume typed state, not fetch the route directly.
export function useLiveBridgeState(refreshInterval = 2000): UseLiveBridgeStateResult {
  return useSharedLiveBridgeState(refreshInterval)
}
