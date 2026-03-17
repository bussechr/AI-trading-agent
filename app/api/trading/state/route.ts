import { NextResponse } from "next/server"
import { fetchBridgeJson } from "@/lib/server/bridge"

function toMs(value: any): number {
  if (value === null || value === undefined) return 0
  if (typeof value === "number") {
    return value > 10_000_000_000 ? value : value * 1000
  }
  const parsed = Date.parse(String(value))
  return Number.isFinite(parsed) ? parsed : 0
}

export async function GET() {
  try {
    const raw = await fetchBridgeJson(["/v2/state"])

    const lastUpdateMs = toMs(raw?.last_update)
    const isStale = lastUpdateMs > 0 && Date.now() - lastUpdateMs > 30000
    const systemStatus = isStale ? "stale" : String(raw?.system_status || raw?.systemStatus || "unknown")

    const data = {
      isRunning: !isStale && systemStatus === "connected",
      systemStatus,
      equity: Number(raw?.equity || 0),
      positions: Array.isArray(raw?.positions) ? raw.positions : [],
      agentDecisions: Array.isArray(raw?.agent_decisions)
        ? raw.agent_decisions
        : Array.isArray(raw?.agentDecisions)
          ? raw.agentDecisions
          : [],
      lastHeartbeat: raw?.last_heartbeat || raw?.lastHeartbeat || null,
      cycleActive: Boolean(raw?.cycle_active || raw?.cycleActive || false),
      cycleStartEquity: Number(raw?.cycle_start_equity || raw?.cycleStartEquity || 0),
      cycleTarget: Number(raw?.cycle_target || raw?.cycleTarget || 0),
      signalsSent: Number(raw?.signals_sent || raw?.signalsSent || 0),
      tradesExecuted: Number(raw?.trades_executed || raw?.tradesExecuted || 0),
      lastSignal: raw?.last_signal || raw?.lastSignal || null,
      lastAck: raw?.last_ack || raw?.lastAck || null,
      monitor: raw?.monitor || null,
      governance: raw?.governance || null,
      riskEnvelope: raw?.risk_envelope || raw?.riskEnvelope || null,
      lastUpdate: raw?.last_update || raw?.lastUpdate || null,
    }

    return NextResponse.json({ status: "success", data })
  } catch (error: any) {
    console.error("[api/trading/state] Failed to fetch state:", error)
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Failed to fetch state",
        data: {
          isRunning: false,
          systemStatus: "error",
          equity: 0,
          positions: [],
          agentDecisions: [],
        },
      },
      { status: 503 },
    )
  }
}
