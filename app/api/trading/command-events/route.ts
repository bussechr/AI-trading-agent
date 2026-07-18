import { NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt, requireBridgeRecordArrayField } from "@/lib/server/bridge"

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const bounded = parseBoundedInt(searchParams.get("limit"), 500, 1, 10000)
    const commandId = String(searchParams.get("command_id") || "").trim()
    const query = commandId
      ? `limit=${bounded}&command_id=${encodeURIComponent(commandId)}`
      : `limit=${bounded}`

    const payload: any = await fetchBridgeJson([`/v2/commands/events?${query}`])
    return NextResponse.json({
      status: "success",
      events: requireBridgeRecordArrayField(payload, "events"),
    })
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Command lifecycle endpoint unavailable",
        events: [],
      },
      { status: 503 },
    )
  }
}
