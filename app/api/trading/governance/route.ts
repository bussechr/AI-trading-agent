import { NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt } from "@/lib/server/bridge"

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const bounded = parseBoundedInt(searchParams.get("limit"), 200, 1, 2000)
    const payload: any = await fetchBridgeJson([`/v2/governance/events?limit=${bounded}`])
    return NextResponse.json({ status: "success", events: Array.isArray(payload?.events) ? payload.events : [] })
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Governance unavailable",
        events: [],
      },
      { status: 503 },
    )
  }
}
