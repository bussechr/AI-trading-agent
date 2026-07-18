// AGENT: ROLE: Normalize mixed bridge `/v2/state` payloads into the stable dashboard contract consumed by the polling hook.
// AGENT: ENTRYPOINT: Next.js route `GET /api/trading/state`.
// AGENT: PRIMARY INPUTS: bridge JSON from `lib/server/bridge.ts`, mixed runtime diagnostics, ticks, positions, shadow/adaptive summaries.
// AGENT: PRIMARY OUTPUTS: normalized dashboard state for the client hook.
// AGENT: DEPENDS ON: `lib/server/bridge.ts`.
// AGENT: CALLED BY: `lib/hooks/use-live-bridge-state.ts`.
// AGENT: STATE / SIDE EFFECTS: fetches bridge state only; no persistence.
// AGENT: HANDSHAKES: bridge `/v2/state`, dashboard client polling contract, runtime startup failure normalization.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `lib/hooks/use-live-bridge-state.ts` -> `docs/agents/bridge-and-api-handshakes.md`
import { NextResponse } from "next/server"
import { BRIDGE_URL, fetchBridgeJson, fetchBridgeObjectWithSource } from "@/lib/server/bridge"
import { ageSecsFromTimestamp, normalizeAgeSecs, timestampToMs } from "@/lib/trading/freshness"
import { shouldSuppressRuntimeStartupFailure } from "@/lib/trading/runtime-startup"
import { isLiveStateRunning, normalizeBridgeStatusTier } from "@/lib/trading/status-tier"
import { tickMidPrice } from "@/lib/trading/ticks"

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

function normalizeStringList(raw: any): string[] {
  if (Array.isArray(raw)) {
    return raw
      .map((value) => String(value || "").trim())
      .filter((value, index, values) => value.length > 0 && values.indexOf(value) === index)
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw)
      .filter(([, value]) => Boolean(value))
      .map(([key]) => String(key || "").trim())
      .filter((value, index, values) => value.length > 0 && values.indexOf(value) === index)
  }
  const txt = String(raw || "").trim()
  return txt ? [txt] : []
}

function normalizeObjectMap(raw: any): Record<string, any> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {}
  return Object.fromEntries(Object.entries(raw).map(([key, value]) => [String(key).toUpperCase(), value]))
}

function normalizeAnyObject(raw: any): Record<string, any> {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {}
  return { ...raw }
}

function normalizeRlPortfolioProposal(raw: any): Record<string, any> {
  const row = normalizeAnyObject(raw)
  const proposalsByPair = normalizeObjectMap(row.proposals_by_pair ?? row.proposalsByPair)
  const diagnostics = normalizeAnyObject(row.diagnostics)
  const pairUniverse = normalizeStringList(row.pair_universe ?? row.pairUniverse)
  return {
    ...row,
    pairUniverse,
    proposalsByPair,
    checkpointLoaded: Boolean(row.checkpoint_loaded ?? row.checkpointLoaded ?? false),
    checkpointPath: String(row.checkpoint_path || row.checkpointPath || ""),
    source: String(row.source || ""),
    supervisedFallbackUsed: Boolean(row.supervised_fallback_used ?? row.supervisedFallbackUsed ?? false),
    fallbackReason: String(row.fallback_reason || row.fallbackReason || ""),
    proposalCount: Number(row.proposal_count ?? row.proposalCount ?? Object.keys(proposalsByPair).length),
    candidateCount: Number(row.candidate_count ?? row.candidateCount ?? diagnostics.decision_count ?? 0),
    strategyEngineMode: String(row.strategy_engine_mode || row.strategyEngineMode || "supervised_legacy"),
    proposalSource: String(row.proposal_source || row.proposalSource || row.source || ""),
    routedEntryCount: Number(row.routed_entry_count ?? row.routedEntryCount ?? 0),
    blockedEntryCount: Number(row.blocked_entry_count ?? row.blockedEntryCount ?? 0),
    fallbackEntryCount: Number(row.fallback_entry_count ?? row.fallbackEntryCount ?? 0),
    scaledEntryCount: Number(row.scaled_entry_count ?? row.scaledEntryCount ?? 0),
    lifecycleReviewedCount: Number(row.lifecycle_reviewed_count ?? row.lifecycleReviewedCount ?? 0),
    lifecycleAppliedCount: Number(row.lifecycle_applied_count ?? row.lifecycleAppliedCount ?? 0),
    lifecycleExitCount: Number(row.lifecycle_exit_count ?? row.lifecycleExitCount ?? 0),
    lifecycleFlipExitCount: Number(row.lifecycle_flip_exit_count ?? row.lifecycleFlipExitCount ?? 0),
    lifecycleResizeCount: Number(row.lifecycle_resize_count ?? row.lifecycleResizeCount ?? 0),
    lifecycleTightenStopCount: Number(row.lifecycle_tighten_stop_count ?? row.lifecycleTightenStopCount ?? 0),
    lifecyclePreservedExitCount: Number(row.lifecycle_preserved_exit_count ?? row.lifecyclePreservedExitCount ?? 0),
    lifecycleFallbackCount: Number(row.lifecycle_fallback_count ?? row.lifecycleFallbackCount ?? 0),
    lifecyclePairs: normalizeStringList(row.lifecycle_pairs ?? row.lifecyclePairs),
    executionMode: String(row.execution_mode || row.executionMode || ""),
    diagnostics,
  }
}

function normalizeRlExecutionPolicy(raw: any): Record<string, any> {
  const row = normalizeAnyObject(raw)
  return {
    ...row,
    checkpointLoaded: Boolean(row.checkpoint_loaded ?? row.checkpointLoaded ?? false),
    checkpointPath: String(row.checkpoint_path || row.checkpointPath || ""),
    proposalSource: String(row.proposal_source || row.proposalSource || ""),
    supervisedFallbackUsed: Boolean(row.supervised_fallback_used ?? row.supervisedFallbackUsed ?? false),
    fallbackReason: String(row.fallback_reason || row.fallbackReason || ""),
    routedEntryCount: Number(row.routed_entry_count ?? row.routedEntryCount ?? 0),
    blockedEntryCount: Number(row.blocked_entry_count ?? row.blockedEntryCount ?? 0),
    fallbackEntryCount: Number(row.fallback_entry_count ?? row.fallbackEntryCount ?? 0),
    scaledEntryCount: Number(row.scaled_entry_count ?? row.scaledEntryCount ?? 0),
    lifecycleReviewedCount: Number(row.rl_lifecycle_reviewed_count ?? row.lifecycle_reviewed_count ?? row.lifecycleReviewedCount ?? 0),
    lifecycleAppliedCount: Number(row.rl_lifecycle_applied_count ?? row.lifecycle_applied_count ?? row.lifecycleAppliedCount ?? 0),
    lifecycleExitCount: Number(row.rl_lifecycle_exit_count ?? row.lifecycle_exit_count ?? row.lifecycleExitCount ?? 0),
    lifecycleFlipExitCount: Number(row.rl_lifecycle_flip_exit_count ?? row.lifecycle_flip_exit_count ?? row.lifecycleFlipExitCount ?? 0),
    lifecycleResizeCount: Number(row.rl_lifecycle_resize_count ?? row.lifecycle_resize_count ?? row.lifecycleResizeCount ?? 0),
    lifecycleTightenStopCount: Number(row.rl_lifecycle_tighten_stop_count ?? row.lifecycle_tighten_stop_count ?? row.lifecycleTightenStopCount ?? 0),
    lifecyclePreservedExitCount: Number(row.rl_lifecycle_preserved_exit_count ?? row.lifecycle_preserved_exit_count ?? row.lifecyclePreservedExitCount ?? 0),
    lifecycleFallbackCount: Number(row.rl_lifecycle_fallback_count ?? row.lifecycle_fallback_count ?? row.lifecycleFallbackCount ?? 0),
    lifecyclePairs: normalizeStringList(row.rl_lifecycle_pairs ?? row.lifecycle_pairs ?? row.lifecyclePairs),
    executionMode: String(row.execution_mode || row.executionMode || ""),
    strategyEngineMode: String(row.strategy_engine_mode || row.strategyEngineMode || "supervised_legacy"),
  }
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

// AGENT HANDSHAKE: Startup failure normalization isolates bridge/runtime boot diagnostics from the rest of the dashboard contract.
function normalizeRuntimeStartupFailure(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const payload = row.payload_json && typeof row.payload_json === "object" ? row.payload_json : row.payload && typeof row.payload === "object" ? row.payload : {}
  const eventType = String(row.event_type || row.eventType || "")
  if (eventType !== "runtime_startup_failed") return null
  const failedAtRaw = payload.failed_at ?? row.failed_at ?? row.time ?? row.ts ?? null
  const failedAtMs = timestampToMs(failedAtRaw)
  return {
    eventType,
    reason: String(row.reason || payload.failure_reason || ""),
    bootId: String(payload.boot_id || ""),
    phase: String(payload.phase || ""),
    phasePair: String(payload.phase_pair || "").toUpperCase(),
    failedAt: failedAtMs > 0 ? new Date(failedAtMs).toISOString() : null,
    failedAgeSecs: ageSecsFromTimestamp(failedAtRaw),
  }
}

function normalizeRuntimeStartupSummary(raw: any, runtimeStatus: string) {
  const row = raw && typeof raw === "object" ? raw : {}
  const lastFailureRaw =
    row.last_runtime_startup_failure && typeof row.last_runtime_startup_failure === "object"
      ? row.last_runtime_startup_failure
      : row.lastRuntimeStartupFailure && typeof row.lastRuntimeStartupFailure === "object"
        ? row.lastRuntimeStartupFailure
        : {}
  const summaryRaw =
    row.runtime_startup_summary && typeof row.runtime_startup_summary === "object"
      ? row.runtime_startup_summary
      : row.runtimeStartupSummary && typeof row.runtimeStartupSummary === "object"
        ? row.runtimeStartupSummary
        : row.runtime_startup && typeof row.runtime_startup === "object"
          ? row.runtime_startup
          : {}
  const runtimeDiag = row.runtime_diag && typeof row.runtime_diag === "object" ? row.runtime_diag : {}
  const phase = String(summaryRaw.phase || row.runtime_phase || row.runtimePhase || "").trim().toLowerCase()
  const phasePair = String(summaryRaw.phase_pair || row.runtime_phase_pair || row.runtimePhasePair || "").trim().toUpperCase()
  const failureReason = String(summaryRaw.failure_reason || row.runtime_failure_reason || row.runtimeFailureReason || "").trim()
  const startupDisabledPairs = normalizeStringList(summaryRaw.startup_disabled_pairs ?? runtimeDiag.startup_disabled_pairs)
  const modelLoadErrors = Number(summaryRaw.model_load_errors ?? row.model_load_errors ?? runtimeDiag.model_load_errors ?? 0)
  const modelLoadTimeouts = Number(summaryRaw.model_load_timeouts ?? row.model_load_timeouts ?? runtimeDiag.model_load_timeouts ?? 0)
  const startupInferenceFailures = Number(
    summaryRaw.startup_inference_failures ?? row.startup_inference_failures ?? runtimeDiag.startup_inference_failures ?? 0,
  )
  const warningCount = Number(
    summaryRaw.warning_count ??
      row.runtime_startup_warning_count ??
      row.runtimeStartupWarningCount ??
      (modelLoadErrors > 0 ? 1 : 0) +
        (modelLoadTimeouts > 0 ? 1 : 0) +
        (startupInferenceFailures > 0 ? 1 : 0) +
        (startupDisabledPairs.length > 0 ? 1 : 0),
  )
  const status =
    String(summaryRaw.status || row.runtime_startup_status || row.runtimeStartupStatus || "").trim().toLowerCase() ||
    (failureReason
      ? "failed"
      : runtimeStatus === "stalled"
        ? "stalled"
        : runtimeStatus === "starting"
          ? "starting"
          : runtimeStatus === "running" && warningCount > 0
            ? "recovered_with_warnings"
            : runtimeStatus === "running"
              ? "ready"
              : runtimeStatus || "unknown")
  return {
    bootId: String(summaryRaw.boot_id || row.runtime_boot_id || row.runtimeBootId || "").trim(),
    lastFailureBootId: String(lastFailureRaw.bootId || "").trim(),
    bootedAt: summaryRaw.booted_at ?? row.runtime_booted_at ?? row.runtimeBootedAt ?? null,
    runtimePid: summaryRaw.runtime_pid ?? row.runtime_pid ?? row.runtimePid ?? null,
    phase,
    phasePair,
    phaseIndex: Number(summaryRaw.phase_index ?? row.runtime_phase_index ?? row.runtimePhaseIndex ?? 0),
    phaseTotal: Number(summaryRaw.phase_total ?? row.runtime_phase_total ?? row.runtimePhaseTotal ?? 0),
    lastProgressTs: summaryRaw.last_progress_ts ?? row.runtime_startup?.last_progress_ts ?? null,
    lastProgressAgeSecs: normalizeAgeSecs(summaryRaw.last_progress_age_secs ?? row.runtime_last_progress_age_secs),
    failureReason,
    failedAt: summaryRaw.failed_at ?? row.runtime_failed_at ?? row.runtimeFailedAt ?? null,
    pendingCommandPolicy: String(summaryRaw.pending_command_policy || row.runtime_startup?.pending_command_policy || "").trim(),
    modelLoadErrors: Number(modelLoadErrors || 0),
    modelLoadTimeouts: Number(modelLoadTimeouts || 0),
    startupInferenceFailures: Number(startupInferenceFailures || 0),
    startupDisabledPairs,
    warningCount: Number(warningCount || 0),
    status,
    recovered: Boolean(runtimeStatus === "running" && !failureReason),
  }
}

// AGENT FLOW: Shadow/adaptive policy normalizers are the route-side contract boundary; UI code should not read raw bridge policy fields directly.
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

function normalizeOrchestrationShadow(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    enabled: Boolean(row.enabled ?? false),
    pairCount: Number(row.pair_count ?? row.pairCount ?? 0),
    packetCount: Number(row.packet_count ?? row.packetCount ?? 0),
    traceCount: Number(row.trace_count ?? row.traceCount ?? 0),
    faultCount: Number(row.fault_count ?? row.faultCount ?? 0),
    p50Ms: Number(row.p50_ms ?? row.p50Ms ?? 0),
    p95Ms: Number(row.p95_ms ?? row.p95Ms ?? 0),
    p99Ms: Number(row.p99_ms ?? row.p99Ms ?? 0),
    divergenceCounts:
      row.divergence_counts && typeof row.divergence_counts === "object" ? row.divergence_counts : {},
    faultCounts: row.fault_counts && typeof row.fault_counts === "object" ? row.fault_counts : {},
    perNodeLatencyMs:
      row.per_node_latency_ms && typeof row.per_node_latency_ms === "object" ? row.per_node_latency_ms : {},
  }
}

