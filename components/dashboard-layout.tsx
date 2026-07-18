// AGENT: ROLE: Shared dashboard shell with sidebar navigation and cross-page bridge/runtime summary.
// AGENT: ENTRYPOINT: exported `DashboardLayout` component.
// AGENT: PRIMARY INPUTS: typed bridge state from `useLiveBridgeState`.
// AGENT: PRIMARY OUTPUTS: top-level dashboard chrome and status sidebar.
// AGENT: DEPENDS ON: `lib/hooks/use-live-bridge-state.ts`, `lib/trading/live-state`.
// AGENT: CALLED BY: app layout/page composition.
// AGENT: STATE / SIDE EFFECTS: render only.
// AGENT: HANDSHAKES: dashboard hook contract and route-level page split.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `components/dashboard-home.tsx` -> `components/live-status-rail.tsx`
"use client"

import type React from "react"

import { useState } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { BrainCircuit, LayoutDashboard, Menu, Radar, TrendingUp, X } from "lucide-react"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useTradingHistory } from "@/lib/hooks/use-trading-history"
import { describeBridgeSource } from "@/lib/trading/bridge-source"
import {
  bridgeStatusClasses,
  bridgeStatusDotClasses,
  bridgeStatusLabel,
  formatAgeSeconds,
  formatSignedBps,
} from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

const navigation = [
  { name: "Dashboard", href: "/", icon: LayoutDashboard, summary: "Bridge truth, runtime, live cards" },
  { name: "Signals", href: "/signals", icon: TrendingUp, summary: "Signal history and filters" },
  { name: "Options", href: "/options", icon: Radar, summary: "Volatility and options surfaces" },
  { name: "Performance", href: "/performance", icon: TrendingUp, summary: "Equity, drawdown, governance" },
  { name: "AI Training", href: "/ai-training", icon: BrainCircuit, summary: "Observe-only training telemetry" },
]

const pageTitles: Record<string, string> = {
  "/": "Dashboard",
  "/signals": "Signals",
  "/options": "Options",
  "/performance": "Performance",
  "/ai-training": "AI Training",
}

