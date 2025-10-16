import { NextResponse } from "next/server"

declare global {
  var tradingState: any
}

export async function POST(request: Request) {
  try {
    const data = await request.json()

    // Store in global state (in production, use Redis/Upstash)
    global.tradingState = {
      ...data,
      lastUpdate: Date.now(),
    }

    console.log("[v0] Trading state updated:", {
      equity: data.equity,
      positions: data.positions?.length || 0,
      systemStatus: data.system_status,
    })

    return NextResponse.json({ success: true })
  } catch (error: any) {
    console.error("[v0] Failed to update trading state:", error)
    return NextResponse.json({ error: "Failed to update state" }, { status: 500 })
  }
}