function normalizePaperExecution(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const governedDecision =
    row.governed_decision && typeof row.governed_decision === "object"
      ? row.governed_decision
      : row.governedDecision && typeof row.governedDecision === "object"
        ? row.governedDecision
        : {}
  const lastCommand =
    row.last_command && typeof row.last_command === "object"
      ? row.last_command
      : row.lastCommand && typeof row.lastCommand === "object"
        ? row.lastCommand
        : {}
  const lastEvent =
    row.last_event && typeof row.last_event === "object"
      ? row.last_event
      : row.lastEvent && typeof row.lastEvent === "object"
        ? row.lastEvent
        : {}
  const eventFlow =
    row.event_flow && typeof row.event_flow === "object"
      ? row.event_flow
      : row.eventFlow && typeof row.eventFlow === "object"
        ? row.eventFlow
        : {}
  return {
    enabled: Boolean(row.enabled ?? false),
    executionProvider: String(row.execution_provider || row.executionProvider || ""),
    agentMode: String(row.agent_mode || row.agentMode || ""),
    pendingCommandCount: Number(row.pending_command_count ?? row.pendingCommandCount ?? 0),
    orphanCommandCount: Number(row.orphan_command_count ?? row.orphanCommandCount ?? 0),
    recentCommandCount: Number(row.recent_command_count ?? row.recentCommandCount ?? 0),
    governedDecision,
    lastCommand,
    lastEvent,
    eventFlow,
  }
}

function normalizeOrchestrationLive(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const governedDecision =
    row.governed_decision && typeof row.governed_decision === "object"
      ? row.governed_decision
      : row.governedDecision && typeof row.governedDecision === "object"
        ? row.governedDecision
        : {}
  const lastCommand =
    row.last_command && typeof row.last_command === "object"
      ? row.last_command
      : row.lastCommand && typeof row.lastCommand === "object"
        ? row.lastCommand
        : {}
  const lastEvent =
    row.last_event && typeof row.last_event === "object"
      ? row.last_event
      : row.lastEvent && typeof row.lastEvent === "object"
        ? row.lastEvent
        : {}
  const eventFlow =
    row.event_flow && typeof row.event_flow === "object"
      ? row.event_flow
      : row.eventFlow && typeof row.eventFlow === "object"
        ? row.eventFlow
        : {}
  return {
    enabled: Boolean(row.enabled ?? false),
    agentMode: String(row.agent_mode || row.agentMode || ""),
    executionProvider: String(row.execution_provider || row.executionProvider || ""),
    releaseStatus: String(row.release_status || row.releaseStatus || ""),
    bundleRunId: String(row.bundle_run_id || row.bundleRunId || ""),
    activePairScope: Array.isArray(row.active_pair_scope) ? row.active_pair_scope : Array.isArray(row.activePairScope) ? row.activePairScope : [],
    activeSleeveScope: Array.isArray(row.active_sleeve_scope) ? row.active_sleeve_scope : Array.isArray(row.activeSleeveScope) ? row.activeSleeveScope : [],
    activeIntentScope: Array.isArray(row.active_intent_scope) ? row.active_intent_scope : Array.isArray(row.activeIntentScope) ? row.activeIntentScope : [],
    rampStepsPct: Array.isArray(row.ramp_steps_pct) ? row.ramp_steps_pct : Array.isArray(row.rampStepsPct) ? row.rampStepsPct : [],
    currentStageIndex: Number(row.current_stage_index ?? row.currentStageIndex ?? 0),
    currentStagePct: Number(row.current_stage_pct ?? row.currentStagePct ?? 0),
    budgetScale: Number(row.budget_scale ?? row.budgetScale ?? 0),
    runtimeEnabled: Boolean(row.runtime_enabled ?? row.runtimeEnabled ?? true),
    queueKillActive: Boolean(row.queue_kill_active ?? row.queueKillActive ?? false),
    queueKillReason: String(row.queue_kill_reason || row.queueKillReason || ""),
    queueKilledAt: row.queue_killed_at ?? row.queueKilledAt ?? 0,
    promotionPackPath: String(row.promotion_pack_path || row.promotionPackPath || ""),
    signoffRecords: Array.isArray(row.signoff_records) ? row.signoff_records : Array.isArray(row.signoffRecords) ? row.signoffRecords : [],
    pendingCommandCount: Number(row.pending_command_count ?? row.pendingCommandCount ?? 0),
    orphanCommandCount: Number(row.orphan_command_count ?? row.orphanCommandCount ?? 0),
    ackSuccessRate: Number(row.ack_success_rate ?? row.ackSuccessRate ?? 0),
    ackTimeoutRate: Number(row.ack_timeout_rate ?? row.ackTimeoutRate ?? 0),
    overheadP95Ms: Number(row.overhead_p95_ms ?? row.overheadP95Ms ?? 0),
    overheadP99Ms: Number(row.overhead_p99_ms ?? row.overheadP99Ms ?? 0),
    entryRatioVsBaseline: Number(row.entry_ratio_vs_baseline ?? row.entryRatioVsBaseline ?? 0),
    slotUtilisationVsBaseline: Number(row.slot_utilisation_vs_baseline ?? row.slotUtilisationVsBaseline ?? 0),
    drawdownDeteriorationPct: Number(row.drawdown_deterioration_pct ?? row.drawdownDeteriorationPct ?? 0),
    repeatedGraphFaultCount: Number(row.repeated_graph_fault_count ?? row.repeatedGraphFaultCount ?? 0),
    tracePersistenceFailureCount: Number(row.trace_persistence_failure_count ?? row.tracePersistenceFailureCount ?? 0),
    baselineFallbackCount: Number(row.baseline_fallback_count ?? row.baselineFallbackCount ?? 0),
    governedDecision,
    lastCommand,
    lastEvent,
    eventFlow,
  }
}

function normalizeOrchestrationLiveHealth(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    status: String(row.status || row.healthStatus || "healthy"),
    reason: String(row.reason || row.healthReason || ""),
    reasons: normalizeStringList(row.reasons ?? row.reason_list ?? row.healthReasons),
    warningCount: Number(row.warning_count ?? row.warningCount ?? 0),
    blockingCount: Number(row.blocking_count ?? row.blockingCount ?? 0),
    runtimeStatus: String(row.runtime_status || row.runtimeStatus || ""),
    runtimeReady: Boolean(row.runtime_ready ?? row.runtimeReady ?? false),
    statusTier: String(row.status_tier || row.statusTier || ""),
    runtimeEnabled: Boolean(row.runtime_enabled ?? row.runtimeEnabled ?? true),
    queueKillActive: Boolean(row.queue_kill_active ?? row.queueKillActive ?? false),
    pendingCommandCount: Number(row.pending_command_count ?? row.pendingCommandCount ?? 0),
    orphanCommandCount: Number(row.orphan_command_count ?? row.orphanCommandCount ?? 0),
    ackSuccessRate: Number(row.ack_success_rate ?? row.ackSuccessRate ?? 0),
    ackTimeoutRate: Number(row.ack_timeout_rate ?? row.ackTimeoutRate ?? 0),
    ackTimeoutSpike: Boolean(row.ack_timeout_spike ?? row.ackTimeoutSpike ?? false),
    repeatedGraphFaultCount: Number(row.repeated_graph_fault_count ?? row.repeatedGraphFaultCount ?? 0),
    tracePersistenceFailureCount: Number(row.trace_persistence_failure_count ?? row.tracePersistenceFailureCount ?? 0),
    baselineFallbackCount: Number(row.baseline_fallback_count ?? row.baselineFallbackCount ?? 0),
  }
}

