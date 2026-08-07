"use client"

import { createSharedPollingHook } from "@/lib/hooks/shared-polling-hook"
import {
  mergePinnedAITrainingSnapshot,
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
  bridgeUrl: string | null
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
    bridgeUrl: null,
    sources: { workflows: null, events: null },
  },
  poll: async (current) => {
    const now = Date.now()
    try {
      const payload = await fetchJson("/api/trading/ops?workflows_limit=200&events_limit=300")
      const merged = mergePinnedAITrainingSnapshot(
        { sources: current.sources, bridgeUrl: current.bridgeUrl },
        payload,
        now,
      )
      const data = merged.data
      const hasContent = Boolean(data.summary.has_content)
      const stale = Boolean(
        hasContent &&
          (data.summary.last_update_age_sec === null || data.summary.last_update_age_sec > 20),
      )
      const error = merged.error

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
        bridgeUrl: merged.bridgeUrl,
        sources: merged.sources,
      }
    } catch (err: any) {
      const error = err?.message || "Ops telemetry polling failed"
      const merged = mergePinnedAITrainingSnapshot(
        { sources: current.sources, bridgeUrl: current.bridgeUrl },
        { status: "error", error },
        now,
      )
      return {
        data: merged.data.summary.has_content ? merged.data : current.data,
        loading: false,
        error,
        stale: true,
        updatedAt: now,
        status: "degraded",
        bridgeUrl: merged.bridgeUrl,
        sources: merged.sources,
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
