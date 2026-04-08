// AGENT: ROLE: Render the compact runtime/bridge/ops health rail shown on the home dashboard and layout chrome.
// AGENT: ENTRYPOINT: exported `LiveStatusRail` component.
// AGENT: PRIMARY INPUTS: bridge state hook plus ops telemetry hook.
// AGENT: PRIMARY OUTPUTS: summarized freshness, runtime, shadow, and adaptive status UI.
// AGENT: DEPENDS ON: `lib/hooks/use-live-bridge-state.ts`, `lib/hooks/use-ops-telemetry`, `lib/trading/live-state`.
// AGENT: CALLED BY: `components/dashboard-home.tsx`, `components/dashboard-layout.tsx`.
// AGENT: STATE / SIDE EFFECTS: render only.
// AGENT: HANDSHAKES: normalized dashboard state contract and ops telemetry contract.
// AGENT: SEE: `docs/agents/dashboard-dataflow.md` -> `lib/hooks/use-live-bridge-state.ts` -> `components/dashboard-home.tsx`
"use client"

import { Badge } from "@/components/ui/badge"
import { Card } from "@/components/ui/card"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { useOpsTelemetry } from "@/lib/hooks/use-ops-telemetry"
import { bridgeStatusClasses, bridgeStatusLabel, formatAgeSeconds } from "@/lib/trading/live-state"
import { Activity, Bot, ChartNoAxesCombined, RefreshCcw, Wifi, Zap } from "lucide-react"

function formatLatency(value: unknown): string {
  const latency = Number(value)
  return Number.isFinite(latency) && latency >= 0 ? `${latency.toFixed(0)} ms` : "n/a"
}

