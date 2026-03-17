import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"

export async function GET() {
  try {
    const data = await fetchBridgeJson(["/v2/health"])
    return NextResponse.json({ status: "success", ...data })
  } catch {
    return NextResponse.json({ status: "error", error: "Bridge not reachable" }, { status: 503 })
  }
}
