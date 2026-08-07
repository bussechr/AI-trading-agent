import { NextRequest, NextResponse } from "next/server"
import {
  fetchBridgeJsonBatchPinned,
  NO_STORE_RESPONSE_HEADERS,
  parseBoundedInt,
  requireBridgeObject,
  requireBridgeRecordArrayField,
  type BridgePinnedBatchItem,
} from "@/lib/server/bridge"

export const dynamic = "force-dynamic"
export const revalidate = 0

type OpsSlice = Record<string, unknown>

function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

function normalizeSlice(
  item: BridgePinnedBatchItem | undefined,
  label: string,
  normalize: (payload: unknown) => OpsSlice,
): OpsSlice {
  if (!item) return { status: "error", error: `${label} response is missing` }
  if (!item.ok) return { status: "error", error: item.error || `${label} unavailable` }
  try {
    return { ...normalize(item.payload), status: "success" }
  } catch (error: unknown) {
    const reason = error instanceof Error ? error.message : String(error)
    return { status: "error", error: reason || `${label} returned a malformed payload` }
  }
}

export async function GET(request: NextRequest) {
  const workflowsLimit = parseBoundedInt(request.nextUrl.searchParams.get("workflows_limit"), 200, 1, 5000)
  const eventsLimit = parseBoundedInt(request.nextUrl.searchParams.get("events_limit"), 300, 1, 5000)

  try {
    const batch = await fetchBridgeJsonBatchPinned(
      ["/v2/state"],
      [
        {
          key: "workflows",
          paths: [`/v2/ops/workflows/status?limit=${workflowsLimit}`, "/v2/ops/workflows/status"],
        },
        { key: "events", paths: [`/v2/ops/events?limit=${eventsLimit}`, "/v2/ops/events"] },
      ],
    )

    const workflows = normalizeSlice(batch.results.workflows, "workflow status", (payload) => {
      const base = requireBridgeObject(payload, "workflow status payload")
      const lifecycleCapabilities =
        asObject(base.lifecycle_capabilities).lifecycle_capabilities
          ? asObject(base.lifecycle_capabilities)
          : asObject(base.lifecycle_capabilities || base.lifecycle_capability_snapshot || base.capabilities)
      return {
        ...base,
        workflows: requireBridgeRecordArrayField(base, "workflows"),
        lifecycle_capabilities: lifecycleCapabilities,
        training_eval_reports: base.training_eval_reports || {},
        failure_cluster_summary: base.failure_cluster_summary || null,
        drift_explainability: base.drift_explainability || null,
      }
    })
    const events = normalizeSlice(batch.results.events, "ops events", (payload) => {
      const base = requireBridgeObject(payload, "ops events payload")
      return {
        ...base,
        events: requireBridgeRecordArrayField(base, "events"),
      }
    })

    return NextResponse.json(
      {
        status: "success",
        bridgeUrl: batch.baseUrl,
        sources: { workflows, events },
      },
      { headers: NO_STORE_RESPONSE_HEADERS },
    )
  } catch (error: unknown) {
    const reason = error instanceof Error ? error.message : String(error)
    return NextResponse.json(
      {
        status: "error",
        error: reason || "Unable to select a bridge for ops telemetry",
        bridgeUrl: null,
        sources: null,
      },
      {
        status: 503,
        headers: NO_STORE_RESPONSE_HEADERS,
      },
    )
  }
}