export function LiveStatusRail() {
  const { state, loading } = useLiveBridgeState(3000)
  const ops = useOpsTelemetry(5000)
  const runtimeStatus = String(state?.runtimeStatus || "unknown")
  const runtimePhase = String(state?.runtimePhase || "")
  const runtimePhasePair = String(state?.runtimePhasePair || "")
  const runtimeFailure = String(state?.runtimeFailureReason || "")
  const runtimeStartup = state?.runtimeStartup
  const runtimeStartupStatus = String(state?.runtimeStartupStatus || runtimeStartup?.status || "")
  const runtimeStartupWarnings = Number(state?.runtimeStartupWarningCount || runtimeStartup?.warningCount || 0)
  const lastRuntimeFailure = state?.lastRuntimeStartupFailure
  const shadowPolicy = state?.shadowPolicy
  const adaptiveShadowPolicy = state?.adaptiveShadowPolicy
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
  const showLastRuntimeFailure = Boolean(
    runtimeStatus === "running" &&
      lastRuntimeFailure &&
      lastRuntimeFailure.bootId &&
      lastRuntimeFailure.bootId !== state?.runtimeBootId,
  )

  const runtimeValue =
    runtimeStatus === "running"
      ? formatLatency(state?.runtimeDiag?.loop_latency_ms)
      : [runtimeStatus, runtimePhase].filter(Boolean).join(" · ") || runtimeStatus
  let runtimeDetail = runtimePhasePair
    ? `${runtimePhase || runtimeStatus} on ${runtimePhasePair}`
    : runtimeStatus === "running"
      ? `${Number(state?.tickSymbolsCount || 0)} symbols tracked`
      : String(state?.signalDataReason || "no diagnostics")
  if (shadowPolicy?.enabled) {
    runtimeDetail = `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow entries · ${shadowPolicy.dominantRejectionReason || "no dominant reject"} · rescues ${shadowPolicy.structureRescueCount} · live-only ${shadowPolicy.divergenceCounts.liveOnly}`
    if (secondarySpreadDiagnostics && secondarySpreadDiagnostics.rejectCount > 0) {
      runtimeDetail = `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow entries · ${shadowPolicy.dominantRejectionReason || "no dominant reject"} · secondary spread ${secondarySpreadDiagnostics.dominantSession || "unknown"} · ${secondarySpreadDiagnostics.dominantPair || "n/a"} avg +${Number(secondarySpreadPairRow?.avg_excess_bps || 0).toFixed(2)} bps`
    }
    if (shadowPolicy.dominantRejectionReason === "spread_too_wide" && spreadDiagnostics && spreadDiagnostics.rejectCount > 0) {
      runtimeDetail = `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow entries · spread choke ${spreadDiagnostics.dominantSession || "unknown"} · ${spreadDiagnostics.dominantPair || "n/a"} avg +${Number(spreadPairRow?.avg_excess_bps || 0).toFixed(2)} bps`
    }
    if (adaptiveShadowPolicy?.enabled) {
      runtimeDetail = `${shadowPolicy.wouldTradeCount}/${shadowPolicy.candidateCount} shadow entries · adaptive ${adaptiveShadowPolicy.wouldTradeCount}/${adaptiveShadowPolicy.candidateCount} · ${adaptiveShadowPolicy.dominantRejectionReason || "no adaptive reject"} · fallback ${adaptiveShadowPolicy.aggressiveFallbackCount}`
    }
  }
  if (showLastRuntimeFailure) {
    runtimeDetail = `last fail ${formatAgeSeconds(lastRuntimeFailure?.failedAgeSecs)}${lastRuntimeFailure?.phase ? ` · ${lastRuntimeFailure.phase}` : ""}${lastRuntimeFailure?.phasePair ? ` on ${lastRuntimeFailure.phasePair}` : ""}`
  }
  if (runtimeFailure) {
    runtimeDetail = runtimeFailure
  }
  if (runtimeStartupWarnings > 0) {
    const startupDetails = [
      runtimeStartupStatus || "startup warnings",
      `model load ${runtimeStartup?.modelLoadErrors ?? state?.modelLoadErrors ?? 0} errors`,
      `${runtimeStartup?.modelLoadTimeouts ?? state?.modelLoadTimeouts ?? 0} timeouts`,
      `${runtimeStartup?.startupInferenceFailures ?? state?.startupInferenceFailures ?? 0} inference fails`,
    ]
    runtimeDetail = `${runtimeDetail} · ${startupDetails.join(" · ")}`
  }

  const items = [
    {
      label: "Bridge",
      value: state?.bridgeState === "bridge_up" ? "reachable" : "unreachable",
      detail: state?.statusTier === "bridge_down" ? "dashboard cannot reach bridge" : "proxy responding",
      icon: Wifi,
    },
    {
      label: "MT4 Feed",
      value: bridgeStatusLabel(state?.statusTier),
      detail: state?.lastHeartbeat ? `heartbeat ${formatAgeSeconds(state?.heartbeatAgeSecs)}` : "heartbeat missing",
      icon: Activity,
    },
    {
      label: "Ticks",
      value: String(state?.tickStatus || "unknown"),
      detail: String(state?.tickReason || "no diagnostics"),
      icon: Zap,
    },
    {
      label: "Runtime",
      value: runtimeValue,
      detail: runtimeDetail,
      icon: RefreshCcw,
    },
    {
      label: "AI Ops",
      value: ops.status,
      detail: ops.data ? `${ops.data.summary.workflows_total} workflows` : "no ops snapshot",
      icon: Bot,
    },
    {
      label: "Positions",
      value: loading ? "..." : `${Number(state?.openPositionsCount || state?.positions?.length || 0)}`,
      detail: loading
        ? "loading"
        : `${Number(state?.readyEntriesCount || 0)} ready | ${Number(state?.tradesExecuted || 0)} session executions`,
      icon: ChartNoAxesCombined,
    },
  ]

  return (
    <Card className="overflow-hidden border-slate-300/20 bg-slate-950 text-slate-100">
      <div className="border-b border-white/10 px-6 py-4">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
          <div>
            <div className="text-xs uppercase tracking-[0.24em] text-slate-400">Control Surface</div>
            <h2 className="mt-2 text-2xl font-semibold text-white">Bridge, MT4, runtime, and ops in one line</h2>
          </div>
          <Badge variant="outline" className={bridgeStatusClasses(state?.statusTier)}>
            {bridgeStatusLabel(state?.statusTier)}
          </Badge>
        </div>
      </div>
      <div className="grid gap-px bg-white/8 lg:grid-cols-6">
        {items.map((item) => (
          <div key={item.label} className="bg-slate-950/85 px-5 py-4">
            <div className="flex items-center gap-2 text-[11px] uppercase tracking-[0.22em] text-slate-500">
              <item.icon className="h-3.5 w-3.5" />
              {item.label}
            </div>
            <div className="mt-3 text-lg font-semibold text-slate-100">{item.value}</div>
            <div className="mt-1 text-sm text-slate-400">{item.detail}</div>
          </div>
        ))}
      </div>
    </Card>
  )
}
