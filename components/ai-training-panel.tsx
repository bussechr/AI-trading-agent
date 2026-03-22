"use client"

import { BrainCircuit, CheckCircle2, CircleAlert, LoaderCircle, PauseCircle, Radar } from "lucide-react"
import { Card } from "@/components/ui/card"
import { Badge } from "@/components/ui/badge"
import { useOpsTelemetry } from "@/lib/hooks/use-ops-telemetry"
import { cn } from "@/lib/utils"

function formatAge(seconds: number | null): string {
  if (seconds === null) return "n/a"
  if (seconds < 60) return `${seconds}s ago`
  const mins = Math.floor(seconds / 60)
  if (mins < 60) return `${mins}m ago`
  const hrs = Math.floor(mins / 60)
  return `${hrs}h ago`
}

function formatTime(ms: number | null): string {
  if (!ms) return "n/a"
  return `${new Date(ms).toLocaleTimeString()} · ${formatAge(Math.max(0, Math.floor((Date.now() - ms) / 1000)))}`
}

function formatStatusLabel(value: string): string {
  const txt = String(value || "").trim()
  if (!txt) return "Unknown"
  return txt
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (match) => match.toUpperCase())
}

function opsStatusMeta(status: ReturnType<typeof useOpsTelemetry>["status"]) {
  switch (status) {
    case "live":
      return {
        label: "Live",
        className: "border-emerald-500/30 bg-emerald-500/10 text-emerald-300",
        Icon: CheckCircle2,
      }
    case "stale":
      return {
        label: "Stale",
        className: "border-amber-500/30 bg-amber-500/10 text-amber-200",
        Icon: PauseCircle,
      }
    case "degraded":
      return {
        label: "Degraded",
        className: "border-rose-500/30 bg-rose-500/10 text-rose-200",
        Icon: CircleAlert,
      }
    case "loading":
      return {
        label: "Loading",
        className: "border-sky-500/30 bg-sky-500/10 text-sky-200",
        Icon: LoaderCircle,
      }
    case "idle":
    default:
      return {
        label: "Idle",
        className: "border-slate-500/30 bg-slate-500/10 text-slate-300",
        Icon: Radar,
      }
  }
}