function normalizeTradeFlowSummary(
  raw: any,
  entryExecutionPolicy: ReturnType<typeof normalizeEntryExecutionPolicy>,
  shadowPolicy: ReturnType<typeof normalizeShadowPolicy>,
  adaptiveShadowPolicy: ReturnType<typeof normalizeAdaptiveShadowPolicy>,
  shadowOrchestrator: ReturnType<typeof normalizeOrchestrationShadow>,
  orchestrationLive: ReturnType<typeof normalizeOrchestrationLive>,
  featureObservability: ReturnType<typeof normalizeFeatureObservability>,
  capitalGovernance: ReturnType<typeof normalizeCapitalGovernance>,
) {
  const row = raw && typeof raw === "object" ? raw : {}
  const approvedEntryCount = Number(entryExecutionPolicy.approvedEntryCount || 0)
  const submittedEntryCount = Number(entryExecutionPolicy.submittedEntryCount || 0)
  const blockedEntryCount = Number(entryExecutionPolicy.blockedEntryCount || 0)
  const pendingEntryCount = Number(entryExecutionPolicy.pendingEntryCount || 0)
  const ackSuccessRate = asFiniteNumber((orchestrationLive as any).ackSuccessRate ?? (orchestrationLive as any).ack_success_rate)
  const ackTimeoutRate = asFiniteNumber((orchestrationLive as any).ackTimeoutRate ?? (orchestrationLive as any).ack_timeout_rate)
  const orchestrationLiveHealth = normalizeOrchestrationLiveHealth(
    raw?.orchestration_live_health || raw?.orchestrationLiveHealth || orchestrationLive,
  )
  const runtimeStatus = String(row.runtime_status || row.runtimeStatus || "")
  const runtimeCycleAgeSecs = asFiniteNumber(row.runtime_cycle_age_secs ?? row.runtimeCycleAgeSecs)
  const runtimeReady = Boolean(
    row.runtime_ready ??
      row.runtimeReady ??
      (runtimeStatus === "running" && runtimeCycleAgeSecs !== null && runtimeCycleAgeSecs <= 30),
  )
  const canaryPairs = normalizeStringList(
    row.canary_pairs ??
      row.canaryPairs ??
      row.rollout_runtime?.active_pairs ??
      row.rolloutRuntime?.activePairs ??
      row.rollout_policy?.active_pairs ??
      row.rolloutPolicy?.activePairs,
  )
  return {
    signalsSent: Number(row.signals_sent ?? row.signalsSent ?? 0),
    tradesExecuted: Number(row.trades_executed ?? row.tradesExecuted ?? 0),
    readyEntriesCount: Number(row.ready_entries_count ?? row.readyEntriesCount ?? 0),
    queuedEntriesCount: Number(row.queued_entries_count ?? row.queuedEntriesCount ?? 0),
    suppressedEntriesCount: Number(row.suppressed_entries_count ?? row.suppressedEntriesCount ?? 0),
    pendingEntryCount,
    approvedEntryCount,
    blockedEntryCount,
    submittedEntryCount,
    ackSuccessRate,
    ackTimeoutRate,
    lastAck: row.last_ack || row.lastAck || null,
    lastAckStatus: String(row.last_ack_status || row.lastAckStatus || (orchestrationLive as any)?.lastCommand?.status || ""),
    canaryActive: Boolean(capitalGovernance.canaryActive || (orchestrationLive as any).enabled || row.canary_active || row.canaryActive),
    canaryPairs,
    canaryStagePct: Number((orchestrationLive as any).currentStagePct || row.canary_stage_pct || row.canaryStagePct || 0),
    canaryRuntimeEnabled: Boolean((orchestrationLive as any).runtimeEnabled ?? row.runtime_enabled ?? true),
    canaryQueueKillActive: Boolean((orchestrationLive as any).queueKillActive ?? row.queue_kill_active ?? false),
    divergenceCounts: {
      shadowLiveOnly: Number(shadowPolicy.divergenceCounts.liveOnly || 0),
      shadowShadowOnly: Number(shadowPolicy.divergenceCounts.shadowOnly || 0),
      adaptiveLiveOnly: Number(adaptiveShadowPolicy.divergenceCounts.liveOnly || 0),
      adaptiveAdaptiveOnly: Number(adaptiveShadowPolicy.divergenceCounts.adaptiveOnly || 0),
      orchestratorFaultCount: Number(shadowOrchestrator.faultCount || 0),
    },
    canaryHealth: {
      runtimeStatus,
      runtimeReady,
      featureOnlineReady: Boolean(row.feature_online_ready ?? row.featureOnlineReady ?? false),
      featureDataFresh: Boolean(row.feature_data_fresh ?? row.featureDataFresh ?? false),
      featurePushBacklog: Number(row.feature_push_backlog ?? row.featurePushBacklog ?? 0),
      featurePushBacklogOk: Boolean(row.feature_push_backlog_ok ?? row.featurePushBacklogOk ?? true),
      featureBlockerReason: String(row.feature_blocker_reason || row.featureBlockerReason || ""),
      featureBlockerReasons: normalizeStringList(row.feature_blocker_reasons ?? row.featureBlockerReasons),
      featureBlockerSource: String(row.feature_blocker_source || row.featureBlockerSource || featureObservability.featureBlockerSource || ""),
      featureBarStatus: String(row.feature_bar_status || row.featureBarStatus || featureObservability.featureBarStatus || ""),
      featureServingSource: String(row.feature_serving_source || row.featureServingSource || row.featureServing?.source || ""),
      featureServingReason: String(row.feature_serving_reason || row.featureServingReason || row.featureServing?.reason || ""),
      orchestrationLiveHealth,
    },
  }
}

function normalizeOrchestrationEvidence(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    ...row,
    experimentCount: Number(row.experiment_count ?? row.experimentCount ?? 0),
    promotionCount: Number(row.promotion_count ?? row.promotionCount ?? 0),
    approvalEventCount: Number(row.approval_event_count ?? row.approvalEventCount ?? 0),
    latestExperimentId: String(row.latest_experiment_id || row.latestExperimentId || ""),
    latestPromotionId: String(row.latest_promotion_id || row.latestPromotionId || ""),
    latestApprovalEventId: String(row.latest_approval_event_id || row.latestApprovalEventId || ""),
    latestPromotionStatus: String(row.latest_promotion_status || row.latestPromotionStatus || ""),
    latestApprovalDecision: String(row.latest_approval_decision || row.latestApprovalDecision || ""),
    latestLineage:
      row.latest_lineage && typeof row.latest_lineage === "object"
        ? row.latest_lineage
        : row.latestLineage && typeof row.latestLineage === "object"
          ? row.latestLineage
          : {},
  }
}

// AGENT HANDSHAKE: Execution policy normalization exposes live strict/adaptive entry counts without leaking raw runtime diagnostic shape to the client.
function normalizeEntryExecutionPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const rejectionReasonCounts =
    row.rejection_reason_counts && typeof row.rejection_reason_counts === "object"
      ? row.rejection_reason_counts
      : row.rejectionReasonCounts && typeof row.rejectionReasonCounts === "object"
        ? row.rejectionReasonCounts
        : row.reject_reason_counts && typeof row.reject_reason_counts === "object"
          ? row.reject_reason_counts
          : row.rejectReasonCounts && typeof row.rejectReasonCounts === "object"
            ? row.rejectReasonCounts
            : {}
  const rejectionReasonSummary =
    row.rejection_reason_summary && typeof row.rejection_reason_summary === "object"
      ? row.rejection_reason_summary
      : row.rejectionReasonSummary && typeof row.rejectionReasonSummary === "object"
        ? row.rejectionReasonSummary
        : row.reject_reason_summary && typeof row.reject_reason_summary === "object"
          ? row.reject_reason_summary
          : row.rejectReasonSummary && typeof row.rejectReasonSummary === "object"
            ? row.rejectReasonSummary
            : {}
  return {
    executionMode: String(row.execution_mode || row.executionMode || ""),
    adaptiveExecutionEnabled: Boolean(row.adaptive_execution_enabled ?? row.adaptiveExecutionEnabled ?? false),
    pendingEntryCount: Number(row.pending_entry_count ?? row.pendingEntryCount ?? row.pending_count ?? row.pendingCount ?? 0),
    approvedEntryCount: Number(row.approved_entry_count ?? row.approvedEntryCount ?? row.approved_count ?? row.approvedCount ?? 0),
    blockedEntryCount: Number(row.blocked_entry_count ?? row.blockedEntryCount ?? row.blocked_count ?? row.blockedCount ?? 0),
    submittedEntryCount: Number(row.submitted_entry_count ?? row.submittedEntryCount ?? row.submitted_count ?? row.submittedCount ?? 0),
    duplicateEntryCount: Number(row.duplicate_entry_count ?? row.duplicateEntryCount ?? row.duplicate_count ?? row.duplicateCount ?? 0),
    dominantRejectionReason: String(
      row.dominant_rejection_reason || row.dominantRejectionReason || row.reject_reason || row.rejectReason || "",
    ),
    rejectionReasonCounts,
    rejectionReasonSummary,
  }
}

function normalizeAllocatorPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    candidateCount: Number(row.candidate_count || row.allocator_candidate_count || 0),
    selectedCount: Number(row.selected_count || row.allocator_selected_count || 0),
    rankedOutCount: Number(row.ranked_out_count || row.allocator_ranked_out_count || 0),
    replacementCandidateCount: Number(
      row.replacement_candidate_count || row.allocator_replacement_candidate_count || 0,
    ),
    replacementExitCount: Number(row.replacement_exit_count || row.allocator_replacement_exit_count || 0),
    sleeveCandidateCounts:
      row.sleeve_candidate_counts && typeof row.sleeve_candidate_counts === "object"
        ? row.sleeve_candidate_counts
        : row.allocator_sleeve_candidate_counts && typeof row.allocator_sleeve_candidate_counts === "object"
          ? row.allocator_sleeve_candidate_counts
          : {},
    sleeveSelectedCounts:
      row.sleeve_selected_counts && typeof row.sleeve_selected_counts === "object"
        ? row.sleeve_selected_counts
        : row.allocator_sleeve_selected_counts && typeof row.allocator_sleeve_selected_counts === "object"
          ? row.allocator_sleeve_selected_counts
          : {},
    sleeveBudgetTargets:
      row.sleeve_budget_targets && typeof row.sleeve_budget_targets === "object"
        ? row.sleeve_budget_targets
        : row.allocator_sleeve_budget_targets && typeof row.allocator_sleeve_budget_targets === "object"
          ? row.allocator_sleeve_budget_targets
          : {},
    sleeveBudgetUsed:
      row.sleeve_budget_used && typeof row.sleeve_budget_used === "object"
        ? row.sleeve_budget_used
        : row.allocator_sleeve_budget_used && typeof row.allocator_sleeve_budget_used === "object"
          ? row.allocator_sleeve_budget_used
          : {},
  }
}

function normalizeCampaignPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    enabled: Boolean(row.enabled ?? false),
    shadowOnly: Boolean(row.shadow_only ?? row.shadowOnly ?? true),
    abandonCooldownBars: Number(row.abandon_cooldown_bars || row.abandonCooldownBars || 0),
    pressProtectedBars: Number(row.press_protected_bars || row.pressProtectedBars || 0),
    reattackCooldownScale: Number(row.reattack_cooldown_scale || row.reattackCooldownScale || 0),
  }
}

function normalizeCampaignCycleSummary(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    stateCounts: row.state_counts && typeof row.state_counts === "object" ? row.state_counts : {},
    transitionCounts:
      row.transition_counts && typeof row.transition_counts === "object" ? row.transition_counts : {},
    registrySize: Number(row.registry_size || row.registrySize || 0),
    activePositionTheses: Number(row.active_position_theses || row.activePositionTheses || 0),
    reentryBlockedCount: Number(row.reentry_blocked_count || row.reentryBlockedCount || 0),
  }
}

