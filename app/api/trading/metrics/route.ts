import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"

export async function GET() {
  try {
    const payload = await fetchBridgeJson(["/v2/metrics"])

    return NextResponse.json({ status: "success", data: payload })
  } catch (error: any) {
    return NextResponse.json({ status: "error", error: error?.message || "Metrics unavailable", data: {} }, { status: 503 })
  }
}
