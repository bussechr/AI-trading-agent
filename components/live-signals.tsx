// AGENT: ROLE: Render live decision tickets for the signals tab and compact open-position cards for the home dashboard.
// AGENT: ENTRYPOINT: exported `LiveSignals` and `OpenPositionsSignals` React components.
// AGENT: PRIMARY INPUTS: typed bridge state from `useLiveBridgeState`.
// AGENT: PRIMARY OUTPUTS: live candidate cards, open-position cards, and execution/adaptive diagnostics in UI form.
// AGENT: DEPENDS ON: `lib/hooks/use-live-bridge-state.ts`, shared UI components.
// AGENT: CALLED BY: `components/dashboard-home.tsx`, `app/signals/page.tsx`.
// AGENT: STATE / SIDE EFFECTS: render only.
// AGENT: HANDSHAKES: dashboard route decision contract, shadow/adaptive policy display contract.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `lib/hooks/use-live-bridge-state.ts` -> `components/dashboard-home.tsx`
"use client"

import { ArrowDownRight, ArrowUpRight, Minus, ShieldCheck } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { bridgeStatusClasses, bridgeStatusLabel } from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

function formatNumber(value: unknown, digits = 2): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? numeric.toFixed(digits) : "—"
}

function formatPercent(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? `${(numeric * 100).toFixed(2)}%` : "—"
}

function formatBps(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  return Number.isFinite(numeric) ? `${numeric.toFixed(2)} bps` : "—"
}

function formatSignedCurrency(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return "—"
  const sign = numeric > 0 ? "+" : ""
  return `${sign}$${numeric.toFixed(2)}`
}

function formatReasonList(values: unknown): string {
  if (!Array.isArray(values) || values.length === 0) return "none"
  return values.map((value) => String(value || "").trim()).filter(Boolean).join(", ")
}

function humanizeToken(value: unknown): string {
  const txt = String(value || "").trim()
  if (!txt) return "none"
  return txt.replaceAll("_", " ")
}

function formatShadowDivergence(value: unknown): string {
  const txt = String(value || "").trim().toLowerCase()
  if (!txt) return "—"
  if (txt === "agree_ready") return "agrees with live"
  if (txt === "agree_blocked") return "agrees on block"
  if (txt === "live_only") return "live-only approval"
  if (txt === "shadow_only") return "shadow-only approval"
  if (txt === "adaptive_only") return "adaptive-only approval"
  if (txt === "open_position") return "open position"
  return humanizeToken(txt)
}

function formatProposalVotes(value: unknown): string {
  const votes = value && typeof value === "object" ? value : {}
  const byIntent = (votes as any).by_intent && typeof (votes as any).by_intent === "object" ? (votes as any).by_intent : {}
  const parts = Object.entries(byIntent)
    .map(([intent, count]) => `${humanizeToken(intent)} ${Number(count || 0)}`)
    .filter((part) => !part.endsWith(" 0"))
  return parts.length > 0 ? parts.join(" • ") : "no proposal votes"
}

function isOppositeSide(signal: { side?: string; position_side?: string }): boolean {
  const signalSide = String(signal.side || "").trim().toUpperCase()
  const positionSide = String(signal.position_side || "").trim().toUpperCase()
  if (!signalSide || !positionSide) return false
  return (signalSide === "BUY" && positionSide === "SELL") || (signalSide === "SELL" && positionSide === "BUY")
}