function normalizeDirectionalBeliefPolicy(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    enabled: Boolean(row.enabled ?? false),
    runtimeRequired: Boolean(row.runtime_required ?? row.runtimeRequired ?? false),
    shortHorizonBars: Number(row.short_horizon_bars || row.shortHorizonBars || 0),
    tradeHorizonBars: Number(row.trade_horizon_bars || row.tradeHorizonBars || 0),
    structuralHorizonBars: Number(row.structural_horizon_bars || row.structuralHorizonBars || 0),
  }
}

function normalizeDirectionalBeliefCycleSummary(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    candidateCountWithBelief: Number(row.candidate_count_with_belief || row.candidateCountWithBelief || 0),
    avgBeliefGap: Number(row.avg_belief_gap || row.avgBeliefGap || 0),
    avgFragilityScore: Number(row.avg_fragility_score || row.avgFragilityScore || 0),
    avgPrimaryRankScore: Number(row.avg_primary_rank_score || row.avgPrimaryRankScore || 0),
    avgPrimaryEvAboveHurdleProb: Number(
      row.avg_primary_ev_above_hurdle_prob || row.avgPrimaryEvAboveHurdleProb || 0,
    ),
    avgPrimaryExpectedNetEvBps: Number(
      row.avg_primary_expected_net_ev_bps || row.avgPrimaryExpectedNetEvBps || 0,
    ),
    avgPrimaryFailFastProb: Number(row.avg_primary_fail_fast_prob || row.avgPrimaryFailFastProb || 0),
    noEdgeShare: Number(row.no_edge_share || row.noEdgeShare || 0),
    primaryScenarioCounts:
      row.primary_scenario_counts && typeof row.primary_scenario_counts === "object" ? row.primary_scenario_counts : {},
    oppositionScenarioCounts:
      row.opposition_scenario_counts && typeof row.opposition_scenario_counts === "object"
        ? row.opposition_scenario_counts
        : {},
    oppositionSideCounts:
      row.opposition_side_counts && typeof row.opposition_side_counts === "object"
        ? row.opposition_side_counts
        : {},
    artifactVersions:
      row.artifact_versions && typeof row.artifact_versions === "object" ? row.artifact_versions : {},
  }
}

function normalizeDirectionalBeliefMetrics(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    decisionCount: Number(row.decision_count || row.decisionCount || 0),
    beliefLoadedShare: Number(row.belief_loaded_share || row.beliefLoadedShare || 0),
    avgBeliefGap: Number(row.avg_belief_gap || row.avgBeliefGap || 0),
    avgFragilityScore: Number(row.avg_fragility_score || row.avgFragilityScore || 0),
    avgPrimaryRankScore: Number(row.avg_primary_rank_score || row.avgPrimaryRankScore || 0),
    avgPrimaryEvAboveHurdleProb: Number(
      row.avg_primary_ev_above_hurdle_prob || row.avgPrimaryEvAboveHurdleProb || 0,
    ),
    avgPrimaryExpectedNetEvBps: Number(
      row.avg_primary_expected_net_ev_bps || row.avgPrimaryExpectedNetEvBps || 0,
    ),
    avgPrimaryFailFastProb: Number(row.avg_primary_fail_fast_prob || row.avgPrimaryFailFastProb || 0),
    noEdgeShare: Number(row.no_edge_share || row.noEdgeShare || 0),
    primaryScenarioCounts:
      row.primary_scenario_counts && typeof row.primary_scenario_counts === "object" ? row.primary_scenario_counts : {},
    oppositionScenarioCounts:
      row.opposition_scenario_counts && typeof row.opposition_scenario_counts === "object"
        ? row.opposition_scenario_counts
        : {},
    oppositionSideCounts:
      row.opposition_side_counts && typeof row.opposition_side_counts === "object"
        ? row.opposition_side_counts
        : {},
  }
}

function normalizeOverlayPolicyTrace(raw: any): string[] {
  if (Array.isArray(raw)) {
    return raw.map((value) => String(value || "").trim()).filter(Boolean)
  }
  if (raw && typeof raw === "object") {
    return Object.entries(raw)
      .map(([key, value]) => `${String(key)}:${String(value ?? "")}`.trim())
      .filter(Boolean)
  }
  const txt = String(raw || "").trim()
  return txt ? [txt] : []
}

function normalizeOverlayCycleSummary(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  return {
    convictionScoreAvg: asFiniteNumber(row.conviction_score_avg ?? row.convictionScoreAvg),
    convictionScoreMax: asFiniteNumber(row.conviction_score_max ?? row.convictionScoreMax),
    convictionScoreMin: asFiniteNumber(row.conviction_score_min ?? row.convictionScoreMin),
    convictionBandCounts:
      row.conviction_band_counts && typeof row.conviction_band_counts === "object"
        ? row.conviction_band_counts
        : row.convictionBandCounts && typeof row.convictionBandCounts === "object"
          ? row.convictionBandCounts
          : {},
    thesisStageCounts:
      row.thesis_stage_counts && typeof row.thesis_stage_counts === "object"
        ? row.thesis_stage_counts
        : row.thesisStageCounts && typeof row.thesisStageCounts === "object"
          ? row.thesisStageCounts
          : {},
    postureCounts:
      row.posture_counts && typeof row.posture_counts === "object"
        ? row.posture_counts
        : row.postureCounts && typeof row.postureCounts === "object"
          ? row.postureCounts
          : {},
    sleeveBudgetTargetTotal: asFiniteNumber(row.sleeve_budget_target_total ?? row.sleeveBudgetTargetTotal),
    sleeveBudgetUsedTotal: asFiniteNumber(row.sleeve_budget_used_total ?? row.sleeveBudgetUsedTotal),
    replacementUrgencyAvg: asFiniteNumber(row.replacement_urgency_avg ?? row.replacementUrgencyAvg),
    policyTraceCount: Number(row.policy_trace_count || row.policyTraceCount || 0),
    diagnostics:
      row.diagnostics && typeof row.diagnostics === "object"
        ? row.diagnostics
        : row.overlay_diagnostics && typeof row.overlay_diagnostics === "object"
          ? row.overlay_diagnostics
          : {},
  }
}

function normalizeFeatureServing(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const summary =
    row.feature_observability && typeof row.feature_observability === "object"
      ? row.feature_observability
      : row.featureObservability && typeof row.featureObservability === "object"
        ? row.featureObservability
        : row.feature_serving_summary && typeof row.feature_serving_summary === "object"
          ? row.feature_serving_summary
          : row.featureServingSummary && typeof row.featureServingSummary === "object"
            ? row.featureServingSummary
            : {}
  const source = String(row.source || row.feature_serving_source || row.featureServingSource || summary.feature_serving_source || summary.featureServingSource || "")
  const featureService = String(
    row.feature_service || row.feature_serving_feature_service || row.featureServingFeatureService || summary.feature_serving_feature_service || summary.featureServingFeatureService || "",
  )
  const reason = String(row.reason || row.feature_serving_reason || row.featureServingReason || summary.feature_blocker_reason || summary.featureBlockerReason || "")
  const cacheHit = Boolean(row.cache_hit ?? row.feature_serving_cache_hit ?? row.featureServingCacheHit ?? summary.feature_online_ready ?? summary.featureOnlineReady ?? false)
  const stale = Boolean(row.stale ?? row.feature_serving_stale ?? row.featureServingStale ?? (summary.feature_data_fresh !== undefined ? !Boolean(summary.feature_data_fresh ?? summary.featureDataFresh) : false))
  return {
    source,
    sourceChain: Array.isArray(row.source_chain) ? row.source_chain.map((value: any) => String(value || "")) : [],
    featureService,
    cacheHit,
    freshnessSecs: asFiniteNumber(row.freshness_secs ?? row.feature_serving_freshness_secs ?? summary.feature_freshness_secs ?? summary.featureFreshnessSecs),
    stale,
    reason,
    latencyMs: asFiniteNumber(row.latency_ms ?? row.feature_serving_latency_ms ?? summary.feature_latency_ms ?? summary.featureLatencyMs ?? row.details?.latency_ms),
    backlog: Number(row.outbox_backlog ?? row.feature_push_backlog ?? row.featurePushBacklog ?? summary.feature_push_backlog ?? summary.featurePushBacklog ?? 0),
    parityBreaches: Number(row.parity_breaches ?? row.feature_parity_breaches ?? row.featureParityBreaches ?? summary.feature_parity_breaches ?? summary.featureParityBreaches ?? 0),
    byPair:
      row.by_pair && typeof row.by_pair === "object"
        ? row.by_pair
        : summary.by_pair && typeof summary.by_pair === "object"
          ? summary.by_pair
          : summary.byPair && typeof summary.byPair === "object"
            ? summary.byPair
            : {},
    details:
      row.details && typeof row.details === "object"
        ? row.details
        : summary.details && typeof summary.details === "object"
          ? summary.details
          : {},
  }
}

function normalizeFeatureObservability(raw: any, featureServing: ReturnType<typeof normalizeFeatureServing>) {
  const row = raw && typeof raw === "object" ? raw : {}
  const blockerReasons = normalizeStringList(row.feature_blocker_reasons ?? row.featureBlockerReasons)
  const featureOnlineReady = Boolean(
    row.feature_online_ready ??
      row.featureOnlineReady ??
      String(row.feature_serving_source || row.featureServingSource || featureServing.source || "").trim(),
  )
  const featureDataFresh = Boolean(
    row.feature_data_fresh ??
      row.featureDataFresh ??
      (featureOnlineReady && !Boolean(featureServing.stale)),
  )
  const featurePushBacklog = Number(row.feature_push_backlog ?? row.featurePushBacklog ?? featureServing.backlog ?? 0)
  const featurePushBacklogWarn = Number(row.feature_push_backlog_warn ?? row.featurePushBacklogWarn ?? 0)
  const featurePushBacklogOk = Boolean(
    row.feature_push_backlog_ok ??
      row.featurePushBacklogOk ??
      (featurePushBacklogWarn > 0 ? featurePushBacklog <= featurePushBacklogWarn : true),
  )
  const featurePushBacklogOverage = Number(
    row.feature_push_backlog_overage ??
      row.featurePushBacklogOverage ??
      Math.max(0, featurePushBacklog - featurePushBacklogWarn),
  )
  if (blockerReasons.length === 0) {
    if (!featureOnlineReady) {
      blockerReasons.push("feature_serving:missing")
    } else if (Boolean(featureServing.stale)) {
      blockerReasons.push("feature_serving:stale")
    }
    if (!featurePushBacklogOk) {
      blockerReasons.push("feature_push:backlog")
    }
  }
  const featureBlockerReason = String(
    row.feature_blocker_reason ||
      row.featureBlockerReason ||
      blockerReasons[0] ||
      "",
  )
  const featureBlockerSource = String(
    row.feature_blocker_source ||
      row.featureBlockerSource ||
      (featureBlockerReason.startsWith("feature_serving:") ? "feature_serving" : featureBlockerReason.startsWith("feature_push:") ? "feature_push" : ""),
  )
  const featureBarStatus = featureDataFresh ? "fresh" : Boolean(featureServing.stale) ? "stale" : "missing"
  return {
    featureOnlineReady,
    featureDataFresh,
    featurePushBacklog,
    featurePushBacklogWarn,
    featurePushBacklogOk,
    featurePushBacklogOverage,
    featureBlockerReason,
    featureBlockerReasons: blockerReasons,
    featureBlockerSource,
    featureBarStatus,
  }
}

