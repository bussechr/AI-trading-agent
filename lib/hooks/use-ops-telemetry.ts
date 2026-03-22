"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"
import { normalizeAITrainingTelemetry, type AITrainingViewModel } from "@/lib/trading/ai-training-normalize"

export type OpsTelemetryStatus = "loading" | "live" | "stale" | "degraded" | "idle"

export interface OpsTelemetryState {
  data: AITrainingViewModel | null
  loading: boolean
  error: string | null
  stale: boolean
  updatedAt: number | null
  status: OpsTelemetryStatus
}

async function fetchJson(path: string): Promise<any> {
  const response = await fetch(path, { cache: "no-store" })
  if (!response.ok) {
    throw new Error(`${path} -> HTTP ${response.status}`)
  }
  return response.json()
}

const useSharedOpsTelemetry = createSharedPollingHook<OpsTelemetryState>({
  initialSnapshot: {
    data: null,
    loading: true,
    error: null,
    stale: false,
    updatedAt: null,
    status: "loading",
  },
  poll: async () => {
    const now = Date.now()
    try {
      const [workflowsRes, eventsRes] = await Promise.allSettled([
        fetchJson("/api/trading/ops/workflows/status?limit=200"),
        fetchJson("/api/trading/ops/events?limit=300"),
      ])

      const workflowsOk = workflowsRes.status === "fulfilled"
      const eventsOk = eventsRes.status === "fulfilled"

      if (!workflowsOk && !eventsOk) {
        const wfErr = workflowsRes.status === "rejected" ? String(workflowsRes.reason?.message || workflowsRes.reason) : ""
        const evErr = eventsRes.status === "rejected" ? String(eventsRes.reason?.message || eventsRes.reason) : ""
        return {
          data: null,
          loading: false,
          error: `Ops endpoints unavailable: ${wfErr || "workflow status"}; ${evErr || "ops events"}`,
          stale: true,
          updatedAt: now,
          status: "degraded",
        }
      }

      const workflowsPayload = workflowsOk ? workflowsRes.value : {}
      const eventsPayload = eventsOk ? eventsRes.value : {}
      const data = normalizeAITrainingTelemetry(workflowsPayload, eventsPayload, now)
      const hasContent = Boolean(data.summary.has_content)
      const stale = Boolean(hasContent && data.summary.last_update_age_sec !== null && data.summary.last_update_age_sec > 20)
      const error =
        workflowsOk && eventsOk
          ? null
          : workflowsOk
            ? "Ops events endpoint degraded"
            : "Workflow status endpoint degraded"

      let status: OpsTelemetryStatus = "idle"
      if (error && hasContent) status = "degraded"
      else if (error) status = "degraded"
      else if (!hasContent) status = "idle"
      else if (stale) status = "stale"
      else status = "live"

      return {
        data,
        loading: false,
        error,
        stale,
        updatedAt: now,
        status,
      }
    } catch (err: any) {
      return {
        data: null,
        loading: false,
        error: err?.message || "Ops telemetry polling failed",
        stale: true,
        updatedAt: now,
        status: "degraded",
      }
    }
  },
})

export function useOpsTelemetry(refreshIntervalMs = 5000): OpsTelemetryState {
  const snapshot = useSharedOpsTelemetry(refreshIntervalMs)
  const hasContent = Boolean(snapshot.data?.summary?.has_content)
  if (snapshot.loading && !snapshot.data) return snapshot
  if (hasContent) return snapshot
  if (snapshot.error) return { ...snapshot, status: "degraded" }
  return { ...snapshot, status: "idle" }
}
