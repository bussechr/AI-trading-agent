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
      value: formatLatency(state?.runtimeDiag?.loop_latency_ms),
      detail: `${Number(state?.tickSymbolsCount || 0)} symbols tracked`,
      icon: RefreshCcw,
    },
    {
      label: "AI Ops",
      value: ops.status,
      detail: ops.data ? `${ops.data.summary.workflows_total} workflows` : "no ops snapshot",
      icon: Bot,
    },
    {
      label: "Signals",
      value: loading ? "..." : `${Number(state?.signalsSent || 0)}`,
      detail: loading ? "loading" : `${Number(state?.tradesExecuted || 0)} executions`,
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
