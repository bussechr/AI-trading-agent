import { NextResponse } from "next/server"

// In-memory state storage (use Redis/Upstash in production)
declare global {
  var tradingState: any
}

export async function GET() {
  try {
    const state = global.tradingState || {
      isRunning: false,
      systemStatus: "waiting_for_data",
      equity: 0,
      positions: [],
      agentDecisions: [],
      lastHeartbeat: null,
      cycleActive: false,
      cycleStartEquity: 0,
      cycleTarget: 0,
      signalsSent: 0,
      tradesExecuted: 0,
      lastSignal: null,
    }

    // Check if data is stale (no update in 30 seconds)
    const isStale = state.lastUpdate && Date.now() - state.lastUpdate > 30000

    return NextResponse.json({
      status: "success",
      data: {
        ...state,
        isRunning: !isStale && state.systemStatus === "connected",
        systemStatus: isStale ? "stale" : state.systemStatus,
      },
    })
  } catch (error: any) {
    console.error("[v0] Failed to read trading state:", error)
    return NextResponse.json(
      {
        status: "error",
        error: error.message,
        data: {
          isRunning: false,
          systemStatus: "error",
          equity: 0,
          positions: [],
          agentDecisions: [],
        },
      },
      { status: 500 },
    )
  }
}
