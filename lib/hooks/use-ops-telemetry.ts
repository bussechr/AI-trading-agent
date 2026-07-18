"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"
import {
  normalizeAITrainingTelemetryWithLastGood,
  type AITrainingSourcePayloads,
  type AITrainingViewModel,
} from "@/lib/trading/ai-training-normalize"

export type OpsTelemetryStatus = "loading" | "live" | "stale" | "degraded" | "idle"

export interface OpsTelemetryState {
  data: AITrainingViewModel | null
  loading: boolean
  error: string | null
  stale: boolean
  updatedAt: number | null
  status: OpsTelemetryStatus
}

interface OpsTelemetrySnapshot extends OpsTelemetryState {
  sources: AITrainingSourcePayloads
}

async function fetchJson(path: string): Promise<any> {
  const response = await fetch(path, { cache: "no-store" })
  const payload = await response.json()
  if (!response.ok) {
    const reason = String(payload?.error || payload?.detail || "").trim()
    throw new Error(`${path} -> HTTP ${response.status}${reason ? `: ${reason}` : ""}`)
  }
  if (payload?.status === "error") {
    throw new Error(`${path} -> ${String(payload.error || "error response")}`)
  }
  return payload
}

const useSharedOpsTelemetry = createSharedPollingHook<OpsTelemetrySnapshot>({
  initialSnapshot: {
    data: null,
    loading: true,
    error: null,
    stale: false,
    updatedAt: null,
    status: "loading",
    sources: { workflows: null, events: null },
  },
  poll: async (current) => {
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
        const normalized = normalizeAITrainingTelemetryWithLastGood(current.sources, {}, now)
        return {
          data: normalized.data.summary.has_content ? normalized.data : current.data,
          loading: false,
          error: `Ops endpoints unavailable: ${wfErr || "workflow status"}; ${evErr || "ops events"}`,
          stale: true,
          updatedAt: now,
          status: "degraded",
          sources: normalized.sources,
        }
      }

      const normalized = normalizeAITrainingTelemetryWithLastGood(
        current.sources,
        {
          ...(workflowsOk ? { workflows: workflowsRes.value } : {}),
          ...(eventsOk ? { events: eventsRes.value } : {}),
        },
        now,
      )
      const data = normalized.data
      const hasContent = Boolean(data.summary.has_content)
      const stale = Boolean(
        hasContent &&
          (data.summary.last_update_age_sec === null || data.summary.last_update_age_sec > 20),
      )
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
        sources: normalized.sources,
      }
    } catch (err: any) {
      return {
        data: current.data,
        loading: false,
        error: err?.message || "Ops telemetry polling failed",
        stale: true,
        updatedAt: now,
        status: "degraded",
        sources: current.sources,
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