// AGENT FLOW: Gate description collapses runtime execution, queue state, and lifecycle state into the primary trader-facing verdict.
function describeGate(signal: {
  position_open?: boolean
  execution_ready?: boolean
  enqueue_status?: string
  enqueue_action?: string
  lifecycle_action?: string
  lifecycle_reason?: string
  reversal_context_active?: boolean
  reversal_ready?: boolean
  reversal_blocking_reasons?: string[]
  entry_blocking_reasons?: string[]
  side?: string
  position_side?: string
}) {
  const enqueueStatus = String(signal.enqueue_status || "").trim().toLowerCase()
  const action = String(signal.enqueue_action || "").trim().toLowerCase()
  const lifecycleAction = String(signal.lifecycle_action || "").trim().toLowerCase()
  const lifecycleReason = String(signal.lifecycle_reason || "").trim()
  const opposite = Boolean(signal.reversal_context_active ?? isOppositeSide(signal))
  if (signal.position_open) {
    if (opposite) {
      if (signal.reversal_ready) {
        return { label: "open", tone: "text-emerald-600", detail: "reversal allowed" }
      }
      return {
        label: "open",
        tone: "text-sky-600",
        detail: `reversal blocked: ${formatReasonList(signal.reversal_blocking_reasons)}`,
      }
    }
    if (lifecycleAction || lifecycleReason) {
      return {
        label: "open",
        tone: "text-sky-600",
        detail: lifecycleReason || lifecycleAction || "position live",
      }
    }
    return { label: "open", tone: "text-sky-600", detail: "position live" }
  }
  if (enqueueStatus === "queued") {
    return { label: "queued", tone: "text-sky-600", detail: action || "entry queued" }
  }
  if (enqueueStatus === "duplicate_action_skip") {
    return { label: "suppressed", tone: "text-slate-500", detail: "duplicate entry skipped" }
  }
  if (signal.execution_ready) {
    return { label: "ready", tone: "text-emerald-600", detail: "entry allowed" }
  }
  return {
    label: "blocked",
    tone: "text-amber-600",
    detail: formatReasonList(signal.entry_blocking_reasons) || action || "entry blocked",
  }
}

function signalTone(side: string): {
  accent: string
  halo: string
  iconWrap: string
  icon: string
  sidePill: string
  sideLabel: string
  Icon: typeof ArrowUpRight
} {
  if (side === "BUY") {
    return {
      accent: "bg-emerald-500",
      halo: "from-emerald-500/18 via-emerald-400/10 to-transparent",
      iconWrap: "border-emerald-200/80 bg-emerald-50 text-emerald-700",
      icon: "text-emerald-600",
      sidePill: "border-emerald-300/70 bg-emerald-50 text-emerald-700",
      sideLabel: "trend-up bias",
      Icon: ArrowUpRight,
    }
  }
  if (side === "SELL") {
    return {
      accent: "bg-rose-500",
      halo: "from-rose-500/18 via-rose-400/10 to-transparent",
      iconWrap: "border-rose-200/80 bg-rose-50 text-rose-700",
      icon: "text-rose-600",
      sidePill: "border-rose-300/70 bg-rose-50 text-rose-700",
      sideLabel: "trend-down bias",
      Icon: ArrowDownRight,
    }
  }
  return {
    accent: "bg-slate-400",
    halo: "from-slate-400/18 via-slate-300/10 to-transparent",
    iconWrap: "border-slate-200/80 bg-slate-100 text-slate-700",
    icon: "text-slate-500",
    sidePill: "border-slate-300/70 bg-slate-100 text-slate-700",
    sideLabel: "neutral bias",
    Icon: Minus,
  }
}

function gateBadgeClasses(label: string): string {
  const txt = String(label || "").trim().toLowerCase()
  if (txt === "ready") return "border-emerald-300/70 bg-emerald-50 text-emerald-700"
  if (txt === "open" || txt === "queued") return "border-sky-300/70 bg-sky-50 text-sky-700"
  if (txt === "suppressed") return "border-slate-300/70 bg-slate-100 text-slate-600"
  return "border-amber-300/70 bg-amber-50 text-amber-700"
}

function verdictBadgeClasses(kind: "live" | "shadow" | "adaptive" | "orchestrator", state: string): string {
  const txt = String(state || "").trim().toLowerCase()
  if (txt.includes("trade") || txt === "ready" || txt === "open" || txt === "queued") {
    return kind === "adaptive"
      ? "border-cyan-300/70 bg-cyan-50 text-cyan-700"
      : kind === "orchestrator"
        ? "border-indigo-300/70 bg-indigo-50 text-indigo-700"
      : kind === "shadow"
        ? "border-blue-300/70 bg-blue-50 text-blue-700"
        : "border-emerald-300/70 bg-emerald-50 text-emerald-700"
  }
  if (txt === "blocked" || txt.includes("block")) {
    return kind === "adaptive"
      ? "border-amber-300/70 bg-amber-50 text-amber-700"
      : kind === "orchestrator"
        ? "border-rose-300/70 bg-rose-50 text-rose-700"
      : "border-slate-300/70 bg-slate-100 text-slate-700"
  }
  return "border-slate-300/70 bg-slate-100 text-slate-700"
}

