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

function formatShadowDivergence(value: unknown): string {
  const txt = String(value || "").trim().toLowerCase()
  if (!txt) return "—"
  if (txt === "agree_ready") return "agrees with live"
  if (txt === "agree_blocked") return "agrees on block"
  if (txt === "live_only") return "live-only approval"
  if (txt === "shadow_only") return "shadow-only approval"
  if (txt === "open_position") return "open position"
  return txt.replaceAll("_", " ")
}

function isOppositeSide(signal: { side?: string; position_side?: string }): boolean {
  const signalSide = String(signal.side || "").trim().toUpperCase()
  const positionSide = String(signal.position_side || "").trim().toUpperCase()
  if (!signalSide || !positionSide) return false
  return (signalSide === "BUY" && positionSide === "SELL") || (signalSide === "SELL" && positionSide === "BUY")
}

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
        return { label: "open", tone: "text-emerald-400", detail: "reversal allowed" }
      }
      return {
        label: "open",
        tone: "text-sky-400",
        detail: `reversal blocked: ${formatReasonList(signal.reversal_blocking_reasons)}`,
      }
    }
    if (lifecycleAction || lifecycleReason) {
      return {
        label: "open",
        tone: "text-sky-400",
        detail: lifecycleReason || lifecycleAction || "position live",
      }
    }
    return { label: "open", tone: "text-sky-400", detail: "position live" }
  }
  if (enqueueStatus === "queued") {
    return { label: "queued", tone: "text-sky-400", detail: action || "entry queued" }
  }
  if (enqueueStatus === "duplicate_action_skip") {
    return { label: "suppressed", tone: "text-slate-400", detail: "duplicate entry skipped" }
  }
  if (signal.execution_ready) {
    return { label: "ready", tone: "text-emerald-400", detail: "entry allowed" }
  }
  return {
    label: "blocked",
    tone: "text-amber-400",
    detail: formatReasonList(signal.entry_blocking_reasons) || action || "entry blocked",
  }
}

function signalTone(side: string): { wrap: string; icon: string; Icon: typeof ArrowUpRight } {
  if (side === "BUY") {
    return {
      wrap: "bg-emerald-500/12",
      icon: "text-emerald-400",
      Icon: ArrowUpRight,
    }
  }
  if (side === "SELL") {
    return {
      wrap: "bg-rose-500/12",
      icon: "text-rose-400",
      Icon: ArrowDownRight,
    }
  }
  return {
    wrap: "bg-slate-500/12",
    icon: "text-slate-400",
    Icon: Minus,
  }
}

