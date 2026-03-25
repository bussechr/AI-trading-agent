import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"
import { parseBoundedInt } from "@/lib/server/bridge"

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const limit = parseBoundedInt(searchParams.get("limit"), 200, 1, 5000)
    const data = await fetchBridgeJson([`/v2/reports?limit=${limit}`])

    return NextResponse.json({
      status: "success",
      reports: data.reports || [],
    })
  } catch (error) {
    console.error("[api/trading/reports] Failed to fetch reports:", error)
    return NextResponse.json(
      {
        status: "error",
        error: "Failed to connect to MT4 bridge",
        reports: [],
      },
      { status: 503 },
    )
  }
}