export function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { state, updatedAt } = useLiveBridgeState(5000)
  const { error: historyError } = useTradingHistory(5000)
  const bridgeSource = describeBridgeSource(state?.bridgeUrl, state?.bridgePrimaryUrl)

  const currentPage = pageTitles[pathname] || "FX Trader"
  const pulseLabel = state?.lastHeartbeat
    ? formatAgeSeconds(state.heartbeatAgeSecs)
    : updatedAt
      ? new Date(updatedAt).toLocaleTimeString()
      : "n/a"
  const runtimeStatusLabel = String(state?.runtimeStatus || "unknown")
  const runtimePhaseLabel = String(state?.runtimePhase || "")
  const runtimePairLabel = String(state?.runtimePhasePair || "")
  const runtimeLine =
    runtimeStatusLabel === "running"
      ? runtimePhaseLabel || "main_loop"
      : [runtimeStatusLabel, runtimePhaseLabel, runtimePairLabel].filter(Boolean).join(" · ") || runtimeStatusLabel
  const runtimeFailureLabel = String(state?.runtimeFailureReason || "")
  const shadowPolicy = state?.shadowPolicy
  const spreadDiagnostics = shadowPolicy?.spreadDiagnostics
  const secondarySpreadDiagnostics = shadowPolicy?.secondarySpreadDiagnostics
  const spreadPairRow =
    spreadDiagnostics?.dominantPair && spreadDiagnostics?.byPair?.[spreadDiagnostics.dominantPair]
      ? spreadDiagnostics.byPair[spreadDiagnostics.dominantPair]
      : null
  const secondarySpreadPairRow =
    secondarySpreadDiagnostics?.dominantPair && secondarySpreadDiagnostics?.byPair?.[secondarySpreadDiagnostics.dominantPair]
      ? secondarySpreadDiagnostics.byPair[secondarySpreadDiagnostics.dominantPair]
      : null
  const spreadSessionRow =
    spreadDiagnostics?.dominantSession && spreadDiagnostics?.bySession?.[spreadDiagnostics.dominantSession]
      ? spreadDiagnostics.bySession[spreadDiagnostics.dominantSession]
      : null
  const secondarySpreadSessionRow =
    secondarySpreadDiagnostics?.dominantSession && secondarySpreadDiagnostics?.bySession?.[secondarySpreadDiagnostics.dominantSession]
      ? secondarySpreadDiagnostics.bySession[secondarySpreadDiagnostics.dominantSession]
      : null
  const lastRuntimeFailure = state?.lastRuntimeStartupFailure
  const showLastRuntimeFailure = Boolean(
    runtimeStatusLabel === "running" &&
      lastRuntimeFailure &&
      lastRuntimeFailure.bootId &&
      lastRuntimeFailure.bootId !== state?.runtimeBootId,
  )
  const lastRuntimeFailureLine = showLastRuntimeFailure
    ? `Last failure ${formatAgeSeconds(lastRuntimeFailure?.failedAgeSecs)}${lastRuntimeFailure?.phase ? ` · ${lastRuntimeFailure.phase}` : ""}${lastRuntimeFailure?.phasePair ? ` · ${lastRuntimeFailure.phasePair}` : ""}`
    : ""
  const runtimePillLine = showLastRuntimeFailure ? `${runtimeLine} · last fail ${formatAgeSeconds(lastRuntimeFailure?.failedAgeSecs)}` : runtimeLine
  const shadowLine = shadowPolicy?.enabled
    ? shadowPolicy.dominantRejectionReason === "spread_too_wide" && spreadDiagnostics && spreadDiagnostics.rejectCount > 0
      ? `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow · spread choke ${spreadDiagnostics.dominantSession || "unknown"} · ${spreadDiagnostics.dominantPair || "n/a"}`
      : secondarySpreadDiagnostics && secondarySpreadDiagnostics.rejectCount > 0
        ? `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow · ${shadowPolicy.dominantRejectionReason || "no dominant reject"} · secondary spread ${secondarySpreadDiagnostics.dominantPair || "n/a"}`
      : `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow · ${shadowPolicy.dominantRejectionReason || "no dominant reject"} · rescues ${shadowPolicy.structureRescueCount} · live-only ${shadowPolicy.divergenceCounts.liveOnly}`
    : "shadow disabled"
  const tier1Shadow = shadowPolicy?.tierSummary?.tier1
  const tier2Shadow = shadowPolicy?.tierSummary?.tier2

  return (
    <div className="min-h-screen bg-background text-foreground">
      {sidebarOpen && (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-40 bg-slate-950/45 backdrop-blur-sm active:bg-slate-950/55 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-80 border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-transform duration-200 ease-out lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-full flex-col">
          <div className="border-b border-sidebar-border px-6 py-6">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.26em] text-slate-400">FX Trader</div>
                <h2 className="mt-3 text-2xl font-semibold text-white">Control Surface</h2>
                <p className="mt-2 text-sm text-slate-400">Stable local production dashboard</p>
              </div>
              <button
                type="button"
                aria-label="Close navigation"
                onClick={() => setSidebarOpen(false)}
                className="flex h-11 w-11 items-center justify-center rounded-full text-slate-400 hover:bg-white/10 hover:text-white active:scale-95 lg:hidden"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
          </div>

          <nav className="flex-1 space-y-2 px-4 py-5">
            {navigation.map((item) => {
              const isActive = pathname === item.href
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  onClick={() => setSidebarOpen(false)}
                  className={cn(
                    "group block rounded-2xl border px-4 py-3 transition-all active:scale-[0.99]",
                    isActive
                      ? "border-white/14 bg-white/10 text-white"
                      : "border-transparent bg-white/[0.03] text-slate-300 hover:border-white/10 hover:bg-white/[0.06] hover:text-white",
                  )}
                >
                  <div className="flex items-center gap-3">
                    <item.icon className="h-4 w-4" />
                    <span className="font-medium">{item.name}</span>
                  </div>
                  <div className="mt-2 text-sm text-slate-400">{item.summary}</div>
                </Link>
              )
            })}
          </nav>

          <div className="border-t border-sidebar-border px-4 py-5">
            <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Bridge Status</div>
                  <div className="mt-2 flex items-center gap-2">
                    <span className={cn("h-2.5 w-2.5 rounded-full", bridgeStatusDotClasses(state?.statusTier))} />
                    <span className="text-sm font-medium text-white">{bridgeStatusLabel(state?.statusTier)}</span>
                  </div>
                </div>
                <span className={cn("rounded-full border px-2 py-1 text-[11px] font-medium", bridgeStatusClasses(state?.statusTier))}>
                  {String(state?.tickStatus || "unknown")}
                </span>
              </div>
              <div className="mt-3 space-y-1 text-xs text-slate-400">
                <div>Heartbeat: {pulseLabel}</div>
                <div>Ticks: {String(state?.tickReason || "waiting")}</div>
                <div>Loop: {Number.isFinite(Number(state?.runtimeDiag?.loop_latency_ms)) ? `${Number(state?.runtimeDiag?.loop_latency_ms).toFixed(0)} ms` : "n/a"}</div>
                <div>Runtime: {runtimeLine}</div>
                <div>Shadow: {shadowLine}</div>
                {shadowPolicy?.dominantRejectionReason === "spread_too_wide" && spreadDiagnostics && spreadDiagnostics.rejectCount > 0 ? (
                  <div>
                    Spread: {spreadDiagnostics.dominantSession || "unknown"} · {spreadDiagnostics.dominantPair || "n/a"} avg {formatSignedBps(spreadPairRow?.avg_excess_bps)}
                    {spreadSessionRow ? (
                      <span className="ml-1 text-slate-500">({spreadSessionRow.count} rejects)</span>
                    ) : null}
                  </div>
                ) : null}
                {shadowPolicy?.dominantRejectionReason !== "spread_too_wide" &&
                secondarySpreadDiagnostics &&
                secondarySpreadDiagnostics.rejectCount > 0 ? (
                  <div>
                    Secondary spread: {secondarySpreadDiagnostics.dominantSession || "unknown"} · {secondarySpreadDiagnostics.dominantPair || "n/a"} avg {formatSignedBps(secondarySpreadPairRow?.avg_excess_bps)}
                    {secondarySpreadSessionRow ? (
                      <span className="ml-1 text-slate-500">({secondarySpreadSessionRow.count} rejects)</span>
                    ) : null}
                  </div>
                ) : null}
                {shadowPolicy?.enabled ? (
                  <div>
                    Tier1 {tier1Shadow?.wouldTrade ?? 0}/{tier1Shadow?.candidates ?? 0}
                    <span className="mx-1 text-slate-600">·</span>
                    Tier2 {tier2Shadow?.wouldTrade ?? 0}/{tier2Shadow?.candidates ?? 0}
                  </div>
                ) : null}
                {runtimeFailureLabel ? <div className="text-rose-300">Failure: {runtimeFailureLabel}</div> : null}
                {!runtimeFailureLabel && lastRuntimeFailureLine ? <div className="text-amber-300">{lastRuntimeFailureLine}</div> : null}
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div className="lg:pl-80">
        <header className="sticky top-0 z-30 border-b border-border/70 bg-background/80 backdrop-blur-xl">
          <div className="flex h-20 items-center gap-4 px-5 lg:px-8">
            <button
              type="button"
              aria-label="Open navigation"
              onClick={() => setSidebarOpen(true)}
              className="flex h-11 w-11 items-center justify-center rounded-full border border-border bg-card text-muted-foreground hover:bg-accent hover:text-foreground active:scale-95 lg:hidden"
            >
              <Menu className="h-5 w-5" />
            </button>
            <div className="min-w-36 flex-1">
              <div className="text-[11px] uppercase tracking-[0.24em] text-muted-foreground">Route</div>
              <div className="mt-1 text-2xl font-semibold text-foreground">{currentPage}</div>
            </div>
            {bridgeSource.isNonPrimary ? (
              <div
                role="status"
                aria-label={`Fallback bridge source ${bridgeSource.endpointLabel}`}
                title={`Serving ${bridgeSource.activeUrl}; primary is ${bridgeSource.primaryUrl}`}
                className="flex shrink-0 items-center gap-2 rounded-full border border-amber-400/40 bg-amber-400/10 px-3 py-2 text-xs font-medium text-amber-200"
              >
                <span className="h-2 w-2 rounded-full bg-amber-300" aria-hidden="true" />
                <span className="hidden sm:inline">Fallback source</span>
                <span className="font-mono">{bridgeSource.endpointLabel}</span>
              </div>
            ) : null}
            <div className="ml-auto hidden min-w-0 items-center gap-3 xl:flex">
              <div className="hidden rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground 2xl:block">
                Surface <span className="ml-2 font-mono text-foreground">local</span>
              </div>
              <div className="hidden rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground 2xl:block">
                Last pulse <span className="ml-2 font-mono text-foreground">{pulseLabel}</span>
              </div>
              <div
                className="max-w-48 truncate rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground 2xl:max-w-56"
                title={`Runtime ${runtimePillLine}`}
              >
                Runtime <span className="ml-2 font-mono text-foreground">{runtimePillLine}</span>
              </div>
              <div
                className="max-w-64 truncate rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground 2xl:max-w-md"
                title={`Shadow ${shadowLine}`}
              >
                Shadow <span className="ml-2 font-mono text-foreground">{shadowLine}</span>
              </div>
            </div>
          </div>
        </header>

        {historyError ? (
          <div
            role="status"
            aria-live="polite"
            className="mx-5 mt-5 rounded-2xl border border-amber-500/30 bg-amber-500/10 px-4 py-3 text-sm text-amber-100 lg:mx-8"
          >
            History telemetry is degraded: {historyError}. Last known good history remains visible.
          </div>
        ) : null}
        <main className="px-5 py-6 lg:px-8">{children}</main>
      </div>
    </div>
  )
}