function normalizeProviderHealth(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const roles = row.roles && typeof row.roles === "object" ? row.roles : row.provider_roles && typeof row.provider_roles === "object" ? row.provider_roles : {}
  const historyProvider =
    row.history_provider && typeof row.history_provider === "object"
      ? row.history_provider
      : row.historyProvider && typeof row.historyProvider === "object"
        ? row.historyProvider
        : {}
  const marketDataProvider =
    row.market_data_provider && typeof row.market_data_provider === "object"
      ? row.market_data_provider
      : row.marketDataProvider && typeof row.marketDataProvider === "object"
        ? row.marketDataProvider
        : {}
  const executionProvider =
    row.execution_provider && typeof row.execution_provider === "object"
      ? row.execution_provider
      : row.executionProvider && typeof row.executionProvider === "object"
        ? row.executionProvider
        : {}
  const historyProviderName = String(
    historyProvider.provider || historyProvider.name || roles.history_provider || row.history_provider_name || "",
  ).trim()
  const marketDataProviderName = String(
    marketDataProvider.provider || marketDataProvider.name || roles.market_data_provider || row.market_data_provider_name || "",
  ).trim()
  const executionProviderName = String(
    executionProvider.provider || executionProvider.name || roles.execution_provider || row.execution_provider_name || "",
  ).trim()
  const sourceChain = [historyProviderName, marketDataProviderName, executionProviderName].filter(Boolean)
  const fallbackReason = String(
    marketDataProvider.fallback_mode ||
      historyProvider.fallback_mode ||
      executionProvider.fallback_mode ||
      marketDataProvider.reason ||
      historyProvider.reason ||
      executionProvider.reason ||
      row.fallback_reason ||
      row.reason ||
      "",
  ).trim()
  const statusValues = [historyProvider.status, marketDataProvider.status, executionProvider.status, row.status]
    .map((value) => String(value || "").trim().toLowerCase())
    .filter(Boolean)
  return {
    historyProvider: historyProviderName,
    marketDataProvider: marketDataProviderName,
    executionProvider: executionProviderName,
    primaryProvider: String(
      row.primary_provider ||
        row.primaryProvider ||
        marketDataProviderName ||
        historyProviderName ||
        executionProviderName ||
        row.provider ||
        "",
    ),
    venue: String(row.venue || marketDataProvider.venue || historyProvider.venue || executionProvider.venue || ""),
    assetClass: String(
      row.asset_class || row.assetClass || historyProvider.asset_class || marketDataProvider.asset_class || executionProvider.asset_class || "",
    ),
    sourceChain,
    freshnessSecs: asFiniteNumber(
      row.freshness_secs ??
        row.freshnessSecs ??
        marketDataProvider.freshness_secs ??
        marketDataProvider.freshnessSecs ??
        historyProvider.freshness_secs ??
        historyProvider.freshnessSecs,
    ),
    stale: Boolean(row.stale ?? statusValues.includes("degraded")),
    fallbackActive: Boolean(
      row.fallback_active ??
        row.fallbackActive ??
        (fallbackReason.length > 0 || statusValues.includes("degraded")),
    ),
    fallbackReason,
    missingRate: asFiniteNumber(
      row.missing_rate ??
        row.missingRate ??
        marketDataProvider.missing_rate ??
        marketDataProvider.missingRate ??
        historyProvider.missing_rate ??
        historyProvider.missingRate,
    ),
    duplicateRate: asFiniteNumber(
      row.duplicate_rate ??
        row.duplicateRate ??
        marketDataProvider.duplicate_rate ??
        marketDataProvider.duplicateRate ??
        historyProvider.duplicate_rate ??
        historyProvider.duplicateRate,
    ),
    qualityFlags: normalizeStringList(
      row.quality_flags ??
        row.qualityFlags ??
        marketDataProvider.quality_flags ??
        marketDataProvider.qualityFlags ??
        historyProvider.quality_flags ??
        historyProvider.qualityFlags,
    ),
    bySymbol: row.by_symbol && typeof row.by_symbol === "object" ? row.by_symbol : {},
    details:
      row.details && typeof row.details === "object"
        ? row.details
        : {
            roles,
            history_provider: historyProvider,
            market_data_provider: marketDataProvider,
            execution_provider: executionProvider,
          },
  }
}

function normalizePortfolioTelemetry(raw: any, overlayCycleSummary: any = null) {
  const row = raw && typeof raw === "object" ? raw : {}
  const postureFallback =
    overlayCycleSummary && typeof overlayCycleSummary === "object"
      ? String(
          overlayCycleSummary.diagnostics?.environment_posture ||
            overlayCycleSummary.diagnostics?.portfolio_posture ||
            "",
        ).trim()
      : ""
  return {
    grossExposure: asFiniteNumber(row.gross_exposure ?? row.grossExposure),
    netExposure: asFiniteNumber(row.net_exposure ?? row.netExposure),
    exposureUnit: String(row.exposure_unit || row.exposureUnit || row.exposure_unit_kind || "lots"),
    openPositionCount: Number(row.open_position_count || row.openPositionCount || 0),
    pendingEntryCount: Number(row.pending_entry_count || row.pendingEntryCount || 0),
    replacementPressure: asFiniteNumber(row.replacement_pressure ?? row.replacementPressure),
    portfolioPosture: String(
      row.portfolio_posture ||
        row.portfolioPosture ||
        row.governance?.mode ||
        postureFallback ||
        "unknown",
    ),
    concentration: row.concentration && typeof row.concentration === "object" ? row.concentration : {},
    correlation: row.correlation && typeof row.correlation === "object" ? row.correlation : {},
    budgetTargets:
      row.budget && typeof row.budget === "object"
        ? row.budget
        : row.budget_targets && typeof row.budget_targets === "object"
          ? row.budget_targets
          : {},
    budgetUsed:
      row.budget_used && typeof row.budget_used === "object"
        ? row.budget_used
        : row.budget && typeof row.budget === "object"
          ? row.budget
          : {},
    bySymbol:
      row.per_symbol_exposure && typeof row.per_symbol_exposure === "object"
        ? row.per_symbol_exposure
        : row.by_symbol && typeof row.by_symbol === "object"
          ? row.by_symbol
          : {},
    details:
      row.details && typeof row.details === "object"
        ? row.details
        : {
            per_symbol_exposure: row.per_symbol_exposure && typeof row.per_symbol_exposure === "object" ? row.per_symbol_exposure : {},
            per_currency_exposure: row.per_currency_exposure && typeof row.per_currency_exposure === "object" ? row.per_currency_exposure : {},
            per_asset_class_exposure:
              row.per_asset_class_exposure && typeof row.per_asset_class_exposure === "object"
                ? row.per_asset_class_exposure
                : {},
            session_counts: row.session_counts && typeof row.session_counts === "object" ? row.session_counts : {},
            sleeve_counts: row.sleeve_counts && typeof row.sleeve_counts === "object" ? row.sleeve_counts : {},
            stress: row.stress && typeof row.stress === "object" ? row.stress : {},
            governance: row.governance && typeof row.governance === "object" ? row.governance : {},
          },
  }
}

function normalizeCapitalGovernance(raw: any, governanceRaw: any = null, stateRaw: any = null) {
  const row = raw && typeof raw === "object" ? raw : {}
  const governance = governanceRaw && typeof governanceRaw === "object" ? governanceRaw : {}
  const state = stateRaw && typeof stateRaw === "object" ? stateRaw : {}
  const rollbackActions = Array.isArray(row.rollback_actions) ? row.rollback_actions : []
  const rollbackArmed = rollbackActions.some((action: any) => Boolean(action && typeof action === "object" && action.armed))
  const activeTriggers = normalizeStringList(row.reasons ?? row.active_triggers ?? row.activeTriggers)
  return {
    capitalBand: String(row.capital_band || row.capitalBand || "unknown"),
    releaseMode: String(row.mode || row.release_mode || row.releaseMode || "normal"),
    paused: Boolean(row.paused ?? governance.paused ?? false),
    entriesOnly: Boolean(row.entries_only ?? row.entriesOnly ?? false),
    shadowOnly: Boolean(row.shadow_only ?? row.shadowOnly ?? false),
    riskScale: Number(row.budget_scale ?? row.risk_scale ?? row.riskScale ?? governance.risk_scale ?? 1),
    rollbackArmed,
    rollbackReason: String(
      row.rollback_reason ||
        row.rollbackReason ||
        rollbackActions.find((action: any) => Boolean(action && typeof action === "object" && action.armed))?.reason ||
        activeTriggers[0] ||
        "",
    ),
    eligibleForUpgrade: Boolean(row.eligible_for_upgrade ?? row.eligibleForUpgrade ?? false),
    canaryActive: Boolean(row.canary_active ?? row.canaryActive ?? state.canary_active ?? state.canaryActive ?? governance.canary_active ?? false),
    observationWindowActive: Boolean(
      row.observation_window_active ??
        row.observationWindowActive ??
        state.observation_window_active ??
        state.observationWindowActive ??
        row.canary_active ??
        row.canaryActive ??
        state.canary_active ??
        state.canaryActive ??
        false,
    ),
    reasons: activeTriggers,
    breachCounts:
      row.metrics && typeof row.metrics === "object"
        ? {
            rollout: Number(row.metrics.rollout_breach_count || 0),
            featureParity: Number(row.metrics.feature_parity_breaches || 0),
            staleFeatures: Number(row.metrics.stale_feature_count || 0),
          }
        : row.breach_counts && typeof row.breach_counts === "object"
          ? row.breach_counts
          : row.breaches && typeof row.breaches === "object"
            ? row.breaches
            : {},
    activeTriggers,
    details:
      row.details && typeof row.details === "object"
        ? row.details
        : {
            metrics: row.metrics && typeof row.metrics === "object" ? row.metrics : {},
            rollback_actions: rollbackActions,
            eligible_for_upgrade: Boolean(row.eligible_for_upgrade ?? row.eligibleForUpgrade ?? false),
          },
  }
}

