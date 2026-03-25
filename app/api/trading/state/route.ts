import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"

function toMs(value: any): number {
  if (value === null || value === undefined) return 0
  if (typeof value === "number") {
    return value > 10_000_000_000 ? value : value * 1000
  }
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : 0
}

function asFiniteNumber(value: any): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function pickFirstFinite(values: any[], fallback = 0): number {
  for (const value of values) {
    if (value === null || value === undefined) continue
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return fallback
}

function normalizeSide(raw: any): string {
  const txt = String(raw ?? "").trim().toUpperCase()
  if (txt === "BUY" || txt === "SELL") return txt
  if (txt === "LONG") return "BUY"
  if (txt === "SHORT") return "SELL"
  return "N/A"
}

function normalizeReasonList(raw: any): string[] {
  if (!Array.isArray(raw)) return []
  return raw
    .map((value) => String(value || "").trim())
    .filter((value, index, values) => value.length > 0 && values.indexOf(value) === index)
}

function normalizePosition(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const type = Number(row.type)
  return {
    symbol: String(row.symbol || row.pair || "N/A").toUpperCase(),
    side: type === 0 ? "BUY" : type === 1 ? "SELL" : "N/A",
    open_price: asFiniteNumber(row.open_price ?? row.openPrice),
    lots: asFiniteNumber(row.lots),
    profit: asFiniteNumber(row.profit),
    open_time: row.open_time ?? row.openTime ?? null,
  }
}

function tickMidPrice(raw: any): number | null {
  const row = raw && typeof raw === "object" ? raw : {}
  const bid = asFiniteNumber(row.bid)
  const ask = asFiniteNumber(row.ask)
  if (bid !== null && ask !== null) return (bid + ask) / 2
  return asFiniteNumber(row.mid ?? row.price ?? row.last ?? row.ask ?? row.bid)
}

function normalizeRuntimeStartupFailure(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const payload = row.payload_json && typeof row.payload_json === "object" ? row.payload_json : row.payload && typeof row.payload === "object" ? row.payload : {}
  const eventType = String(row.event_type || row.eventType || "")
  if (eventType !== "runtime_startup_failed") return null
  const failedAtRaw = payload.failed_at ?? row.failed_at ?? row.time ?? row.ts ?? null
  const failedAtMs = toMs(failedAtRaw)
  return {
    eventType,
    reason: String(row.reason || payload.failure_reason || ""),
    bootId: String(payload.boot_id || ""),
    phase: String(payload.phase || ""),
    phasePair: String(payload.phase_pair || "").toUpperCase(),
    failedAt: failedAtMs > 0 ? new Date(failedAtMs).toISOString() : null,
    failedAgeSecs: failedAtMs > 0 ? Math.max(0, (Date.now() - failedAtMs) / 1000) : null,
  }
}

function normalizeShadowPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const divergenceRaw =
    row.shadow_live_divergence_counts && typeof row.shadow_live_divergence_counts === "object"
      ? row.shadow_live_divergence_counts
      : {}
  const tierSummaryRaw = row.shadow_tier_summary && typeof row.shadow_tier_summary === "object" ? row.shadow_tier_summary : {}
  const spreadRaw =
    row.shadow_spread_diagnostics && typeof row.shadow_spread_diagnostics === "object" ? row.shadow_spread_diagnostics : {}
  const secondarySpreadRaw =
    row.shadow_secondary_spread_diagnostics && typeof row.shadow_secondary_spread_diagnostics === "object"
      ? row.shadow_secondary_spread_diagnostics
      : {}
  const tierSummary = Object.fromEntries(
    Object.entries(tierSummaryRaw).map(([tier, value]) => {
      const stats = value && typeof value === "object" ? value : {}
      return [
        String(tier),
        {
          total: Number((stats as any).total || 0),
          blocked: Number((stats as any).blocked || 0),
          candidates: Number((stats as any).candidates || 0),
          wouldTrade: Number((stats as any).would_trade || (stats as any).wouldTrade || 0),
        },
      ]
    }),
  )
  return {
    enabled: Boolean(row.shadow_policy_enabled ?? false),
    candidateCount: Number(row.shadow_candidate_count || 0),
    rankedCount: Number(row.shadow_ranked_count || 0),
    wouldTradeCount: Number(row.shadow_would_trade_count || 0),
    remainingSlots: Number(row.shadow_remaining_slots || 0),
    maxNewEntries: Number(row.shadow_max_new_entries || 0),
    structureRescueCount: Number(row.shadow_structure_rescue_count || 0),
    structureRescuesByPair:
      row.shadow_structure_rescues_by_pair && typeof row.shadow_structure_rescues_by_pair === "object"
        ? row.shadow_structure_rescues_by_pair
        : {},
    divergenceCounts: {
      agreeReady: Number(divergenceRaw.agree_ready || 0),
      agreeBlocked: Number(divergenceRaw.agree_blocked || 0),
      liveOnly: Number(divergenceRaw.live_only || 0),
      shadowOnly: Number(divergenceRaw.shadow_only || 0),
      openPosition: Number(divergenceRaw.open_position || 0),
    },
    dominantRejectionReason: String(row.shadow_dominant_rejection_reason || ""),
    rejectionReasonCounts:
      row.shadow_rejection_reason_counts && typeof row.shadow_rejection_reason_counts === "object"
        ? row.shadow_rejection_reason_counts
        : {},
    rejectionsByPair:
      row.shadow_rejections_by_pair && typeof row.shadow_rejections_by_pair === "object"
        ? row.shadow_rejections_by_pair
        : {},
    tierSummary,
    spreadDiagnostics: {
      rejectCount: Number(spreadRaw.reject_count || 0),
      dominantPair: String(spreadRaw.dominant_pair || ""),
      dominantSession: String(spreadRaw.dominant_session || ""),
      byPair: spreadRaw.by_pair && typeof spreadRaw.by_pair === "object" ? spreadRaw.by_pair : {},
      bySession: spreadRaw.by_session && typeof spreadRaw.by_session === "object" ? spreadRaw.by_session : {},
    },
    secondarySpreadDiagnostics: {
      rejectCount: Number(secondarySpreadRaw.reject_count || 0),
      dominantPair: String(secondarySpreadRaw.dominant_pair || ""),
      dominantSession: String(secondarySpreadRaw.dominant_session || ""),
      byPair: secondarySpreadRaw.by_pair && typeof secondarySpreadRaw.by_pair === "object" ? secondarySpreadRaw.by_pair : {},
      bySession:
        secondarySpreadRaw.by_session && typeof secondarySpreadRaw.by_session === "object" ? secondarySpreadRaw.by_session : {},
    },
  }
}

function normalizeAdaptiveShadowPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const divergenceRaw =
    row.adaptive_shadow_live_divergence_counts && typeof row.adaptive_shadow_live_divergence_counts === "object"
      ? row.adaptive_shadow_live_divergence_counts
      : {}
  return {
    enabled: Boolean(row.adaptive_shadow_enabled ?? false),
    candidateCount: Number(row.adaptive_shadow_candidate_count || 0),
    rankedCount: Number(row.adaptive_shadow_ranked_count || 0),
    wouldTradeCount: Number(row.adaptive_shadow_would_trade_count || 0),
    remainingSlots: Number(row.adaptive_shadow_remaining_slots || 0),
    maxNewEntries: Number(row.adaptive_shadow_max_new_entries || 0),
    aggressiveFallbackCount: Number(row.adaptive_shadow_aggressive_fallback_count || 0),
    divergenceCounts: {
      agreeReady: Number(divergenceRaw.agree_ready || 0),
      agreeBlocked: Number(divergenceRaw.agree_blocked || 0),
      liveOnly: Number(divergenceRaw.live_only || 0),
      adaptiveOnly: Number(divergenceRaw.adaptive_only || 0),
      openPosition: Number(divergenceRaw.open_position || 0),
    },
    dominantRejectionReason: String(row.adaptive_shadow_dominant_rejection_reason || ""),
    rejectionReasonCounts:
      row.adaptive_shadow_rejection_reason_counts && typeof row.adaptive_shadow_rejection_reason_counts === "object"
        ? row.adaptive_shadow_rejection_reason_counts
        : {},
    rejectionsByPair:
      row.adaptive_shadow_rejections_by_pair && typeof row.adaptive_shadow_rejections_by_pair === "object"
        ? row.adaptive_shadow_rejections_by_pair
        : {},
    playbookCounts:
      row.adaptive_shadow_playbook_counts && typeof row.adaptive_shadow_playbook_counts === "object"
        ? row.adaptive_shadow_playbook_counts
        : {},
    environmentCounts:
      row.adaptive_shadow_environment_counts && typeof row.adaptive_shadow_environment_counts === "object"
        ? row.adaptive_shadow_environment_counts
        : {},
  }
}

function normalizeEntryExecutionPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    executionMode: String(row.execution_mode || row.executionMode || ""),
    adaptiveExecutionEnabled: Boolean(row.adaptive_execution_enabled ?? row.adaptiveExecutionEnabled ?? false),
    pendingEntryCount: Number(row.pending_entry_count || row.pendingEntryCount || 0),
    approvedEntryCount: Number(row.approved_entry_count || row.approvedEntryCount || 0),
    blockedEntryCount: Number(row.blocked_entry_count || row.blockedEntryCount || 0),
    submittedEntryCount: Number(row.submitted_entry_count || row.submittedEntryCount || 0),
    duplicateEntryCount: Number(row.duplicate_entry_count || row.duplicateEntryCount || 0),
  }
}

function normalizeDecision(
  raw: any,
  options: {
    ticksBySymbol: Map<string, any>
    positionsBySymbol: Map<string, any>
  },
) {
  const row = raw && typeof raw === "object" ? raw : {}
  const metadata = row.metadata && typeof row.metadata === "object" ? row.metadata : {}
  const thresholdSnapshot =
    metadata.threshold_snapshot && typeof metadata.threshold_snapshot === "object" ? metadata.threshold_snapshot : {}
  const reasons = Array.isArray(row.reasons) ? row.reasons : []
  const symbol = String(row.symbol || metadata.pair || "N/A").toUpperCase()
  const position = options.positionsBySymbol.get(symbol) || null
  const tick = options.ticksBySymbol.get(symbol) || null
  const score = asFiniteNumber(row.score)
  const expectedEdgeBps = asFiniteNumber(row.expected_edge_bps ?? metadata.expected_edge_bps)
  const price = asFiniteNumber(
    row.price ??
      metadata.price ??
      metadata.mid ??
      metadata.bid ??
      metadata.ask ??
      tickMidPrice(tick) ??
      position?.open_price,
  )
  const targetPct = asFiniteNumber(
    row.target_pct ?? metadata.target_pct ?? (expectedEdgeBps !== null ? expectedEdgeBps / 10_000 : null),
  )
  const spreadBps = asFiniteNumber(row.spread_bps ?? metadata.spread_bps)
  const maxSpreadBps = asFiniteNumber(thresholdSnapshot.max_spread_bps ?? thresholdSnapshot.max_allowed_spread_bps)
  const executionReady = Boolean(
    row.execution_ready ?? row.executionReady ?? metadata.execution_ready ?? metadata.allowed ?? false,
  )
  const reason = String(row.reason || reasons[0] || metadata.rejection_reason || "none")
  const enqueue =
    metadata.enqueue && typeof metadata.enqueue === "object"
      ? metadata.enqueue
      : row.enqueue && typeof row.enqueue === "object"
        ? row.enqueue
        : {}
  const positionSide = normalizeSide(
    position?.side ??
      metadata.position_side ??
      metadata.positionSide ??
      row.position_side ??
      row.positionSide,
  )
  const entryBlockingReasons = normalizeReasonList(
    metadata.entry_blocking_reasons ?? metadata.entryBlockingReasons ?? reasons,
  )
  const reversalBlockingReasons = normalizeReasonList(
    metadata.reversal_blocking_reasons ?? metadata.reversalBlockingReasons,
  )
  return {
    symbol,
    side: normalizeSide(row.side),
    score,
    price,
    target_pct: targetPct,
    expected_edge_bps: expectedEdgeBps,
    spread_bps: spreadBps,
    max_spread_bps: maxSpreadBps,
    reason,
    execution_ready: executionReady,
    enqueue_status: String(enqueue.status || ""),
    enqueue_action: String(enqueue.action || metadata.lifecycle_action || ""),
    position_open: Boolean(position),
    position_side: positionSide,
    position_lots: position?.lots ?? null,
    position_profit: position?.profit ?? null,
    position_open_price: position?.open_price ?? null,
    execution_mode: String(metadata.execution_mode || metadata.executionMode || ""),
    execution_entry_ready: Boolean(
      metadata.execution_entry_ready ?? metadata.executionEntryReady ?? executionReady,
    ),
    execution_blocking_reasons: normalizeReasonList(
      metadata.execution_blocking_reasons ?? metadata.executionBlockingReasons,
    ),
    execution_rejection_reason: String(
      metadata.execution_rejection_reason || metadata.executionRejectionReason || "",
    ),
    position_count_pair: asFiniteNumber(metadata.position_count_pair ?? metadata.positionCountPair),
    strict_entry_ready: Boolean(metadata.strict_entry_ready ?? metadata.strictEntryReady ?? executionReady),
    strict_entry_blocking_reasons: normalizeReasonList(
      metadata.strict_entry_blocking_reasons ?? metadata.strictEntryBlockingReasons,
    ),
    strict_rejection_reason: String(metadata.strict_rejection_reason || metadata.strictRejectionReason || ""),
    entry_ready: Boolean(metadata.entry_ready ?? metadata.entryReady ?? executionReady),
    entry_blocking_reasons: entryBlockingReasons,
    reversal_context_active: Boolean(
      metadata.reversal_context_active ?? metadata.reversalContextActive ?? false,
    ),
    reversal_ready: Boolean(metadata.reversal_ready ?? metadata.reversalReady ?? false),
    reversal_blocking_reasons: reversalBlockingReasons,
    reversal_failure_prob: asFiniteNumber(metadata.reversal_failure_prob ?? metadata.reversalFailureProb),
    reversal_opportunity_prob: asFiniteNumber(
      metadata.reversal_opportunity_prob ?? metadata.reversalOpportunityProb,
    ),
    reversal_should_exit: Boolean(metadata.reversal_should_exit ?? metadata.reversalShouldExit ?? false),
    exit_action_selected: String(metadata.exit_action_selected || metadata.exitActionSelected || ""),
    exit_action_score: asFiniteNumber(metadata.exit_action_score ?? metadata.exitActionScore),
    exit_action_probs:
      metadata.exit_action_probs && typeof metadata.exit_action_probs === "object"
        ? metadata.exit_action_probs
        : metadata.exitActionProbs && typeof metadata.exitActionProbs === "object"
          ? metadata.exitActionProbs
          : {},
    lifecycle_action: String(metadata.lifecycle_action || metadata.lifecycleAction || ""),
    lifecycle_reason: String(metadata.lifecycle_reason || metadata.lifecycleReason || ""),
    lifecycle_activation_mode: String(
      metadata.lifecycle_activation_mode || metadata.lifecycleActivationMode || "",
    ),
    lifecycle_inference_error: String(
      metadata.lifecycle_inference_error || metadata.lifecycleInferenceError || "",
    ),
    uncertainty_score: asFiniteNumber(metadata.uncertainty_score ?? metadata.uncertaintyScore),
    directional_swing_confidence: asFiniteNumber(
      metadata.directional_swing_confidence ?? metadata.directionalSwingConfidence,
    ),
    entry_margin: asFiniteNumber(metadata.entry_margin ?? metadata.entryMargin),
    meta_margin: asFiniteNumber(metadata.meta_margin ?? metadata.metaMargin),
    model_disagreement_score: asFiniteNumber(
      metadata.model_disagreement_score ?? metadata.modelDisagreementScore,
    ),
    htf_alignment_score: asFiniteNumber(metadata.htf_alignment_score ?? metadata.htfAlignmentScore),
    pullback_quality_score: asFiniteNumber(metadata.pullback_quality_score ?? metadata.pullbackQualityScore),
    resume_trigger_score: asFiniteNumber(metadata.resume_trigger_score ?? metadata.resumeTriggerScore),
    extension_penalty_score: asFiniteNumber(metadata.extension_penalty_score ?? metadata.extensionPenaltyScore),
    structure_timing_score: asFiniteNumber(metadata.structure_timing_score ?? metadata.structureTimingScore),
    structure_bonus_bps: asFiniteNumber(metadata.structure_bonus_bps ?? metadata.structureBonusBps),
    chase_penalty_bps: asFiniteNumber(metadata.chase_penalty_bps ?? metadata.chasePenaltyBps),
    calibrated_ev_bps_shadow: asFiniteNumber(
      metadata.calibrated_ev_bps_shadow ?? metadata.calibratedEvBpsShadow,
    ),
    entry_quality_score_shadow: asFiniteNumber(
      metadata.entry_quality_score_shadow ?? metadata.entryQualityScoreShadow,
    ),
    structure_rescue_active: Boolean(metadata.structure_rescue_active ?? metadata.structureRescueActive ?? false),
    portfolio_rank_shadow: asFiniteNumber(metadata.portfolio_rank_shadow ?? metadata.portfolioRankShadow),
    shadow_floor_ok: Boolean(metadata.shadow_floor_ok ?? metadata.shadowFloorOk ?? false),
    shadow_floor_rejection_reason: String(
      metadata.shadow_floor_rejection_reason || metadata.shadowFloorRejectionReason || "",
    ),
    shadow_would_trade: Boolean(metadata.shadow_would_trade ?? metadata.shadowWouldTrade ?? false),
    shadow_rejection_reason: String(metadata.shadow_rejection_reason || metadata.shadowRejectionReason || ""),
    shadow_live_divergence: String(metadata.shadow_live_divergence || metadata.shadowLiveDivergence || ""),
    adaptive_environment_state: String(metadata.adaptive_environment_state || metadata.adaptiveEnvironmentState || ""),
    adaptive_trend_persistence_score: asFiniteNumber(
      metadata.adaptive_trend_persistence_score ?? metadata.adaptiveTrendPersistenceScore,
    ),
    adaptive_compression_score: asFiniteNumber(metadata.adaptive_compression_score ?? metadata.adaptiveCompressionScore),
    adaptive_expansion_score: asFiniteNumber(metadata.adaptive_expansion_score ?? metadata.adaptiveExpansionScore),
    adaptive_range_score: asFiniteNumber(metadata.adaptive_range_score ?? metadata.adaptiveRangeScore),
    adaptive_hostility_score: asFiniteNumber(metadata.adaptive_hostility_score ?? metadata.adaptiveHostilityScore),
    adaptive_macro_coherence_score: asFiniteNumber(
      metadata.adaptive_macro_coherence_score ?? metadata.adaptiveMacroCoherenceScore,
    ),
    adaptive_pair_strength_score: asFiniteNumber(
      metadata.adaptive_pair_strength_score ?? metadata.adaptivePairStrengthScore,
    ),
    adaptive_playbook: String(metadata.adaptive_playbook || metadata.adaptivePlaybook || ""),
    adaptive_playbook_score: asFiniteNumber(metadata.adaptive_playbook_score ?? metadata.adaptivePlaybookScore),
    adaptive_location_score: asFiniteNumber(metadata.adaptive_location_score ?? metadata.adaptiveLocationScore),
    adaptive_trigger_score: asFiniteNumber(metadata.adaptive_trigger_score ?? metadata.adaptiveTriggerScore),
    adaptive_entry_quality: asFiniteNumber(metadata.adaptive_entry_quality ?? metadata.adaptiveEntryQuality),
    adaptive_currency_crowding_penalty: asFiniteNumber(
      metadata.adaptive_currency_crowding_penalty ?? metadata.adaptiveCurrencyCrowdingPenalty,
    ),
    adaptive_playbook_diversification_penalty: asFiniteNumber(
      metadata.adaptive_playbook_diversification_penalty ?? metadata.adaptivePlaybookDiversificationPenalty,
    ),
    adaptive_aggressive_fallback_used: Boolean(
      metadata.adaptive_aggressive_fallback_used ?? metadata.adaptiveAggressiveFallbackUsed ?? false,
    ),
    adaptive_shadow_allowed: Boolean(metadata.adaptive_shadow_allowed ?? metadata.adaptiveShadowAllowed ?? false),
    adaptive_portfolio_rank_shadow: asFiniteNumber(
      metadata.adaptive_portfolio_rank_shadow ?? metadata.adaptivePortfolioRankShadow,
    ),
    adaptive_shadow_would_trade: Boolean(
      metadata.adaptive_shadow_would_trade ?? metadata.adaptiveShadowWouldTrade ?? false,
    ),
    adaptive_shadow_rejection_reason: String(
      metadata.adaptive_shadow_rejection_reason || metadata.adaptiveShadowRejectionReason || "",
    ),
    adaptive_shadow_live_divergence: String(
      metadata.adaptive_shadow_live_divergence || metadata.adaptiveShadowLiveDivergence || "",
    ),
    regime_prob: asFiniteNumber(metadata.regime_prob ?? metadata.regimeProb),
    swing_prob: asFiniteNumber(metadata.swing_prob ?? metadata.swingProb),
    entry_prob: asFiniteNumber(metadata.entry_prob ?? metadata.entryProb),
    trade_prob: asFiniteNumber(metadata.trade_prob ?? metadata.tradeProb),
  }
}