function MetricCell({
  label,
  value,
  detail,
}: {
  label: string
  value: string
  detail?: string
}) {
  return (
    <div className="rounded-[22px] border border-slate-200/80 bg-white/80 px-4 py-3 shadow-[inset_0_1px_0_rgba(255,255,255,0.65)]">
      <div className="text-[10px] uppercase tracking-[0.26em] text-slate-500">{label}</div>
      <div className="mt-2 font-mono text-[1.1rem] font-semibold tracking-[-0.04em] text-slate-900">{value}</div>
      {detail ? <div className="mt-1 text-[11px] text-slate-500">{detail}</div> : null}
    </div>
  )
}

function DecisionRow({
  label,
  state,
  detail,
  meta,
  kind,
}: {
  label: string
  state: string
  detail: string
  meta?: string
  kind: "live" | "shadow" | "adaptive" | "orchestrator"
}) {
  return (
    <div className="rounded-[22px] border border-slate-200/80 bg-slate-50/80 px-4 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">{label}</span>
        <span
          className={cn(
            "inline-flex items-center rounded-full border px-2.5 py-1 text-[11px] font-semibold uppercase tracking-[0.18em]",
            verdictBadgeClasses(kind, state),
          )}
        >
          {state}
        </span>
        {meta ? <span className="text-xs text-slate-500">{meta}</span> : null}
      </div>
      <div className="mt-2 text-sm leading-relaxed text-slate-700">{detail}</div>
    </div>
  )
}

function StatChip({
  label,
  value,
  active = false,
}: {
  label: string
  value: string
  active?: boolean
}) {
  return (
    <div
      className={cn(
        "rounded-2xl border px-3 py-2",
        active ? "border-cyan-300/80 bg-cyan-50/80" : "border-slate-200/80 bg-white/75",
      )}
    >
      <div className="text-[10px] uppercase tracking-[0.22em] text-slate-500">{label}</div>
      <div className="mt-1 font-mono text-sm font-semibold text-slate-900">{value}</div>
    </div>
  )
}

export function LiveSignals() {
  const { state, loading } = useLiveBridgeState(3000)
  const decisions = state?.agentDecisions
  const signals = Array.isArray(decisions)
    ? [...decisions].sort((a, b) => Number(Boolean(b.position_open)) - Number(Boolean(a.position_open)))
    : []
  const live = Boolean(state?.signalDataFresh && state?.statusTier === "bridge_up_mt4_live")

  return renderLiveSignals({
    loading,
    live,
    signals,
    tradeFlowSummary: (state as any)?.tradeFlowSummary || null,
    emptyTitle: "No active signals in the current evaluation cycle.",
    title: "Live Signals",
    heading: "MT4-fed candidate stream",
    openOnly: false,
    compact: false,
    stateReason: String(state?.signalDataReason || state?.tickReason || "waiting"),
    statusTier: state?.statusTier,
    overlayCycleSummary: state?.overlayCycleSummary,
  })
}

export function OpenPositionsSignals() {
  const { state, loading } = useLiveBridgeState(3000)
  const decisions = state?.agentDecisions
  const signals = Array.isArray(decisions)
    ? [...decisions]
        .filter((decision) => Boolean(decision.position_open))
        .sort((a, b) => Number(b.position_profit || 0) - Number(a.position_profit || 0))
    : []
  const live = Boolean(state?.signalDataFresh && state?.statusTier === "bridge_up_mt4_live")

  return renderLiveSignals({
    loading,
    live,
    signals,
    tradeFlowSummary: (state as any)?.tradeFlowSummary || null,
    emptyTitle: "No open positions right now.",
    title: "Open Positions",
    heading: "Compact execution view for the live book",
    openOnly: true,
    compact: true,
    stateReason: String(state?.signalDataReason || state?.tickReason || "waiting"),
    statusTier: state?.statusTier,
    overlayCycleSummary: state?.overlayCycleSummary,
  })
}