// AGENT HOT PATH: `normalizeDecision` flattens one bridge decision into the card contract used by both home and signals views.
function normalizeDecision(
  raw: any,
  options: {
    ticksBySymbol: Map<string, any>
    positionsBySymbol: Map<string, any>
  },
) {
  const row = raw && typeof raw === "object" ? raw : {}
  const metadata = row.metadata && typeof row.metadata === "object" ? row.metadata : {}
  const orchestrationShadow =
    metadata.orchestration_shadow && typeof metadata.orchestration_shadow === "object"
      ? metadata.orchestration_shadow
      : metadata.orchestrationShadow && typeof metadata.orchestrationShadow === "object"
        ? metadata.orchestrationShadow
        : {}
  const orchestrationShadowBaseline =
    orchestrationShadow.baseline_action && typeof orchestrationShadow.baseline_action === "object"
      ? orchestrationShadow.baseline_action
      : {}
  const orchestrationShadowAction =
    orchestrationShadow.shadow_action && typeof orchestrationShadow.shadow_action === "object"
      ? orchestrationShadow.shadow_action
      : {}
  const orchestrationShadowCommittee =
    orchestrationShadow.committee && typeof orchestrationShadow.committee === "object"
      ? orchestrationShadow.committee
      : {}
  const orchestrationShadowGovernedDecision =
    orchestrationShadow.governed_decision && typeof orchestrationShadow.governed_decision === "object"
      ? orchestrationShadow.governed_decision
      : {}
  const orchestrationShadowCommandFlow =
    orchestrationShadow.command_flow && typeof orchestrationShadow.command_flow === "object"
      ? orchestrationShadow.command_flow
      : {}
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
  const overlayMetadata =
    metadata.overlay_metadata && typeof metadata.overlay_metadata === "object"
      ? metadata.overlay_metadata
      : metadata.overlayMetadata && typeof metadata.overlayMetadata === "object"
        ? metadata.overlayMetadata
        : {}
  const overlayDiagnostics =
    metadata.overlay_diagnostics && typeof metadata.overlay_diagnostics === "object"
      ? metadata.overlay_diagnostics
      : metadata.overlayDiagnostics && typeof metadata.overlayDiagnostics === "object"
        ? metadata.overlayDiagnostics
        : {}
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
    orchestration_shadow: orchestrationShadow,
    orchestrationShadow: orchestrationShadow,
    orchestration_shadow_enabled: Boolean(orchestrationShadow.enabled ?? false),
    orchestration_shadow_baseline_action: String(
      orchestrationShadowBaseline.action || orchestrationShadowBaseline.intent || "",
    ),
    orchestration_shadow_baseline_side: String(orchestrationShadowBaseline.side || ""),
    orchestration_shadow_action: String(orchestrationShadowAction.action || orchestrationShadowAction.intent || ""),
    orchestration_shadow_side: String(orchestrationShadowAction.side || ""),
    orchestration_shadow_divergence_reason: String(orchestrationShadow.divergence_reason || ""),
    orchestration_shadow_blocking_reasons: Array.isArray(orchestrationShadow.blocking_reasons)
      ? orchestrationShadow.blocking_reasons.map((item: any) => String(item))
      : [],
    orchestration_shadow_proposal_votes:
      orchestrationShadow.proposal_votes && typeof orchestrationShadow.proposal_votes === "object"
        ? orchestrationShadow.proposal_votes
        : {},
    orchestration_shadow_run_id: String(orchestrationShadow.run_id || ""),
    orchestration_shadow_trace_id: String(orchestrationShadow.trace_id || ""),
    orchestration_shadow_fault_classification: String(orchestrationShadow.fault_classification || ""),
    orchestration_shadow_latency_ms: asFiniteNumber(orchestrationShadow.latency_ms),
    orchestration_shadow_committee:
      orchestrationShadowCommittee && typeof orchestrationShadowCommittee === "object"
        ? orchestrationShadowCommittee
        : {},
    orchestration_shadow_committee_winning_agent: String(orchestrationShadowCommittee.winning_agent || ""),
    orchestration_shadow_committee_winning_proposal_id: String(
      orchestrationShadowCommittee.winning_proposal_id || "",
    ),
    orchestration_shadow_committee_winning_score: asFiniteNumber(orchestrationShadowCommittee.winning_score),
    orchestration_shadow_committee_arbiter_stage: String(orchestrationShadowCommittee.arbiter_stage || ""),
    orchestration_shadow_committee_rationale: String(orchestrationShadowCommittee.rationale || ""),
    orchestration_shadow_committee_top_ranked_proposals: Array.isArray(
      orchestrationShadowCommittee.top_ranked_proposals,
    )
      ? orchestrationShadowCommittee.top_ranked_proposals
      : [],
    orchestration_shadow_governed_action: String(orchestrationShadowGovernedDecision.selected_action || ""),
    orchestration_shadow_approval_state: String(orchestrationShadowGovernedDecision.approval_state || ""),
    orchestration_shadow_command_id: String(orchestrationShadowGovernedDecision.command_id || orchestrationShadowCommandFlow.command_id || ""),
    orchestration_shadow_command_status: String(orchestrationShadowGovernedDecision.command_status || orchestrationShadowCommandFlow.status || ""),
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
    adaptive_sleeve: String(metadata.adaptive_sleeve || metadata.adaptiveSleeve || ""),
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
    conviction_score: asFiniteNumber(row.conviction_score ?? row.convictionScore ?? metadata.conviction_score ?? metadata.convictionScore),
    conviction_band: String(row.conviction_band || row.convictionBand || metadata.conviction_band || metadata.convictionBand || ""),
    thesis_stage: String(row.thesis_stage || row.thesisStage || metadata.thesis_stage || metadata.thesisStage || ""),
    portfolio_posture: String(
      row.portfolio_posture || row.portfolioPosture || metadata.portfolio_posture || metadata.portfolioPosture || "",
    ),
    sleeve_budget_target: asFiniteNumber(
      row.sleeve_budget_target ?? row.sleeveBudgetTarget ?? metadata.sleeve_budget_target ?? metadata.sleeveBudgetTarget,
    ),
    sleeve_budget_used: asFiniteNumber(
      row.sleeve_budget_used ?? row.sleeveBudgetUsed ?? metadata.sleeve_budget_used ?? metadata.sleeveBudgetUsed,
    ),
    replacement_urgency: asFiniteNumber(
      row.replacement_urgency ?? row.replacementUrgency ?? metadata.replacement_urgency ?? metadata.replacementUrgency,
    ),
    policy_trace: normalizeOverlayPolicyTrace(
      row.policy_trace ??
        row.policyTrace ??
        metadata.policy_trace ??
        metadata.policyTrace ??
        overlayMetadata.policy_trace ??
        overlayMetadata.policyTrace ??
        [],
    ),
    overlay_metadata: overlayMetadata,
    overlay_diagnostics: overlayDiagnostics,
    allocator_score: asFiniteNumber(metadata.allocator_score ?? metadata.allocatorScore),
    allocator_rank: asFiniteNumber(metadata.allocator_rank ?? metadata.allocatorRank),
    allocator_selected: Boolean(metadata.allocator_selected ?? metadata.allocatorSelected ?? false),
    allocator_rejection_reason: String(
      metadata.allocator_rejection_reason || metadata.allocatorRejectionReason || "",
    ),
    replacement_candidate: Boolean(metadata.replacement_candidate ?? metadata.replacementCandidate ?? false),
    replacement_target_pair: String(
      metadata.replacement_target_pair || metadata.replacementTargetPair || "",
    ),
    sleeve_health_score: asFiniteNumber(metadata.sleeve_health_score ?? metadata.sleeveHealthScore),
    sleeve_health_state: String(metadata.sleeve_health_state || metadata.sleeveHealthState || ""),
    thesis_id: String(metadata.thesis_id || metadata.thesisId || ""),
    campaign_state: String(metadata.campaign_state || metadata.campaignState || ""),
    campaign_state_reason: String(metadata.campaign_state_reason || metadata.campaignStateReason || ""),
    campaign_proof_score: asFiniteNumber(metadata.campaign_proof_score ?? metadata.campaignProofScore),
    campaign_maturity_score: asFiniteNumber(
      metadata.campaign_maturity_score ?? metadata.campaignMaturityScore,
    ),
    campaign_reset_quality: asFiniteNumber(
      metadata.campaign_reset_quality ?? metadata.campaignResetQuality,
    ),
    campaign_priority_boost: asFiniteNumber(
      metadata.campaign_priority_boost ?? metadata.campaignPriorityBoost,
    ),
    campaign_reentry_blocked: Boolean(
      metadata.campaign_reentry_blocked ?? metadata.campaignReentryBlocked ?? false,
    ),
    belief_primary_side: String(metadata.belief_primary_side || metadata.beliefPrimarySide || ""),
    belief_primary_scenario: String(metadata.belief_primary_scenario || metadata.beliefPrimaryScenario || ""),
    belief_primary_thesis: String(metadata.belief_primary_thesis || metadata.beliefPrimaryThesis || ""),
    belief_primary_score: asFiniteNumber(metadata.belief_primary_score ?? metadata.beliefPrimaryScore),
    belief_primary_rank_score: asFiniteNumber(
      metadata.belief_primary_rank_score ?? metadata.beliefPrimaryRankScore,
    ),
    belief_primary_ev_above_hurdle_prob: asFiniteNumber(
      metadata.belief_primary_ev_above_hurdle_prob ?? metadata.beliefPrimaryEvAboveHurdleProb,
    ),
    belief_primary_expected_net_ev_bps: asFiniteNumber(
      metadata.belief_primary_expected_net_ev_bps ?? metadata.beliefPrimaryExpectedNetEvBps,
    ),
    belief_primary_confirm_prob: asFiniteNumber(
      metadata.belief_primary_confirm_prob ?? metadata.beliefPrimaryConfirmProb,
    ),
    belief_primary_fail_fast_prob: asFiniteNumber(
      metadata.belief_primary_fail_fast_prob ?? metadata.beliefPrimaryFailFastProb,
    ),
    belief_no_edge: Boolean(metadata.belief_no_edge ?? metadata.beliefNoEdge ?? false),
    belief_opposing_side: String(metadata.belief_opposing_side || metadata.beliefOpposingSide || ""),
    belief_opposing_scenario: String(metadata.belief_opposing_scenario || metadata.beliefOpposingScenario || ""),
    belief_opposing_thesis: String(metadata.belief_opposing_thesis || metadata.beliefOpposingThesis || ""),
    belief_opposing_score: asFiniteNumber(metadata.belief_opposing_score ?? metadata.beliefOpposingScore),
    belief_gap: asFiniteNumber(metadata.belief_gap ?? metadata.beliefGap),
    belief_fragility_score: asFiniteNumber(
      metadata.belief_fragility_score ?? metadata.beliefFragilityScore,
    ),
    belief_horizon_alignment_score: asFiniteNumber(
      metadata.belief_horizon_alignment_score ?? metadata.beliefHorizonAlignmentScore,
    ),
    belief_short_up_prob: asFiniteNumber(metadata.belief_short_up_prob ?? metadata.beliefShortUpProb),
    belief_trade_up_prob: asFiniteNumber(metadata.belief_trade_up_prob ?? metadata.beliefTradeUpProb),
    belief_structural_up_prob: asFiniteNumber(
      metadata.belief_structural_up_prob ?? metadata.beliefStructuralUpProb,
    ),
    belief_regime_fit_score: asFiniteNumber(
      metadata.belief_regime_fit_score ?? metadata.beliefRegimeFitScore,
    ),
    belief_expected_confirmation_window_bars: asFiniteNumber(
      metadata.belief_expected_confirmation_window_bars ?? metadata.beliefExpectedConfirmationWindowBars,
    ),
    belief_expected_path_shape: String(
      metadata.belief_expected_path_shape || metadata.beliefExpectedPathShape || "",
    ),
    belief_invalidation_reason: String(
      metadata.belief_invalidation_reason || metadata.beliefInvalidationReason || "",
    ),
    belief_model_version: String(metadata.belief_model_version || metadata.beliefModelVersion || ""),
    belief_source_mode: String(metadata.belief_source_mode || metadata.beliefSourceMode || ""),
    regime_prob: asFiniteNumber(metadata.regime_prob ?? metadata.regimeProb),
    swing_prob: asFiniteNumber(metadata.swing_prob ?? metadata.swingProb),
    entry_prob: asFiniteNumber(metadata.entry_prob ?? metadata.entryProb),
    trade_prob: asFiniteNumber(metadata.trade_prob ?? metadata.tradeProb),
  }
}