export async function GET() {
  try {
    const raw = await fetchBridgeJson(["/v2/state"])
    const ticksRaw = await fetchBridgeJson(["/v2/market/ticks"]).catch(() => null)
    const monitorEmbedded = raw?.monitor && typeof raw.monitor === "object"
    const monitor = monitorEmbedded ? null : await fetchBridgeJson(["/v2/monitor"]).catch(() => null)
    const governanceRaw = await fetchBridgeJson(["/v2/governance/events?limit=50"]).catch(() => null)

    const heartbeatStaleAfterSecs = Math.max(1, asFiniteNumber(raw?.heartbeat_stale_after_secs) || 30)
    const lastHeartbeat = raw?.last_heartbeat || raw?.lastHeartbeat || null
    const heartbeatAgeFromState = asFiniteNumber(raw?.heartbeat_age_secs ?? raw?.heartbeatAgeSecs)
    const heartbeatAgeFromTs =
      lastHeartbeat && toMs(lastHeartbeat) > 0 ? Math.max(0, (Date.now() - toMs(lastHeartbeat)) / 1000) : null
    const heartbeatAgeSecs = heartbeatAgeFromState ?? heartbeatAgeFromTs

    const statusRaw = String(raw?.system_status || raw?.systemStatus || "unknown").trim().toLowerCase()
    const mt4Connected = statusRaw === "connected"
    const mt4FreshByHeartbeat = heartbeatAgeSecs !== null && heartbeatAgeSecs <= heartbeatStaleAfterSecs
    const mt4Fresh = mt4Connected && mt4FreshByHeartbeat
    const ticksFresh = typeof raw?.ticks_fresh === "boolean" ? Boolean(raw?.ticks_fresh) : mt4Fresh
    const runtimeStatus = String(raw?.runtime_status || raw?.runtimeStatus || "unknown").trim().toLowerCase()
    const runtimePhase = String(raw?.runtime_phase || raw?.runtimePhase || raw?.runtime_startup?.phase || "").trim().toLowerCase()
    const runtimePhasePair = String(
      raw?.runtime_phase_pair || raw?.runtimePhasePair || raw?.runtime_startup?.phase_pair || "",
    )
      .trim()
      .toUpperCase()
    const runtimePhaseIndex = Number(raw?.runtime_phase_index || raw?.runtimePhaseIndex || raw?.runtime_startup?.phase_index || 0)
    const runtimePhaseTotal = Number(raw?.runtime_phase_total || raw?.runtimePhaseTotal || raw?.runtime_startup?.phase_total || 0)
    const runtimeLastProgressAgeSecs = asFiniteNumber(
      raw?.runtime_last_progress_age_secs ??
        raw?.runtimeLastProgressAgeSecs ??
        raw?.runtime_startup?.last_progress_age_secs,
    )
    const runtimeFailureReason = String(
      raw?.runtime_failure_reason || raw?.runtimeFailureReason || raw?.runtime_startup?.failure_reason || "",
    ).trim()
    const runtimeBootId = String(raw?.runtime_boot_id || raw?.runtimeBootId || raw?.runtime_startup?.boot_id || "").trim()
    const runtimeCycleAgeSecs = asFiniteNumber(raw?.runtime_cycle_age_secs ?? raw?.runtimeCycleAgeSecs)
    const runtimeCycleStaleAfterSecs = Math.max(1, asFiniteNumber(raw?.runtime_cycle_stale_after_secs) || 30)
    const runtimeSignalFresh =
      typeof raw?.runtime_signal_fresh === "boolean"
        ? Boolean(raw.runtime_signal_fresh)
        : runtimeStatus === "running" &&
          runtimeCycleAgeSecs !== null &&
          runtimeCycleAgeSecs <= runtimeCycleStaleAfterSecs
    const signalDataFresh = mt4Fresh && ticksFresh && runtimeSignalFresh
    const isStale = !mt4Fresh || !ticksFresh || !runtimeSignalFresh
    const bridgeState = "bridge_up"
    const statusTier = String(raw?.status_tier || raw?.statusTier || "").trim() || (
      mt4Fresh && ticksFresh ? (runtimeSignalFresh ? "bridge_up_mt4_live" : "bridge_up_runtime_stale") : "bridge_up_mt4_stale"
    )

    let systemStatus = statusRaw || "unknown"
    if (mt4Connected && !mt4FreshByHeartbeat) {
      systemStatus = "stale"
    }
    if (mt4Connected && mt4FreshByHeartbeat && !ticksFresh) {
      systemStatus = "stale"
    }
    if (!mt4Connected && systemStatus === "connected") {
      systemStatus = "disconnected"
    }

    const positions: ReturnType<typeof normalizePosition>[] = Array.isArray(raw?.positions)
      ? raw.positions.map((position: any) => normalizePosition(position))
      : []
    const positionsBySymbol = new Map<string, ReturnType<typeof normalizePosition>>(
      positions.map((position: ReturnType<typeof normalizePosition>) => [String(position.symbol || "").toUpperCase(), position]),
    )
    const ticksEntries: Array<[string, any]> =
      ticksRaw && typeof ticksRaw === "object"
        ? Object.entries(ticksRaw).map(([symbol, value]) => [String(symbol).toUpperCase(), value] as [string, any])
        : []
    const ticksBySymbol = new Map<string, any>(ticksEntries)

    const liveEquity = pickFirstFinite(
      [
        raw?.mt4_equity,
        raw?.mt4Equity,
        raw?.account_equity,
        raw?.accountEquity,
        raw?.monitor?.account?.equity,
        monitor?.account?.equity,
        raw?.equity,
      ],
      0,
    )
    const equity = mt4Fresh ? liveEquity : 0
    const margin = pickFirstFinite([raw?.margin, raw?.monitor?.account?.margin, monitor?.account?.margin], 0)
    const freeMargin = pickFirstFinite(
      [raw?.freemargin, raw?.free_margin, raw?.monitor?.account?.freemargin, monitor?.account?.freemargin],
      0,
    )
    const decisionsRaw = Array.isArray(raw?.agent_decisions)
      ? raw.agent_decisions
      : Array.isArray(raw?.agentDecisions)
        ? raw.agentDecisions
        : []
    const agentDecisions = signalDataFresh
      ? decisionsRaw.map((decision: any) => normalizeDecision(decision, { ticksBySymbol, positionsBySymbol }))
      : []
    const openPositionsCount = positions.length
    const readyEntriesCount = agentDecisions.filter(
      (decision: ReturnType<typeof normalizeDecision>) => !decision.position_open && Boolean(decision.execution_ready),
    ).length
    const queuedEntriesCount = agentDecisions.filter(
      (decision: ReturnType<typeof normalizeDecision>) => decision.enqueue_status === "queued",
    ).length
    const suppressedEntriesCount = agentDecisions.filter((decision: ReturnType<typeof normalizeDecision>) =>
      String(decision.enqueue_status || "").includes("duplicate"),
    ).length
    const governanceEvents = Array.isArray(governanceRaw?.events) ? governanceRaw.events : []
    const lastRuntimeStartupFailure =
      governanceEvents
        .map((event: any) => normalizeRuntimeStartupFailure(event))
        .find((event: ReturnType<typeof normalizeRuntimeStartupFailure>) => Boolean(event)) ?? null

    const data = {
      isRunning: mt4Connected && mt4Fresh && ticksFresh && runtimeSignalFresh,
      bridgeState,
      statusTier,
      mt4Connected,
      mt4Fresh,
      isStale,
      signalDataFresh,
      runtimeSignalFresh,
      runtimePhase,
      runtimePhasePair,
      runtimePhaseIndex: Number.isFinite(runtimePhaseIndex) ? runtimePhaseIndex : 0,
      runtimePhaseTotal: Number.isFinite(runtimePhaseTotal) ? runtimePhaseTotal : 0,
      runtimeLastProgressAgeSecs,
      runtimeFailureReason,
      runtimeBootId,
      lastRuntimeStartupFailure,
      systemStatus,
      heartbeatStaleAfterSecs,
      runtimeCycleAgeSecs,
      runtimeCycleStaleAfterSecs,
      equity,
      displayEquity: mt4Fresh && ticksFresh ? liveEquity : null,
      cachedEquity: mt4Fresh ? null : liveEquity,
      margin,
      freemargin: freeMargin,
      positions,
      openPositionsCount,
      agentDecisions,
      readyEntriesCount,
      queuedEntriesCount,
      suppressedEntriesCount,
      tickStatus: String(raw?.tick_status || "unknown"),
      tickReason: String(raw?.tick_reason || "unknown"),
      tickSymbolsCount: Number(raw?.tick_symbols_count || 0),
      tickMaxAgeSecs: asFiniteNumber(raw?.tick_max_age_secs),
      signalDataReason:
        runtimeStatus === "failed"
          ? "runtime_startup_failed"
          : runtimeStatus === "stalled"
            ? "runtime_startup_stalled"
            : runtimeStatus === "starting"
              ? "runtime_starting"
              : !runtimeSignalFresh
                ? "runtime_cycle_stale"
                : String(raw?.tick_reason || raw?.tick_status || (signalDataFresh ? "fresh" : "stale")),
      lastHeartbeat,
      heartbeatAgeSecs: heartbeatAgeSecs ?? null,
      cycleActive: Boolean(raw?.cycle_active || raw?.cycleActive || false),
      cycleStartEquity: Number(raw?.cycle_start_equity || raw?.cycleStartEquity || 0),
      cycleTarget: Number(raw?.cycle_target || raw?.cycleTarget || 0),
      signalsSent: Number(raw?.signals_sent || raw?.signalsSent || 0),
      tradesExecuted: Number(raw?.trades_executed || raw?.tradesExecuted || 0),
      lastSignal: raw?.last_signal || raw?.lastSignal || null,
      lastAck: raw?.last_ack || raw?.lastAck || null,
      monitor: raw?.monitor || monitor?.monitor || null,
      governance: raw?.governance || null,
      riskEnvelope: raw?.risk_envelope || raw?.riskEnvelope || null,
      runtimeDiag: raw?.runtime_diag || null,
      shadowPolicy: normalizeShadowPolicy(raw?.runtime_diag?.shadow_policy),
      adaptiveShadowPolicy: normalizeAdaptiveShadowPolicy(raw?.runtime_diag?.adaptive_shadow_policy),
      entryExecutionPolicy: normalizeEntryExecutionPolicy(raw?.runtime_diag?.entry_execution_policy),
      runtimeStatus: String(raw?.runtime_status || raw?.runtimeStatus || "unknown"),
      lastUpdate: raw?.last_update || raw?.lastUpdate || null,
      equitySource:
        !mt4Fresh
          ? "stale_or_missing_heartbeat"
          : raw?.mt4_equity !== undefined || raw?.mt4Equity !== undefined
          ? "mt4_equity"
          : raw?.account_equity !== undefined || raw?.accountEquity !== undefined
            ? "account_equity"
            : raw?.monitor?.account?.equity !== undefined || monitor?.account?.equity !== undefined
              ? "monitor.account.equity"
              : "equity",
    }

    return NextResponse.json({ status: "success", data })
  } catch (error: any) {
    console.error("[api/trading/state] Failed to fetch state:", error)
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Failed to fetch state",
        data: {
          isRunning: false,
          bridgeState: "bridge_down",
          statusTier: "bridge_down",
          mt4Connected: false,
          mt4Fresh: false,
          isStale: true,
          signalDataFresh: false,
          runtimeSignalFresh: false,
          runtimePhase: "",
          runtimePhasePair: "",
          runtimePhaseIndex: 0,
          runtimePhaseTotal: 0,
          runtimeLastProgressAgeSecs: null,
          runtimeFailureReason: "",
          runtimeBootId: "",
          lastRuntimeStartupFailure: null,
          signalDataReason: "state_proxy_error",
          tickStatus: "unknown",
          tickReason: "state_proxy_error",
          tickSymbolsCount: 0,
          tickMaxAgeSecs: null,
          runtimeStatus: "error",
          shadowPolicy: {
            enabled: false,
            candidateCount: 0,
            rankedCount: 0,
            wouldTradeCount: 0,
            remainingSlots: 0,
            maxNewEntries: 0,
            structureRescueCount: 0,
            structureRescuesByPair: {},
            divergenceCounts: {
              agreeReady: 0,
              agreeBlocked: 0,
              liveOnly: 0,
              shadowOnly: 0,
              openPosition: 0,
            },
            dominantRejectionReason: "",
            rejectionReasonCounts: {},
            rejectionsByPair: {},
            tierSummary: {},
            spreadDiagnostics: {
              rejectCount: 0,
              dominantPair: "",
              dominantSession: "",
              byPair: {},
              bySession: {},
            },
            secondarySpreadDiagnostics: {
              rejectCount: 0,
              dominantPair: "",
              dominantSession: "",
              byPair: {},
              bySession: {},
            },
          },
          adaptiveShadowPolicy: {
            enabled: false,
            candidateCount: 0,
            rankedCount: 0,
            wouldTradeCount: 0,
            remainingSlots: 0,
            maxNewEntries: 0,
            aggressiveFallbackCount: 0,
            divergenceCounts: {
              agreeReady: 0,
              agreeBlocked: 0,
              liveOnly: 0,
              adaptiveOnly: 0,
              openPosition: 0,
            },
            dominantRejectionReason: "",
            rejectionReasonCounts: {},
            rejectionsByPair: {},
            playbookCounts: {},
            environmentCounts: {},
          },
          entryExecutionPolicy: {
            executionMode: "",
            adaptiveExecutionEnabled: false,
            pendingEntryCount: 0,
            approvedEntryCount: 0,
            blockedEntryCount: 0,
            submittedEntryCount: 0,
            duplicateEntryCount: 0,
          },
          runtimeCycleAgeSecs: null,
          runtimeCycleStaleAfterSecs: 30,
          heartbeatStaleAfterSecs: 30,
          heartbeatAgeSecs: null,
          displayEquity: null,
          cachedEquity: null,
          lastHeartbeat: null,
          systemStatus: "error",
          equity: 0,
          positions: [],
          openPositionsCount: 0,
          agentDecisions: [],
          readyEntriesCount: 0,
          queuedEntriesCount: 0,
          suppressedEntriesCount: 0,
          cycleActive: false,
          cycleStartEquity: 0,
          cycleTarget: 0,
          signalsSent: 0,
          tradesExecuted: 0,
          lastSignal: null,
        },
      },
      { status: 503 },
    )
  }
}
