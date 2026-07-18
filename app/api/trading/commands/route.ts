import { NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt, requireBridgeRecordArrayField } from "@/lib/server/bridge"

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const bounded = parseBoundedInt(searchParams.get("limit"), 200, 1, 5000)
    const payload: any = await fetchBridgeJson([`/v2/commands/history?limit=${bounded}`])
    return NextResponse.json({
      status: "success",
      commands: requireBridgeRecordArrayField(payload, "commands"),
    })
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Command history unavailable",
        commands: [],
      },
      { status: 503 },
    )
  }
}
