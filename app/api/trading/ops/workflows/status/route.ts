import { NextRequest, NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt } from "@/lib/server/bridge"

function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

export async function GET(request: NextRequest) {
  const limit = parseBoundedInt(request.nextUrl.searchParams.get("limit"), 200, 1, 5000)

  try {
    const payload = await fetchBridgeJson([
      `/v2/ops/workflows/status?limit=${limit}`,
      "/v2/ops/workflows/status",
    ])
    const base = asObject(payload)
    const workflows = Array.isArray(base.workflows) ? base.workflows : []
    const lifecycleCapabilities =
      asObject(base.lifecycle_capabilities).lifecycle_capabilities
        ? asObject(base.lifecycle_capabilities)
        : asObject(base.lifecycle_capabilities || base.lifecycle_capability_snapshot || base.capabilities)

    return NextResponse.json({
      ...base,
      status: "success",
      workflows,
      lifecycle_capabilities: lifecycleCapabilities,
      training_eval_reports: base.training_eval_reports || {},
      failure_cluster_summary: base.failure_cluster_summary || null,
      drift_explainability: base.drift_explainability || null,
    })
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Failed to fetch workflow status",
        workflows: [],
        lifecycle_capabilities: {},
        training_eval_reports: {},
        failure_cluster_summary: null,
        drift_explainability: null,
      },
      { status: 503 },
    )
  }
}
