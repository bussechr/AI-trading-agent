import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"

export async function GET() {
  try {
    const data = await fetchBridgeJson(["/v2/reports"])

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