// AGENT HOT PATH: Shared renderer keeps the home compact cards and the full signals page on one decision contract.
function renderLiveSignals({
  loading,
  live,
  signals,
  tradeFlowSummary,
  emptyTitle,
  title,
  heading,
  openOnly,
  compact,
  stateReason,
  statusTier,
  overlayCycleSummary,
}: {
  loading: boolean
  live: boolean
  signals: any[]
  tradeFlowSummary?: {
    signalsSent?: number
    approvedEntryCount?: number
    submittedEntryCount?: number
    blockedEntryCount?: number
    ackSuccessRate?: number | null
    canaryActive?: boolean
    canaryHealth?: {
      featureOnlineReady?: boolean
      featureDataFresh?: boolean
      featureBlockerReason?: string
    }
    divergenceCounts?: Record<string, number>
  } | null
  emptyTitle: string
  title: string
  heading: string
  openOnly: boolean
  compact: boolean
  stateReason: string
  statusTier?: string
  overlayCycleSummary?: {
    convictionScoreAvg: number | null
    convictionBandCounts: Record<string, number>
    thesisStageCounts: Record<string, number>
    postureCounts: Record<string, number>
  }
}) {
  const compactGrid = compact ? "grid gap-4 lg:grid-cols-2" : "space-y-4"

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border/70 px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">{title}</div>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">{heading}</h3>
          </div>
          <Badge variant="outline" className={cn("rounded-full px-3 py-1 text-xs", bridgeStatusClasses(statusTier))}>
            {loading ? "Loading" : bridgeStatusLabel(statusTier)}
          </Badge>
        </div>
        {overlayCycleSummary ? (
          <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-600">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              overlay conviction {formatNumber(overlayCycleSummary.convictionScoreAvg, 2)}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              bands {Object.keys(overlayCycleSummary.convictionBandCounts || {}).length}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              theses {Object.keys(overlayCycleSummary.thesisStageCounts || {}).length}
            </span>
          </div>
        ) : null}
        {tradeFlowSummary ? (
          <div className="mt-3 flex flex-wrap gap-2 text-[11px] text-slate-600">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              sent {Number(tradeFlowSummary.signalsSent || 0)}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              approved {Number(tradeFlowSummary.approvedEntryCount || 0)}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              submitted {Number(tradeFlowSummary.submittedEntryCount || 0)}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              ack {(Number(tradeFlowSummary.ackSuccessRate || 0) * 100).toFixed(1)}%
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              canary {tradeFlowSummary.canaryActive ? "active" : "idle"}
            </span>
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1">
              divergence {Number(tradeFlowSummary.divergenceCounts?.shadowLiveOnly || 0) + Number(tradeFlowSummary.divergenceCounts?.adaptiveLiveOnly || 0) + Number(tradeFlowSummary.divergenceCounts?.orchestratorFaultCount || 0)}
            </span>
          </div>
        ) : null}
      </div>

      <div className="px-6 py-6">
        {loading ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            Loading live bridge snapshot…
          </div>
        ) : !live ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center">
            <div className="text-lg font-medium text-foreground">{openOnly ? "Open positions unavailable" : "No live MT4-fed signals"}</div>
            <div className="mt-2 text-sm text-muted-foreground">
              Signals render only when heartbeat and tick freshness are both live. Current reason: {stateReason}
            </div>
          </div>
        ) : signals.length === 0 ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            {emptyTitle}
          </div>
        ) : (
          <div className={compactGrid}>
            {signals.map((signal) => {
            const tone = signalTone(signal.side)
            const gate = describeGate(signal)
            const shadowState = signal.shadow_would_trade ? "would trade" : "would block"
            const shadowDetail = signal.shadow_would_trade
              ? formatShadowDivergence(signal.shadow_live_divergence)
              : humanizeToken(signal.shadow_rejection_reason || signal.shadow_floor_rejection_reason || "shadow_blocked")
            const adaptiveState = signal.adaptive_shadow_would_trade
              ? `${signal.adaptive_playbook || "adaptive"} trade`
              : signal.adaptive_playbook || "no_trade"
            const adaptiveDetail = signal.adaptive_shadow_would_trade
              ? formatShadowDivergence(signal.adaptive_shadow_live_divergence)
              : humanizeToken(signal.adaptive_shadow_rejection_reason || "adaptive_blocked")
            const orchestrationShadowState = signal.orchestration_shadow_enabled
              ? humanizeToken(signal.orchestration_shadow_action || "hold")
              : "disabled"
            const orchestrationShadowDetail = signal.orchestration_shadow_fault_classification
              ? humanizeToken(signal.orchestration_shadow_fault_classification)
              : signal.orchestration_shadow_committee_rationale
                ? signal.orchestration_shadow_committee_rationale
              : signal.orchestration_shadow_divergence_reason
                ? formatShadowDivergence(signal.orchestration_shadow_divergence_reason)
                : formatProposalVotes(signal.orchestration_shadow_proposal_votes)
            const overlayGuidance =
              signal.overlay_metadata?.sleeve_budget_guidance?.[signal.adaptive_sleeve || ""] || null
            const overlayTraceVerbose = Array.isArray(signal.overlay_diagnostics?.policy_trace_verbose)
              ? signal.overlay_diagnostics.policy_trace_verbose
              : []
            const metricCells = [
              { label: "Score", value: formatNumber(signal.score, 2), detail: "edge snapshot" },
              { label: "Target", value: formatPercent(signal.target_pct), detail: "expected move" },
              { label: "Spread", value: formatBps(signal.spread_bps), detail: `max ${formatBps(signal.max_spread_bps)}` },
              { label: "Shadow EV", value: formatBps(signal.calibrated_ev_bps_shadow), detail: `quality ${formatNumber(signal.entry_quality_score_shadow, 2)}` },
            ]
            const structureChips = [
              { label: "HTF", value: formatNumber(signal.htf_alignment_score, 2) },
              { label: "Pullback", value: formatNumber(signal.pullback_quality_score, 2) },
              { label: "Resume", value: formatNumber(signal.resume_trigger_score, 2) },
              { label: "Chase", value: formatNumber(signal.extension_penalty_score, 2) },
              { label: "Structure", value: formatNumber(signal.structure_timing_score, 2), active: Boolean(signal.structure_rescue_active) },
              { label: "Uncertainty", value: formatNumber(signal.uncertainty_score, 2) },
              { label: "Disagree", value: formatNumber(signal.model_disagreement_score, 2) },
              { label: "Adaptive Q", value: formatNumber(signal.adaptive_entry_quality, 2), active: Boolean(signal.adaptive_shadow_would_trade) },
            ]

            if (compact) {
              return (
                <div
                  key={`${signal.symbol}-${signal.side}-${signal.reason || "signal"}`}
                  className="relative overflow-hidden rounded-[1.8rem] border border-slate-200/80 bg-white/88 p-4 shadow-[0_18px_40px_rgba(15,23,42,0.08)]"
                >
                  <div className={cn("pointer-events-none absolute inset-0 bg-gradient-to-br opacity-100", tone.halo)} />
                  <div className={cn("absolute inset-y-4 left-4 w-1 rounded-full", tone.accent)} />

                  <div className="relative space-y-4 pl-4">
                    <div className="flex items-start justify-between gap-4">
                      <div className="flex items-start gap-3">
                        <div className={cn("flex h-12 w-12 items-center justify-center rounded-[1.1rem] border shadow-[inset_0_1px_0_rgba(255,255,255,0.7)]", tone.iconWrap)}>
                          <tone.Icon className={cn("h-5 w-5", tone.icon)} />
                        </div>
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="text-2xl font-semibold tracking-[-0.05em] text-slate-950">{signal.symbol}</div>
                            <span
                              className={cn(
                                "inline-flex items-center rounded-full border px-2.5 py-1 text-[10px] font-semibold uppercase tracking-[0.2em]",
                                tone.sidePill,
                              )}
                            >
                              {signal.side}
                            </span>
                          </div>
                          <div className="mt-1 text-sm text-slate-600">
                            {formatNumber(signal.position_lots, 2)} lots
                            <span className="mx-2 text-slate-300">•</span>
                            entry {formatNumber(signal.position_open_price, 5)}
                          </div>
                        </div>
                      </div>

                      <div className="text-right">
                        <span
                          className={cn(
                            "inline-flex items-center gap-2 rounded-full border px-3 py-1 text-[10px] font-semibold uppercase tracking-[0.22em]",
                            gateBadgeClasses(gate.label),
                          )}
                        >
                          <ShieldCheck className={cn("h-3.5 w-3.5", gate.tone)} />
                          {gate.label}
                        </span>
                        <div className={cn("mt-2 font-mono text-2xl font-semibold tracking-[-0.05em]", Number(signal.position_profit) >= 0 ? "text-emerald-600" : "text-rose-600")}>
                          {formatSignedCurrency(signal.position_profit)}
                        </div>
                      </div>
                    </div>

                    <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                      <MetricCell label="Price" value={formatNumber(signal.price, 5)} detail={signal.position_side || "N/A"} />
                      <MetricCell label="Spread" value={formatBps(signal.spread_bps)} detail={`max ${formatBps(signal.max_spread_bps)}`} />
                      <MetricCell label="Score" value={formatNumber(signal.score, 2)} detail={`target ${formatPercent(signal.target_pct)}`} />
                      <MetricCell label="Adaptive" value={formatNumber(signal.adaptive_entry_quality, 2)} detail={signal.adaptive_playbook || "no trade"} />
                    </div>

                    <div className="rounded-[1.4rem] border border-slate-200/80 bg-slate-50/80 px-4 py-3">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">Lifecycle</div>
                      <div className="mt-2 text-sm text-slate-700">
                        {humanizeToken(signal.lifecycle_action || "hold")}
                        <span className="mx-2 text-slate-300">•</span>
                        {humanizeToken(signal.lifecycle_reason || "position_open_hold")}
                      </div>
                      <div className="mt-1 text-xs leading-relaxed text-slate-600">
                        {Boolean(signal.reversal_context_active ?? isOppositeSide(signal))
                          ? `reversal ${signal.reversal_ready ? "ready" : "blocked"} • ${signal.reversal_ready ? "qualified" : formatReasonList(signal.reversal_blocking_reasons)}`
                          : `add-on ${signal.entry_ready ? "ready" : "blocked"} • ${signal.entry_ready ? "qualified" : formatReasonList(signal.entry_blocking_reasons)}`}
                      </div>
                    </div>

                    <div className="grid gap-2 md:grid-cols-3">
                      <DecisionRow
                        label="Shadow"
                        state={shadowState}
                        detail={shadowDetail}
                        meta={signal.portfolio_rank_shadow ? `rank #${formatNumber(signal.portfolio_rank_shadow, 0)}` : undefined}
                        kind="shadow"
                      />
                      <DecisionRow
                        label="Orchestrator"
                        state={orchestrationShadowState}
                        detail={orchestrationShadowDetail}
                        meta={[
                          signal.orchestration_shadow_baseline_action
                            ? `baseline ${humanizeToken(signal.orchestration_shadow_baseline_action)}`
                            : "",
                          signal.orchestration_shadow_approval_state
                            ? `approval ${humanizeToken(signal.orchestration_shadow_approval_state)}`
                            : "",
                          signal.orchestration_shadow_committee_winning_agent
                            ? `winner ${humanizeToken(signal.orchestration_shadow_committee_winning_agent)}`
                            : "",
                          signal.orchestration_shadow_command_status
                            ? `cmd ${humanizeToken(signal.orchestration_shadow_command_status)}`
                            : "",
                          signal.orchestration_shadow_fault_classification ? "fault" : "",
                        ]
                          .filter(Boolean)
                          .join(" • ")}
                        kind="orchestrator"
                      />
                      <DecisionRow
                        label="Adaptive"
                        state={adaptiveState}
                        detail={adaptiveDetail}
                        meta={signal.adaptive_environment_state ? `env ${humanizeToken(signal.adaptive_environment_state)}` : undefined}
                        kind="adaptive"
                      />
                    </div>

                    <div className="rounded-[1.4rem] border border-cyan-200/80 bg-cyan-50/70 px-4 py-3">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-cyan-700">Desk Overlay</div>
                      <div className="mt-2 text-sm text-slate-700">
                        conviction {formatNumber(signal.conviction_score, 2)}
                        <span className="mx-2 text-cyan-200">•</span>
                        {humanizeToken(signal.conviction_band || "low")}
                        <span className="mx-2 text-cyan-200">•</span>
                        {humanizeToken(signal.thesis_stage || "stand_down")}
                      </div>
                      <div className="mt-1 text-xs leading-relaxed text-slate-600">
                        {humanizeToken(signal.portfolio_posture || "balanced_probe")}
                        <span className="mx-2 text-cyan-200">•</span>
                        budget {formatNumber(signal.sleeve_budget_used, 0)}/{formatNumber(signal.sleeve_budget_target, 0)}
                        <span className="mx-2 text-cyan-200">•</span>
                        trace {formatNumber(signal.policy_trace?.length || overlayTraceVerbose.length || 0, 0)}
                      </div>
                    </div>
                  </div>
                </div>
              )
            }

            return (
              <div
                key={`${signal.symbol}-${signal.side}-${signal.reason || "signal"}`}
                className="relative overflow-hidden rounded-[2rem] border border-slate-200/80 bg-white/85 p-5 shadow-[0_24px_60px_rgba(15,23,42,0.08)]"
              >
                <div className={cn("pointer-events-none absolute inset-0 bg-gradient-to-br opacity-100", tone.halo)} />
                <div className={cn("absolute inset-y-5 left-4 w-1 rounded-full", tone.accent)} />

                <div className="relative grid gap-5 xl:grid-cols-[minmax(0,1.45fr)_minmax(320px,0.95fr)]">
                  <div className="space-y-4 pl-4">
                    <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
                      <div className="flex items-start gap-4">
                        <div className={cn("flex h-14 w-14 items-center justify-center rounded-[1.25rem] border shadow-[inset_0_1px_0_rgba(255,255,255,0.7)]", tone.iconWrap)}>
                          <tone.Icon className={cn("h-6 w-6", tone.icon)} />
                        </div>
                        <div>
                          <div className="flex flex-wrap items-center gap-2">
                            <div className="text-3xl font-semibold tracking-[-0.05em] text-slate-950">{signal.symbol}</div>
                            <span
                              className={cn(
                                "inline-flex items-center rounded-full border px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.22em]",
                                tone.sidePill,
                              )}
                            >
                              {signal.side}
                            </span>
                            <span className="text-xs uppercase tracking-[0.18em] text-slate-500">{tone.sideLabel}</span>
                          </div>
                          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-sm text-slate-600">
                            <span>@ {formatNumber(signal.price, 5)}</span>
                            <span className="text-slate-300">•</span>
                            <span>spread {formatBps(signal.spread_bps)}</span>
                            <span className="text-slate-300">•</span>
                            <span>{humanizeToken(signal.reason || "no_reason")}</span>
                          </div>
                        </div>
                      </div>

                      <div className="flex flex-col items-start gap-2 lg:items-end">
                        <span
                          className={cn(
                            "inline-flex items-center gap-2 rounded-full border px-3 py-1.5 text-[11px] font-semibold uppercase tracking-[0.22em]",
                            gateBadgeClasses(gate.label),
                          )}
                        >
                          <ShieldCheck className={cn("h-3.5 w-3.5", gate.tone)} />
                          {gate.label}
                        </span>
                        <div className="max-w-[18rem] text-sm leading-relaxed text-slate-600 lg:text-right">{gate.detail}</div>
                      </div>
                    </div>

                    <div className="grid gap-3 sm:grid-cols-2 2xl:grid-cols-4">
                      {metricCells.map((metric) => (
                        <MetricCell key={metric.label} label={metric.label} value={metric.value} detail={metric.detail} />
                      ))}
                    </div>

                    <div className="grid gap-3">
                      <DecisionRow
                        label="Live"
                        state={gate.label}
                        detail={gate.detail}
                        meta={signal.execution_mode ? `exec ${humanizeToken(signal.execution_mode)}` : undefined}
                        kind="live"
                      />
                      <DecisionRow
                        label="Shadow"
                        state={shadowState}
                        detail={shadowDetail}
                        meta={signal.portfolio_rank_shadow ? `rank #${formatNumber(signal.portfolio_rank_shadow, 0)}` : undefined}
                        kind="shadow"
                      />
                      <DecisionRow
                        label="Orchestrator"
                        state={orchestrationShadowState}
                        detail={orchestrationShadowDetail}
                        meta={[
                          signal.orchestration_shadow_baseline_action
                            ? `baseline ${humanizeToken(signal.orchestration_shadow_baseline_action)}`
                            : "",
                          signal.orchestration_shadow_approval_state
                            ? `approval ${humanizeToken(signal.orchestration_shadow_approval_state)}`
                            : "",
                          signal.orchestration_shadow_committee_arbiter_stage
                            ? humanizeToken(signal.orchestration_shadow_committee_arbiter_stage)
                            : "",
                          signal.orchestration_shadow_command_status
                            ? `cmd ${humanizeToken(signal.orchestration_shadow_command_status)}`
                            : "",
                          signal.orchestration_shadow_latency_ms !== null &&
                          signal.orchestration_shadow_latency_ms !== undefined
                            ? `${formatNumber(signal.orchestration_shadow_latency_ms, 0)} ms`
                            : "",
                        ]
                          .filter(Boolean)
                          .join(" • ")}
                        kind="orchestrator"
                      />
                      <DecisionRow
                        label="Adaptive"
                        state={adaptiveState}
                        detail={adaptiveDetail}
                        meta={[
                          signal.adaptive_environment_state ? `env ${humanizeToken(signal.adaptive_environment_state)}` : "",
                          signal.adaptive_aggressive_fallback_used ? "fallback used" : "",
                        ]
                          .filter(Boolean)
                          .join(" • ")}
                        kind="adaptive"
                      />
                    </div>
                  </div>

                  <div className="space-y-4 rounded-[1.75rem] border border-slate-200/80 bg-[linear-gradient(180deg,rgba(248,250,252,0.95),rgba(241,245,249,0.9))] p-4">
                    <div>
                      <div className="text-[11px] uppercase tracking-[0.24em] text-slate-500">Model Context</div>
                      <div className="mt-2 text-sm leading-relaxed text-slate-600">
                        Structure and timing snapshot for this candidate, with live, shadow, and adaptive views aligned.
                      </div>
                    </div>

                    <div className="grid gap-2 sm:grid-cols-2">
                      {structureChips.map((chip) => (
                        <StatChip key={chip.label} label={chip.label} value={chip.value} active={chip.active} />
                      ))}
                    </div>

                    {signal.position_open ? (
                      <div className="rounded-[1.5rem] border border-sky-200/80 bg-sky-50/80 px-4 py-3">
                        <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-sky-700">Open Position</div>
                        <div className="mt-2 text-sm leading-relaxed text-slate-700">
                          {formatNumber(signal.position_lots, 2)} lots {signal.position_side || "N/A"} at {formatNumber(signal.position_open_price, 5)}
                          <span className="mx-2 text-sky-200">•</span>
                          P/L {formatSignedCurrency(signal.position_profit)}
                        </div>
                        <div className="mt-1 text-xs leading-relaxed text-slate-600">
                          {Boolean(signal.reversal_context_active ?? isOppositeSide(signal))
                            ? `bias opposes open position • reversal ${signal.reversal_ready ? "ready" : "blocked"} • ${signal.reversal_ready ? "qualified" : formatReasonList(signal.reversal_blocking_reasons)}`
                            : `bias matches open position • add-on ${signal.entry_ready ? "ready" : "blocked"} • ${signal.entry_ready ? "qualified" : formatReasonList(signal.entry_blocking_reasons)}`}
                        </div>
                        <div className="mt-1 text-xs text-slate-600">
                          lifecycle {humanizeToken(signal.lifecycle_action || "hold")}
                          <span className="mx-2 text-sky-200">•</span>
                          {humanizeToken(signal.lifecycle_reason || "position_open_hold")}
                        </div>
                      </div>
                    ) : (
                      <div className="rounded-[1.5rem] border border-slate-200/80 bg-white/75 px-4 py-3">
                        <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-slate-500">Execution Snapshot</div>
                        <div className="mt-2 text-sm leading-relaxed text-slate-700">
                          strict {signal.strict_entry_ready ? "ready" : "blocked"}
                          <span className="mx-2 text-slate-200">•</span>
                          execution {signal.execution_entry_ready ? "ready" : "blocked"}
                          <span className="mx-2 text-slate-200">•</span>
                          {humanizeToken(signal.execution_rejection_reason || signal.strict_rejection_reason || "none")}
                        </div>
                      </div>
                    )}

                    <div className="rounded-[1.5rem] border border-cyan-200/80 bg-cyan-50/70 px-4 py-3">
                      <div className="text-[10px] font-semibold uppercase tracking-[0.24em] text-cyan-700">Desk Overlay Trace</div>
                      <div className="mt-2 grid gap-2 sm:grid-cols-2">
                        <StatChip label="Conviction" value={formatNumber(signal.conviction_score, 2)} active={Boolean(signal.adaptive_shadow_would_trade)} />
                        <StatChip label="Band" value={humanizeToken(signal.conviction_band || "low")} />
                        <StatChip label="Thesis" value={humanizeToken(signal.thesis_stage || "stand_down")} />
                        <StatChip label="Posture" value={humanizeToken(signal.portfolio_posture || "balanced_probe")} active={String(signal.portfolio_posture || "") === "selective_press"} />
                        <StatChip label="Budget" value={`${formatNumber(signal.sleeve_budget_used, 0)}/${formatNumber(signal.sleeve_budget_target, 0)}`} />
                        <StatChip label="Replace" value={formatNumber(signal.replacement_urgency, 2)} />
                      </div>
                      <div className="mt-3 text-xs leading-relaxed text-slate-600">
                        {overlayGuidance
                          ? `guidance ${humanizeToken(overlayGuidance.reason || "none")} • tilt ${humanizeToken(overlayGuidance.tilt || "neutral")}`
                          : "guidance unavailable"}
                      </div>
                      <div className="mt-2 text-xs leading-relaxed text-slate-600">
                        {(signal.policy_trace || [])
                          .slice(0, 6)
                          .map((item: string) => humanizeToken(item))
                          .join(" • ") || "policy trace unavailable"}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )
          })}
          </div>
        )}
      </div>
    </Card>
  )
}