// AGENT FLOW: Route handler fetches bridge truth first, then assembles one normalized payload for the polling hook and all dashboard consumers.
export async function GET() {
  let bridgeUrl = BRIDGE_URL
  try {
    const stateResult = await fetchBridgeObjectWithSource(["/v2/state"], "state payload")
    const raw = stateResult.payload
    bridgeUrl = stateResult.baseUrl
    const pinnedBase = [bridgeUrl]
    const ticksRaw = await fetchBridgeJson(["/v2/market/ticks"], pinnedBase).catch(() => null)
    const monitorEmbedded = raw?.monitor && typeof raw.monitor === "object"
    const monitor = monitorEmbedded ? null : await fetchBridgeJson(["/v2/monitor"], pinnedBase).catch(() => null)
    const governanceRaw = await fetchBridgeJson(["/v2/governance/events?limit=50"], pinnedBase).catch(() => null)

    const heartbeatStaleAfterSecs = Math.max(1, asFiniteNumber(raw?.heartbeat_stale_after_secs) || 30)
    const lastHeartbeat = raw?.last_heartbeat || raw?.lastHeartbeat || null
    const heartbeatAgeFromState = normalizeAgeSecs(raw?.heartbeat_age_secs ?? raw?.heartbeatAgeSecs)
    const heartbeatAgeFromTs = ageSecsFromTimestamp(lastHeartbeat)
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
    const runtimeLastProgressAgeSecs = normalizeAgeSecs(
      raw?.runtime_last_progress_age_secs ??
        raw?.runtimeLastProgressAgeSecs ??
        raw?.runtime_startup?.last_progress_age_secs,
    )
    const runtimeFailureReason = String(
      raw?.runtime_failure_reason || raw?.runtimeFailureReason || raw?.runtime_startup?.failure_reason || "",
    ).trim()
    const runtimeBootId = String(raw?.runtime_boot_id || raw?.runtimeBootId || raw?.runtime_startup?.boot_id || "").trim()
    const runtimeCycleAgeSecs = normalizeAgeSecs(raw?.runtime_cycle_age_secs ?? raw?.runtimeCycleAgeSecs)
    const runtimeCycleStaleAfterSecs = Math.max(1, asFiniteNumber(raw?.runtime_cycle_stale_after_secs) || 30)
    const runtimeStartup = normalizeRuntimeStartupSummary(raw, runtimeStatus)
    const runtimeSignalFresh =
      typeof raw?.runtime_signal_fresh === "boolean"
        ? Boolean(raw.runtime_signal_fresh)
        : runtimeStatus === "running" &&
          runtimeCycleAgeSecs !== null &&
          runtimeCycleAgeSecs <= runtimeCycleStaleAfterSecs
    const databaseOkRaw = raw.database_ok ?? raw.databaseOk
    const databaseOk = databaseOkRaw === true
    const databaseStatus = String(raw.database_status || raw.databaseStatus || (databaseOk ? "up" : "unhealthy"))
    const signalDataFresh = mt4Fresh && ticksFresh && runtimeSignalFresh
    const isStale = !databaseOk || !mt4Fresh || !ticksFresh || !runtimeSignalFresh
    const bridgeState = "bridge_up"
    const statusTier = normalizeBridgeStatusTier(raw?.status_tier || raw?.statusTier, {
      databaseOk,
      mt4Fresh,
      ticksFresh,
      runtimeSignalFresh,
      runtimeStatus,
    })

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
    const runtimeStartupFailures = governanceEvents
      .map((event: any) => normalizeRuntimeStartupFailure(event))
      .filter((event: ReturnType<typeof normalizeRuntimeStartupFailure>) => Boolean(event))
    const lastRuntimeStartupFailure = shouldSuppressRuntimeStartupFailure(runtimeStartup, runtimeStatus)
      ? null
      : runtimeStartupFailures.find((event: ReturnType<typeof normalizeRuntimeStartupFailure>) => Boolean(event)) ?? null
    const runtimeStartupFailureHistory = runtimeStartupFailures
    const startupInferenceByPair = normalizeObjectMap(
      raw?.startup_inference_by_pair ||
        raw?.startupInferenceByPair ||
        raw?.runtime_diag?.startup_inference_by_pair ||
        raw?.runtime_diag?.startupInferenceByPair ||
        raw?.runtime_diag?.startup_inference ||
        raw?.runtime_diag?.startupInference,
    )
    const featureServingByPair = normalizeObjectMap(
      raw?.feature_serving_by_pair ||
        raw?.featureServingByPair ||
        raw?.runtime_diag?.feature_serving_by_pair ||
        raw?.runtime_diag?.featureServingByPair,
    )
    const pairReadiness = normalizeObjectMap(
      raw?.pair_readiness || raw?.pairReadiness || raw?.runtime_diag?.pair_readiness || raw?.runtime_diag?.pairReadiness,
    )
    const strategyEngineMode = String(
      raw?.strategy_engine_mode || raw?.strategyEngineMode || raw?.runtime_diag?.strategy_engine_mode || raw?.runtime_diag?.strategyEngineMode || "supervised_legacy",
    )
    const supervisedFallback = normalizeAnyObject(
      raw?.supervised_fallback || raw?.supervisedFallback || raw?.runtime_diag?.supervised_fallback || raw?.runtime_diag?.supervisedFallback,
    )
    const challengerConflict = normalizeAnyObject(
      raw?.challenger_conflict || raw?.challengerConflict || raw?.runtime_diag?.challenger_conflict || raw?.runtime_diag?.challengerConflict,
    )
    const rlPortfolioProposal = normalizeRlPortfolioProposal(
      raw?.rl_portfolio_proposal ||
        raw?.rlPortfolioProposal ||
        raw?.runtime_diag?.rl_portfolio_proposal ||
        raw?.runtime_diag?.rlPortfolioProposal,
    )
    const rlExecutionPolicy = normalizeRlExecutionPolicy(
      raw?.rl_execution_policy ||
        raw?.rlExecutionPolicy ||
        raw?.entry_execution_policy ||
        raw?.entryExecutionPolicy ||
        raw?.runtime_diag?.entry_execution_policy ||
        raw?.runtime_diag?.entryExecutionPolicy ||
        raw?.runtime_diag?.rl_execution_policy ||
        raw?.runtime_diag?.rlExecutionPolicy,
    )
    const rlLifecycleSummary = normalizeAnyObject(
      raw?.rl_lifecycle_summary ||
        raw?.rlLifecycleSummary ||
        raw?.runtime_diag?.rl_lifecycle_summary ||
        raw?.runtime_diag?.rlLifecycleSummary,
    )
    const rlRebalanceSummary = normalizeAnyObject(
      raw?.rl_rebalance_summary ||
        raw?.rlRebalanceSummary ||
        rlLifecycleSummary.rebalance_summary ||
        rlLifecycleSummary.rebalanceSummary,
    )
    const rlFlipIntent = normalizeAnyObject(
      raw?.rl_flip_intent ||
        raw?.rlFlipIntent ||
        rlLifecycleSummary.flip_intent ||
        rlLifecycleSummary.flipIntent,
    )
    const rlArtifactReadiness = normalizeAnyObject(
      raw?.rl_artifact_readiness ||
        raw?.rlArtifactReadiness ||
        rlLifecycleSummary.artifact_readiness ||
        rlLifecycleSummary.artifactReadiness,
    )

    const overlayCycleSummary = normalizeOverlayCycleSummary(
      raw?.runtime_diag?.overlay_cycle_summary ||
        raw?.runtime_diag?.desk_overlay_cycle_summary ||
        raw?.runtime_diag?.portfolio_overlay_cycle_summary,
    )
    const featureServing = normalizeFeatureServing(raw)
    const featureObservability = normalizeFeatureObservability(raw, featureServing)
    const shadowOrchestrator = normalizeOrchestrationShadow(
      raw?.orchestration_shadow || raw?.runtime_diag?.orchestration_shadow,
    )
    const paperExecution = normalizePaperExecution(raw?.paper_execution || raw?.paperExecution)
    const orchestrationLive = normalizeOrchestrationLive(raw?.orchestration_live || raw?.orchestrationLive)
    const orchestrationLiveHealth = normalizeOrchestrationLiveHealth(
      raw?.orchestration_live_health || raw?.orchestrationLiveHealth || orchestrationLive,
    )
    const orchestrationEvidence = normalizeOrchestrationEvidence(raw?.orchestration_evidence || raw?.orchestrationEvidence)
    const entryExecutionPolicy = normalizeEntryExecutionPolicy(
      raw?.entry_execution_policy ||
        raw?.entryExecutionPolicy ||
        raw?.runtime_diag?.entry_execution_policy ||
        raw?.runtime_diag?.entryExecutionPolicy,
    )
    const tradeFlowSummary = normalizeTradeFlowSummary(
      raw,
      entryExecutionPolicy,
      normalizeShadowPolicy(raw?.runtime_diag?.shadow_policy),
      normalizeAdaptiveShadowPolicy(raw?.runtime_diag?.adaptive_shadow_policy),
      shadowOrchestrator,
      orchestrationLive,
      featureObservability,
      normalizeCapitalGovernance(
        raw?.capital_governance || raw?.runtime_diag?.capital_governance,
        raw?.governance,
        raw,
      ),
    )
    const canaryPairs = normalizeStringList(
      raw?.canary_pairs ??
        raw?.canaryPairs ??
        raw?.rollout_runtime?.active_pairs ??
        raw?.rolloutRuntime?.activePairs ??
        raw?.rollout_policy?.active_pairs ??
        raw?.rolloutPolicy?.activePairs,
    )

    const data = {
      isRunning: isLiveStateRunning({ databaseOk, mt4Connected, mt4Fresh, ticksFresh, runtimeSignalFresh }),
      bridgeUrl,
      bridgePrimaryUrl: BRIDGE_URL,
      bridgeState,
      statusTier,
      databaseOk,
      databaseStatus,
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
      runtimeStartup,
      runtimeStartupSummary: runtimeStartup,
      runtime_startup_summary: runtimeStartup,
      runtimeStartupStatus: runtimeStartup.status,
      runtimeStartupWarningCount: runtimeStartup.warningCount,
      modelLoadErrors: runtimeStartup.modelLoadErrors,
      modelLoadTimeouts: runtimeStartup.modelLoadTimeouts,
      startupInferenceFailures: runtimeStartup.startupInferenceFailures,
      startupDisabledPairs: runtimeStartup.startupDisabledPairs,
      lastRuntimeStartupFailure,
      runtimeStartupFailureHistory,
      startupInferenceByPair,
      featureServingByPair,
      featureObservability,
      pairReadiness,
      strategyEngineMode,
      executionMode: entryExecutionPolicy.executionMode,
      supervisedFallback,
      challengerConflict,
      rlPortfolioProposal,
      rlExecutionPolicy,
      rlLifecycleSummary,
      rlRebalanceSummary,
      rlFlipIntent,
      rlArtifactReadiness,
      rlCheckpointLoaded: Boolean(rlExecutionPolicy.checkpointLoaded ?? rlPortfolioProposal.checkpointLoaded ?? false),
      rlCheckpointPath: String(rlExecutionPolicy.checkpointPath || rlPortfolioProposal.checkpointPath || ""),
      rlProposalSource: String(rlExecutionPolicy.proposalSource || rlPortfolioProposal.proposalSource || rlPortfolioProposal.source || ""),
      rlSupervisedFallbackUsed: Boolean(rlExecutionPolicy.supervisedFallbackUsed ?? rlPortfolioProposal.supervisedFallbackUsed ?? false),
      rlFallbackReason: String(rlExecutionPolicy.fallbackReason || rlPortfolioProposal.fallbackReason || ""),
      rlRoutedEntryCount: Number(rlExecutionPolicy.routedEntryCount ?? rlPortfolioProposal.routedEntryCount ?? 0),
      rlBlockedEntryCount: Number(rlExecutionPolicy.blockedEntryCount ?? rlPortfolioProposal.blockedEntryCount ?? 0),
      rlFallbackEntryCount: Number(rlExecutionPolicy.fallbackEntryCount ?? rlPortfolioProposal.fallbackEntryCount ?? 0),
      rlScaledEntryCount: Number(rlExecutionPolicy.scaledEntryCount ?? rlPortfolioProposal.scaledEntryCount ?? 0),
      tradeFlowSummary,
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
      canaryPairs,
      readyEntriesCount,
      queuedEntriesCount,
      suppressedEntriesCount,
      tickStatus: String(raw?.tick_status || "unknown"),
      tickReason: String(raw?.tick_reason || "unknown"),
      tickSymbolsCount: Number(raw?.tick_symbols_count || 0),
      tickMaxAgeSecs: normalizeAgeSecs(raw?.tick_max_age_secs),
      signalDataReason:
        runtimeStatus === "failed"
          ? "runtime_startup_failed"
          : runtimeStatus === "stalled"
            ? "runtime_startup_stalled"
            : runtimeStatus === "starting"
              ? "runtime_starting"
              : runtimeStartup.status === "recovered_with_warnings"
                ? "runtime_recovered_with_warnings"
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
      shadowOrchestrator,
      paperExecution,
      orchestrationLive,
      orchestrationLiveHealth,
      orchestrationEvidence,
      orchestration_evidence: orchestrationEvidence,
      allocatorPolicy: normalizeAllocatorPolicy(raw?.runtime_diag?.allocator_policy),
      allocatorCycleSummary: normalizeAllocatorPolicy(raw?.runtime_diag?.allocator_cycle_summary),
      campaignPolicy: normalizeCampaignPolicy(raw?.runtime_diag?.campaign_policy),
      campaignCycleSummary: normalizeCampaignCycleSummary(raw?.runtime_diag?.campaign_cycle_summary),
      campaignMetricsBySleeve:
        raw?.runtime_diag?.campaign_metrics_by_sleeve && typeof raw?.runtime_diag?.campaign_metrics_by_sleeve === "object"
          ? raw.runtime_diag.campaign_metrics_by_sleeve
          : {},
      campaignStateCounts:
        raw?.runtime_diag?.campaign_state_counts && typeof raw?.runtime_diag?.campaign_state_counts === "object"
          ? raw.runtime_diag.campaign_state_counts
          : {},
      directionalBeliefPolicy: normalizeDirectionalBeliefPolicy(raw?.runtime_diag?.directional_belief_policy),
      directionalBeliefCycleSummary: normalizeDirectionalBeliefCycleSummary(
        raw?.runtime_diag?.directional_belief_cycle_summary,
      ),
      directionalBeliefMetrics: normalizeDirectionalBeliefMetrics(raw?.runtime_diag?.directional_belief_metrics),
      overlayCycleSummary,
      featureServing,
      featureServingSource: featureServing.source,
      featureServingReason: featureServing.reason,
      providerRoles: raw?.provider_roles || raw?.runtime_diag?.provider_roles || {},
      providerHealth: normalizeProviderHealth(
        raw?.provider_health || raw?.runtime_diag?.provider_health || raw?.runtime_diag?.provider_telemetry,
      ),
      provider_health: raw?.provider_health || raw?.runtime_diag?.provider_health || raw?.runtime_diag?.provider_telemetry || {},
      portfolioTelemetry: normalizePortfolioTelemetry(
        raw?.portfolio_telemetry || raw?.runtime_diag?.portfolio_telemetry || raw?.runtime_diag?.portfolio_intelligence,
        overlayCycleSummary,
      ),
      portfolio_intelligence:
        raw?.portfolio_intelligence || raw?.runtime_diag?.portfolio_intelligence || raw?.runtime_diag?.portfolio_telemetry || {},
      capitalGovernance: normalizeCapitalGovernance(
        raw?.capital_governance || raw?.runtime_diag?.capital_governance,
        raw?.governance,
        raw,
      ),
      capital_governance: raw?.capital_governance || raw?.runtime_diag?.capital_governance || {},
      featureOnlineReady: featureObservability.featureOnlineReady,
      featureDataFresh: featureObservability.featureDataFresh,
      featurePushBacklog: featureObservability.featurePushBacklog,
      featurePushBacklogWarn: featureObservability.featurePushBacklogWarn,
      featurePushBacklogOk: featureObservability.featurePushBacklogOk,
      featurePushBacklogOverage: featureObservability.featurePushBacklogOverage,
      featureBlockerReason: featureObservability.featureBlockerReason,
      featureBlockerReasons: featureObservability.featureBlockerReasons,
      featureBlockerSource: featureObservability.featureBlockerSource,
      featureBarStatus: featureObservability.featureBarStatus,
      featureParityOk: Boolean(raw?.feature_parity_ok ?? true),
      pendingEntryCount: entryExecutionPolicy.pendingEntryCount,
      approvedEntryCount: entryExecutionPolicy.approvedEntryCount,
      blockedEntryCount: entryExecutionPolicy.blockedEntryCount,
      submittedEntryCount: entryExecutionPolicy.submittedEntryCount,
      duplicateEntryCount: entryExecutionPolicy.duplicateEntryCount,
      dominantEntryRejectionReason: entryExecutionPolicy.dominantRejectionReason,
      entryRejectionReasonCounts: entryExecutionPolicy.rejectionReasonCounts,
      entryRejectionReasonSummary: entryExecutionPolicy.rejectionReasonSummary,
      sleeveMetrics:
        raw?.runtime_diag?.sleeve_metrics && typeof raw?.runtime_diag?.sleeve_metrics === "object"
          ? raw.runtime_diag.sleeve_metrics
          : {},
      entryExecutionPolicy,
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
          bridgeUrl,
          bridgePrimaryUrl: BRIDGE_URL,
          bridgeState: "bridge_down",
          statusTier: "bridge_down",
          databaseOk: false,
          databaseStatus: "unavailable",
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
          runtimeStartupSummary: {
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
          runtime_startup_summary: {
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
          orchestrationLiveHealth: {
            status: "healthy",
            reason: "ok",
            reasons: [],
            warningCount: 0,
            blockingCount: 0,
            runtimeStatus: "",
            runtimeReady: false,
            statusTier: "",
            runtimeEnabled: true,
            queueKillActive: false,
            pendingCommandCount: 0,
            orphanCommandCount: 0,
            ackSuccessRate: 0,
            ackTimeoutRate: 0,
            ackTimeoutSpike: false,
            repeatedGraphFaultCount: 0,
            tracePersistenceFailureCount: 0,
            baselineFallbackCount: 0,
          },
          tradeFlowSummary: {
            signalsSent: 0,
            tradesExecuted: 0,
            readyEntriesCount: 0,
            queuedEntriesCount: 0,
            suppressedEntriesCount: 0,
            pendingEntryCount: 0,
            approvedEntryCount: 0,
            blockedEntryCount: 0,
            submittedEntryCount: 0,
            ackSuccessRate: null,
            ackTimeoutRate: null,
            lastAck: null,
            lastAckStatus: "",
            canaryActive: false,
            canaryStagePct: 0,
            canaryRuntimeEnabled: true,
            canaryQueueKillActive: false,
            divergenceCounts: {
              shadowLiveOnly: 0,
              shadowShadowOnly: 0,
              adaptiveLiveOnly: 0,
              adaptiveAdaptiveOnly: 0,
              orchestratorFaultCount: 0,
            },
            canaryHealth: {
              runtimeStatus: "",
              runtimeReady: false,
              featureOnlineReady: false,
              featureDataFresh: false,
              featurePushBacklog: 0,
              featurePushBacklogOk: true,
              featureBlockerReason: "",
              featureBlockerReasons: [],
              featureBlockerSource: "",
              featureBarStatus: "missing",
              featureServingSource: "",
              featureServingReason: "",
              orchestrationLiveHealth: {
                status: "healthy",
                reason: "ok",
                reasons: [],
                warningCount: 0,
                blockingCount: 0,
                runtimeStatus: "",
                runtimeReady: false,
                statusTier: "",
                runtimeEnabled: true,
                queueKillActive: false,
                pendingCommandCount: 0,
                orphanCommandCount: 0,
                ackSuccessRate: 0,
                ackTimeoutRate: 0,
                ackTimeoutSpike: false,
                repeatedGraphFaultCount: 0,
                tracePersistenceFailureCount: 0,
                baselineFallbackCount: 0,
              },
            },
          },
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
          allocatorPolicy: {
            candidateCount: 0,
            selectedCount: 0,
            rankedOutCount: 0,
            replacementCandidateCount: 0,
            replacementExitCount: 0,
            sleeveCandidateCounts: {},
            sleeveSelectedCounts: {},
          },
          allocatorCycleSummary: {
            candidateCount: 0,
            selectedCount: 0,
            rankedOutCount: 0,
            replacementCandidateCount: 0,
            replacementExitCount: 0,
            sleeveCandidateCounts: {},
            sleeveSelectedCounts: {},
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
          featureServing: {
            source: "",
            sourceChain: ["feast_online", "parquet_fallback", "raw_contract_fallback"],
            featureService: "",
            cacheHit: false,
            freshnessSecs: null,
            stale: false,
            reason: "",
            backlog: 0,
            byPair: {},
            details: {},
          },
          featureObservability: {
            featureOnlineReady: false,
            featureDataFresh: false,
            featurePushBacklog: 0,
            featurePushBacklogWarn: 0,
            featurePushBacklogOk: true,
            featurePushBacklogOverage: 0,
            featureBlockerReason: "",
            featureBlockerReasons: [],
            featureBlockerSource: "",
            featureBarStatus: "missing",
            featureServingSource: "",
            featureServingReason: "",
          },
          featureOnlineReady: false,
          featureDataFresh: false,
          featureServingSource: "",
          featureServingReason: "",
          featurePushBacklog: 0,
          featurePushBacklogWarn: 0,
          featurePushBacklogOk: true,
          featurePushBacklogOverage: 0,
          featureBlockerReason: "",
          featureBlockerReasons: [],
          featureBlockerSource: "",
          featureBarStatus: "missing",
          providerRoles: {},
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
          provider_health: {},
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
          portfolio_intelligence: {},
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
          capital_governance: {},
          sleeveMetrics: {},
          entryExecutionPolicy: {
            executionMode: "",
            adaptiveExecutionEnabled: false,
            pendingEntryCount: 0,
            approvedEntryCount: 0,
            blockedEntryCount: 0,
            submittedEntryCount: 0,
            duplicateEntryCount: 0,
          },
          executionMode: "",
          pendingEntryCount: 0,
          approvedEntryCount: 0,
          blockedEntryCount: 0,
          submittedEntryCount: 0,
          duplicateEntryCount: 0,
          dominantEntryRejectionReason: "",
          entryRejectionReasonCounts: {},
          entryRejectionReasonSummary: {},
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
      { status: 200 },
    )
  }
}