export function LiveSignals() {
  const { state, loading } = useLiveBridgeState(3000)
  const decisions = state?.agentDecisions
  const signals = Array.isArray(decisions)
    ? [...decisions].sort((a, b) => Number(Boolean(b.position_open)) - Number(Boolean(a.position_open)))
    : []
  const live = Boolean(state?.signalDataFresh && state?.statusTier === "bridge_up_mt4_live")

  return (
    <Card className="overflow-hidden p-0">
      <div className="border-b border-border/70 px-6 py-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Live Signals</div>
            <h3 className="mt-2 text-2xl font-semibold text-foreground">MT4-fed candidate stream</h3>
          </div>
          <Badge variant="outline" className={cn("rounded-full px-3 py-1 text-xs", bridgeStatusClasses(state?.statusTier))}>
            {loading ? "Loading" : bridgeStatusLabel(state?.statusTier)}
          </Badge>
        </div>
      </div>

      <div className="space-y-3 px-6 py-6">
        {loading ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            Loading live bridge snapshot…
          </div>
        ) : !live ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center">
            <div className="text-lg font-medium text-foreground">No live MT4-fed signals</div>
            <div className="mt-2 text-sm text-muted-foreground">
              Signals render only when heartbeat and tick freshness are both live. Current reason: {String(state?.signalDataReason || state?.tickReason || "waiting")}
            </div>
          </div>
        ) : signals.length === 0 ? (
          <div className="rounded-3xl border border-dashed border-border px-6 py-12 text-center text-muted-foreground">
            No active signals in the current evaluation cycle.
          </div>
        ) : (
          signals.map((signal) => {
            const tone = signalTone(signal.side)
            const gate = describeGate(signal)
            return (
              <div
                key={`${signal.symbol}-${signal.side}-${signal.reason || "signal"}`}
                className="rounded-3xl border border-border/70 bg-background/55 p-4"
              >
                <div className="flex flex-col gap-4 lg:flex-row lg:items-center lg:justify-between">
                  <div className="flex items-center gap-4">
                    <div className={cn("rounded-2xl p-3", tone.wrap)}>
                      <tone.Icon className={cn("h-5 w-5", tone.icon)} />
                    </div>
                    <div>
                      <div className="text-xl font-semibold text-foreground">{signal.symbol}</div>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-sm text-muted-foreground">
                        <span>{signal.side}</span>
                        <span className="text-border">•</span>
                        <span>@ {formatNumber(signal.price, 5)}</span>
                        <span className="text-border">•</span>
                        <span>
                          spread {formatBps(signal.spread_bps)} / max {formatBps(signal.max_spread_bps)}
                        </span>
                        <span className="text-border">•</span>
                        <span>{signal.reason || "no reason"}</span>
                      </div>
                      {signal.position_open && (
                        <>
                          <div className="mt-2 text-xs text-muted-foreground">
                            Open {formatNumber(signal.position_lots, 2)} lots {signal.position_side || "N/A"}
                            <span className="mx-2 text-border">•</span>
                            entry {formatNumber(signal.position_open_price, 5)}
                            <span className="mx-2 text-border">•</span>
                            P/L {formatSignedCurrency(signal.position_profit)}
                          </div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            {Boolean(signal.reversal_context_active ?? isOppositeSide(signal)) ? (
                              <>
                                Current bias opposes open position
                                <span className="mx-2 text-border">•</span>
                                reversal {signal.reversal_ready ? "ready" : "blocked"}
                                <span className="mx-2 text-border">•</span>
                                {signal.reversal_ready
                                  ? "qualified"
                                  : formatReasonList(signal.reversal_blocking_reasons)}
                              </>
                            ) : (
                              <>
                                Current bias matches open position
                                <span className="mx-2 text-border">•</span>
                                add-on {signal.entry_ready ? "ready" : "blocked"}
                                <span className="mx-2 text-border">•</span>
                                {signal.entry_ready ? "qualified" : formatReasonList(signal.entry_blocking_reasons)}
                              </>
                            )}
                          </div>
                          <div className="mt-1 text-xs text-muted-foreground">
                            lifecycle {signal.lifecycle_action || "hold"}
                            <span className="mx-2 text-border">•</span>
                            {signal.lifecycle_reason || "position_open_hold"}
                          </div>
                        </>
                      )}
                      <div className="mt-2 text-xs text-muted-foreground">
                        shadow EV {formatBps(signal.calibrated_ev_bps_shadow)}
                        <span className="mx-2 text-border">•</span>
                        quality {formatNumber(signal.entry_quality_score_shadow, 2)}
                        <span className="mx-2 text-border">•</span>
                        uncertainty {formatNumber(signal.uncertainty_score, 2)}
                        <span className="mx-2 text-border">•</span>
                        disagreement {formatNumber(signal.model_disagreement_score, 2)}
                      </div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        HTF {formatNumber(signal.htf_alignment_score, 2)}
                        <span className="mx-2 text-border">•</span>
                        pullback {formatNumber(signal.pullback_quality_score, 2)}
                        <span className="mx-2 text-border">•</span>
                        resume {formatNumber(signal.resume_trigger_score, 2)}
                        <span className="mx-2 text-border">•</span>
                        chase risk {formatNumber(signal.extension_penalty_score, 2)}
                        <span className="mx-2 text-border">•</span>
                        structure {formatNumber(signal.structure_timing_score, 2)}
                        {signal.structure_rescue_active ? (
                          <>
                            <span className="mx-2 text-border">•</span>
                            rescue active
                          </>
                        ) : null}
                      </div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        shadow rank {signal.portfolio_rank_shadow ? `#${formatNumber(signal.portfolio_rank_shadow, 0)}` : "—"}
                        <span className="mx-2 text-border">•</span>
                        shadow {signal.shadow_would_trade ? "would trade" : "would block"}
                        <span className="mx-2 text-border">•</span>
                        {signal.shadow_would_trade
                          ? formatShadowDivergence(signal.shadow_live_divergence)
                          : signal.shadow_rejection_reason || signal.shadow_floor_rejection_reason || "shadow blocked"}
                      </div>
                    </div>
                  </div>

                  <div className="grid gap-2 sm:grid-cols-3 lg:min-w-[360px]">
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Score</div>
                      <div className="mt-2 font-mono text-lg text-foreground">{formatNumber(signal.score, 2)}</div>
                    </div>
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Target</div>
                      <div className="mt-2 font-mono text-lg text-foreground">{formatPercent(signal.target_pct)}</div>
                    </div>
                    <div className="rounded-2xl border border-border/70 bg-card/70 px-4 py-3">
                      <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Gate</div>
                      <div className="mt-2 inline-flex items-center gap-2 font-medium text-foreground">
                        <ShieldCheck className={cn("h-4 w-4", gate.tone)} />
                        {gate.label}
                      </div>
                      <div className="mt-1 text-xs text-muted-foreground">
                        {gate.detail}
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            )
          })
        )}
      </div>
    </Card>
  )
}