export function AITrainingPanel() {
  const { data, loading, error, updatedAt, status } = useOpsTelemetry(5000)
  const meta = opsStatusMeta(status)
  const workflows = data?.workflows || []
  const lifecycleRows = Object.entries(data?.lifecycle_capabilities || {})
  const events = data?.events || []

  return (
    <div className="space-y-6">
      <Card className="overflow-hidden border-slate-300/20 bg-slate-950 text-slate-100">
        <div className="border-b border-white/10 px-6 py-5">
          <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
            <div className="max-w-3xl">
              <div className="text-[11px] uppercase tracking-[0.24em] text-slate-500">AI Training</div>
              <h1 className="mt-2 text-4xl font-semibold text-white">Observe-only training, promotion, drift, and failure mining</h1>
              <p className="mt-3 text-sm text-slate-400">
                This surface is read-only. It reports model research activity and promotion evidence without granting any execution authority.
              </p>
            </div>
            <div className="flex items-center gap-3">
              <Badge variant="outline" className={cn("rounded-full px-3 py-1.5 text-xs font-medium", meta.className)}>
                <meta.Icon className={cn("mr-2 h-3.5 w-3.5", status === "loading" && "animate-spin")} />
                {meta.label}
              </Badge>
              <div className="text-right text-xs text-slate-400">
                <div>Refresh 5s</div>
                <div>{updatedAt ? `Updated ${new Date(updatedAt).toLocaleTimeString()}` : "Waiting for first sample"}</div>
              </div>
            </div>
          </div>
        </div>
        <div className="grid gap-px bg-white/8 lg:grid-cols-5">
          <div className="bg-slate-950/85 px-5 py-4">
            <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Workflows</div>
            <div className="mt-2 text-3xl font-semibold text-white">{loading ? "…" : data?.summary.workflows_total ?? 0}</div>
          </div>
          <div className="bg-slate-950/85 px-5 py-4">
            <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Running</div>
            <div className="mt-2 text-3xl font-semibold text-white">{loading ? "…" : data?.summary.running_count ?? 0}</div>
          </div>
          <div className="bg-slate-950/85 px-5 py-4">
            <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Failed</div>
            <div className="mt-2 text-3xl font-semibold text-white">{loading ? "…" : data?.summary.failed_count ?? 0}</div>
          </div>
          <div className="bg-slate-950/85 px-5 py-4">
            <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Last Update Age</div>
            <div className="mt-2 text-3xl font-semibold text-white">{loading ? "…" : formatAge(data?.summary.last_update_age_sec ?? null)}</div>
          </div>
          <div className="bg-slate-950/85 px-5 py-4">
            <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Pairs Full Lifecycle</div>
            <div className="mt-2 text-3xl font-semibold text-white">{loading ? "…" : data?.summary.pairs_with_full_lifecycle ?? 0}</div>
          </div>
        </div>
      </Card>

      {error && (
        <Card className="border-rose-500/20 bg-rose-500/8 p-4 text-sm text-rose-200">
          {error}
        </Card>
      )}

      <div className="grid gap-6 xl:grid-cols-[1.15fr_0.85fr]">
        <Card className="p-6">
          <div className="flex items-center justify-between gap-3">
            <div>
              <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Workflow Status Table</div>
              <h2 className="mt-2 text-2xl font-semibold text-foreground">Training runtime health</h2>
            </div>
            <BrainCircuit className="h-5 w-5 text-primary" />
          </div>
          <div className="mt-5 overflow-x-auto">
            <table className="w-full min-w-[760px] text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                  <th className="py-3 pr-3">Workflow</th>
                  <th className="py-3 pr-3">Type</th>
                  <th className="py-3 pr-3">Status</th>
                  <th className="py-3 pr-3">Updated</th>
                  <th className="py-3 pr-3">Training Refs</th>
                  <th className="py-3">Failure Cluster</th>
                </tr>
              </thead>
              <tbody>
                {workflows.length === 0 ? (
                  <tr>
                    <td className="py-5 text-muted-foreground" colSpan={6}>
                      {loading ? "Loading workflows…" : "No workflow telemetry available"}
                    </td>
                  </tr>
                ) : (
                  workflows.slice(0, 25).map((workflow) => (
                    <tr key={workflow.workflow_id} className="border-b border-border/60">
                      <td className="py-3 pr-3 font-medium text-foreground">{workflow.workflow_id}</td>
                      <td className="py-3 pr-3 text-muted-foreground">{workflow.workflow_type}</td>
                      <td className="py-3 pr-3">
                        <Badge variant="outline" className="capitalize">
                          {formatStatusLabel(workflow.status)}
                        </Badge>
                      </td>
                      <td className="py-3 pr-3 text-muted-foreground">{formatTime(workflow.updated_at_ms)}</td>
                      <td className="py-3 pr-3 text-foreground">{workflow.has_training_refs ? "yes" : "no"}</td>
                      <td className="py-3 text-foreground">{workflow.has_failure_cluster ? "yes" : "no"}</td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <div className="space-y-6">
          <Card className="p-6">
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Latest Training Results</div>
            <h2 className="mt-2 text-2xl font-semibold text-foreground">Promotion and challenger outcomes</h2>
            <div className="mt-5 space-y-3">
              {(data?.latest_results || []).length === 0 ? (
                <div className="text-sm text-muted-foreground">No promotion summaries yet.</div>
              ) : (
                (data?.latest_results || []).slice(0, 8).map((workflow) => (
                  <div key={`${workflow.workflow_id}-promotion`} className="rounded-3xl border border-border/70 bg-background/50 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div className="font-medium text-foreground">{workflow.workflow_id}</div>
                      <Badge variant="outline" className="capitalize">
                        {formatStatusLabel(workflow.promotion.status)}
                      </Badge>
                    </div>
                    <div className="mt-3 grid gap-2 text-sm sm:grid-cols-2">
                      <div className="rounded-2xl border border-border/60 px-3 py-2">
                        <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Candidate</div>
                        <div className="mt-1 font-mono text-foreground">{workflow.promotion.candidate_metric ?? "n/a"}</div>
                      </div>
                      <div className="rounded-2xl border border-border/60 px-3 py-2">
                        <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Champion</div>
                        <div className="mt-1 font-mono text-foreground">{workflow.promotion.champion_metric ?? "n/a"}</div>
                      </div>
                      <div className="rounded-2xl border border-border/60 px-3 py-2">
                        <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Delta</div>
                        <div className="mt-1 font-mono text-foreground">{workflow.promotion.delta ?? "n/a"}</div>
                      </div>
                      <div className="rounded-2xl border border-border/60 px-3 py-2">
                        <div className="text-[11px] uppercase tracking-[0.18em] text-muted-foreground">Report Refs</div>
                        <div className="mt-1 font-mono text-foreground">{workflow.promotion.report_ref_count}</div>
                      </div>
                    </div>
                  </div>
                ))
              )}
            </div>
          </Card>

          <Card className="p-6">
            <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Recent Ops Events</div>
            <h2 className="mt-2 text-2xl font-semibold text-foreground">Latest drift and workflow signals</h2>
            <div className="mt-5 space-y-3">
              {events.length === 0 ? (
                <div className="text-sm text-muted-foreground">No ops events yet.</div>
              ) : (
                events.slice(0, 8).map((event, index) => (
                  <div key={`${event.event_type}-${event.time_ms}-${index}`} className="rounded-3xl border border-border/70 bg-background/50 p-4">
                    <div className="flex items-center justify-between gap-3">
                      <div className="font-medium text-foreground">{event.event_type}</div>
                      <Badge variant="outline" className="capitalize">
                        {formatStatusLabel(event.status)}
                      </Badge>
                    </div>
                    <div className="mt-2 text-sm text-muted-foreground">{event.reason || "No message"}</div>
                    <div className="mt-2 text-xs text-muted-foreground">{formatTime(event.time_ms)}</div>
                  </div>
                ))
              )}
            </div>
          </Card>
        </div>
      </div>

      <div className="grid gap-6 xl:grid-cols-2">
        <Card className="p-6">
          <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Failure and Drift Signals</div>
          <h2 className="mt-2 text-2xl font-semibold text-foreground">Explainability surfaces</h2>
          <div className="mt-5 space-y-5">
            <div>
              <div className="text-sm font-medium text-foreground">Failure cluster summary</div>
              <div className="mt-2 rounded-3xl border border-border/70 bg-background/50 p-4 text-sm text-muted-foreground">
                {data?.failure_cluster_summary ? (
                  <pre className="whitespace-pre-wrap break-all text-xs text-foreground">
                    {JSON.stringify(data.failure_cluster_summary, null, 2)}
                  </pre>
                ) : (
                  "No drift/failure summaries yet."
                )}
              </div>
            </div>
            <div>
              <div className="text-sm font-medium text-foreground">Drift explainability</div>
              <div className="mt-2 rounded-3xl border border-border/70 bg-background/50 p-4 text-sm text-muted-foreground">
                {data?.drift_explainability ? (
                  <pre className="whitespace-pre-wrap break-all text-xs text-foreground">
                    {JSON.stringify(data.drift_explainability, null, 2)}
                  </pre>
                ) : (
                  "No drift/failure summaries yet."
                )}
              </div>
            </div>
          </div>
        </Card>

        <Card className="p-6">
          <div className="text-[11px] uppercase tracking-[0.22em] text-muted-foreground">Lifecycle Capability by Pair</div>
          <h2 className="mt-2 text-2xl font-semibold text-foreground">Coverage snapshot</h2>
          <div className="mt-5 overflow-x-auto">
            <table className="w-full min-w-[620px] text-sm">
              <thead>
                <tr className="border-b border-border text-left text-[11px] uppercase tracking-[0.18em] text-muted-foreground">
                  <th className="py-3 pr-3">Pair</th>
                  <th className="py-3 pr-3">Exit Model</th>
                  <th className="py-3 pr-3">Reversal Models</th>
                  <th className="py-3">Warnings</th>
                </tr>
              </thead>
              <tbody>
                {lifecycleRows.length === 0 ? (
                  <tr>
                    <td className="py-5 text-muted-foreground" colSpan={4}>
                      No lifecycle capability snapshot available.
                    </td>
                  </tr>
                ) : (
                  lifecycleRows.map(([pair, raw]) => {
                    const row = raw as Record<string, unknown>
                    const warnings = Array.isArray(row.warnings)
                      ? row.warnings.join(", ")
                      : Array.isArray(row.activation_warnings)
                        ? row.activation_warnings.join(", ")
                        : String(row.warnings || row.activation_warnings || row.warning || "n/a")
                    return (
                      <tr key={pair} className="border-b border-border/60">
                        <td className="py-3 pr-3 font-medium text-foreground">{pair}</td>
                        <td className="py-3 pr-3 text-foreground">{row.has_exit_model ? "yes" : "no"}</td>
                        <td className="py-3 pr-3 text-foreground">{row.has_reversal_models ? "yes" : "no"}</td>
                        <td className="py-3 text-muted-foreground">{warnings}</td>
                      </tr>
                    )
                  })
                )}
              </tbody>
            </table>
          </div>
        </Card>
      </div>
    </div>
  )
}
