import { NextResponse } from "next/server"
import { fetchBridgeJson, requireBridgeObject } from "@/lib/server/bridge"

export async function GET() {
  try {
    const payload = await fetchBridgeJson(["/v2/metrics"])

    return NextResponse.json({ status: "success", data: requireBridgeObject(payload, "metrics payload") })
  } catch (error: any) {
    return NextResponse.json({ status: "error", error: error?.message || "Metrics unavailable", data: {} }, { status: 503 })
  }
}
