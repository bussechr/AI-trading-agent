import { NextRequest, NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt, requireBridgeArrayField } from "@/lib/server/bridge"

export const dynamic = "force-dynamic"
export const revalidate = 0

function asObject(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {}
  return value as Record<string, unknown>
}

export async function GET(request: NextRequest) {
  const limit = parseBoundedInt(request.nextUrl.searchParams.get("limit"), 300, 1, 5000)

  try {
    const payload = await fetchBridgeJson([`/v2/ops/events?limit=${limit}`, "/v2/ops/events"])
    const base = asObject(payload)
    return NextResponse.json(
      {
        ...base,
        status: "success",
        events: requireBridgeArrayField(base, "events"),
      },
      {
        headers: {
          "Cache-Control": "no-store, max-age=0",
        },
      },
    )
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Failed to fetch ops events",
        events: [],
      },
      {
        status: 503,
        headers: {
          "Cache-Control": "no-store, max-age=0",
        },
      },
    )
  }
}
