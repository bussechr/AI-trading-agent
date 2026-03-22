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

function asFiniteNumber(value: any): number | null {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function pickFirstFinite(values: any[], fallback = 0): number {
  for (const value of values) {
    if (value === null || value === undefined) continue
    const n = Number(value)
    if (Number.isFinite(n)) return n
  }
  return fallback
}

function normalizeSide(raw: any): string {
  const txt = String(raw ?? "").trim().toUpperCase()
  if (txt === "BUY" || txt === "SELL") return txt
  if (txt === "LONG") return "BUY"
  if (txt === "SHORT") return "SELL"
  return "N/A"
}

function normalizeDecision(raw: any) {
  const row = raw && typeof raw === "object" ? raw : {}
  const metadata = row.metadata && typeof row.metadata === "object" ? row.metadata : {}
  const thresholdSnapshot =
    metadata.threshold_snapshot && typeof metadata.threshold_snapshot === "object" ? metadata.threshold_snapshot : {}
  const reasons = Array.isArray(row.reasons) ? row.reasons : []
  const symbol = String(row.symbol || metadata.pair || "N/A").toUpperCase()
  const score = asFiniteNumber(row.score)
  const price = asFiniteNumber(row.price ?? metadata.price ?? metadata.mid ?? metadata.bid ?? metadata.ask)
  const targetPct = asFiniteNumber(row.target_pct ?? metadata.target_pct)
  const spreadBps = asFiniteNumber(row.spread_bps ?? metadata.spread_bps)
  const maxSpreadBps = asFiniteNumber(thresholdSnapshot.max_spread_bps ?? thresholdSnapshot.max_allowed_spread_bps)
  const executionReady = Boolean(
    row.execution_ready ?? row.executionReady ?? metadata.execution_ready ?? metadata.allowed ?? false,
  )
  const reason = String(row.reason || reasons[0] || metadata.rejection_reason || "none")
  return {
    symbol,
    side: normalizeSide(row.side),
    score,
    price,
    target_pct: targetPct,
    spread_bps: spreadBps,
    max_spread_bps: maxSpreadBps,
    reason,
    execution_ready: executionReady,
  }
}

export async function GET() {
  try {
    const raw = await fetchBridgeJson(["/v2/state"])
    const monitorEmbedded = raw?.monitor && typeof raw.monitor === "object"
    const monitor = monitorEmbedded ? null : await fetchBridgeJson(["/v2/monitor"]).catch(() => null)

    const heartbeatStaleAfterSecs = Math.max(1, asFiniteNumber(raw?.heartbeat_stale_after_secs) || 30)
    const lastHeartbeat = raw?.last_heartbeat || raw?.lastHeartbeat || null
    const heartbeatAgeFromState = asFiniteNumber(raw?.heartbeat_age_secs ?? raw?.heartbeatAgeSecs)
    const heartbeatAgeFromTs =
      lastHeartbeat && toMs(lastHeartbeat) > 0 ? Math.max(0, (Date.now() - toMs(lastHeartbeat)) / 1000) : null
    const heartbeatAgeSecs = heartbeatAgeFromState ?? heartbeatAgeFromTs

    const statusRaw = String(raw?.system_status || raw?.systemStatus || "unknown").trim().toLowerCase()
    const mt4Connected = statusRaw === "connected"
    const mt4FreshByHeartbeat = heartbeatAgeSecs !== null && heartbeatAgeSecs <= heartbeatStaleAfterSecs
    const mt4Fresh = mt4Connected && mt4FreshByHeartbeat
    const ticksFresh = typeof raw?.ticks_fresh === "boolean" ? Boolean(raw?.ticks_fresh) : mt4Fresh
    const signalDataFresh = mt4Fresh && ticksFresh
    const isStale = !mt4Fresh || !ticksFresh
    const bridgeState = "bridge_up"
    const statusTier = mt4Fresh && ticksFresh ? "bridge_up_mt4_live" : "bridge_up_mt4_stale"

    let systemStatus = statusRaw || "unknown"
    if (mt4Connected && !mt4FreshByHeartbeat) {
      systemStatus = "stale"
    }
    if (mt4Connected && mt4FreshByHeartbeat && !ticksFresh) {
      systemStatus = "stale"
    }
    if (!mt4Connected && systemStatus === "connected") {
      systemStatus = "disconnected"
    }

    const liveEquity = pickFirstFinite(
      [
        raw?.mt4_equity,
        raw?.mt4Equity,
        raw?.account_equity,
        raw?.accountEquity,
        raw?.monitor?.account?.equity,
        monitor?.account?.equity,
        raw?.equity,
      ],
      0,
    )
    const equity = mt4Fresh ? liveEquity : 0
    const margin = pickFirstFinite([raw?.margin, raw?.monitor?.account?.margin, monitor?.account?.margin], 0)
    const freeMargin = pickFirstFinite(
      [raw?.freemargin, raw?.free_margin, raw?.monitor?.account?.freemargin, monitor?.account?.freemargin],
      0,
    )
    const decisionsRaw = Array.isArray(raw?.agent_decisions)
      ? raw.agent_decisions
      : Array.isArray(raw?.agentDecisions)
        ? raw.agentDecisions
        : []
    const agentDecisions = signalDataFresh ? decisionsRaw.map(normalizeDecision) : []

    const data = {
      isRunning: mt4Connected && mt4Fresh && ticksFresh,
      bridgeState,
      statusTier,
      mt4Connected,
      mt4Fresh,
      isStale,
      signalDataFresh,
      systemStatus,
      heartbeatStaleAfterSecs,
      equity,
      displayEquity: mt4Fresh && ticksFresh ? liveEquity : null,
      cachedEquity: mt4Fresh ? null : liveEquity,
      margin,
      freemargin: freeMargin,
      positions: Array.isArray(raw?.positions) ? raw.positions : [],
      agentDecisions,
      tickStatus: String(raw?.tick_status || "unknown"),
      tickReason: String(raw?.tick_reason || "unknown"),
      tickSymbolsCount: Number(raw?.tick_symbols_count || 0),
      tickMaxAgeSecs: asFiniteNumber(raw?.tick_max_age_secs),
      signalDataReason: String(raw?.tick_reason || raw?.tick_status || (signalDataFresh ? "fresh" : "stale")),
      lastHeartbeat,
      heartbeatAgeSecs: heartbeatAgeSecs ?? null,
      cycleActive: Boolean(raw?.cycle_active || raw?.cycleActive || false),
      cycleStartEquity: Number(raw?.cycle_start_equity || raw?.cycleStartEquity || 0),
      cycleTarget: Number(raw?.cycle_target || raw?.cycleTarget || 0),
      signalsSent: Number(raw?.signals_sent || raw?.signalsSent || 0),
      tradesExecuted: Number(raw?.trades_executed || raw?.tradesExecuted || 0),
      lastSignal: raw?.last_signal || raw?.lastSignal || null,
      lastAck: raw?.last_ack || raw?.lastAck || null,
      monitor: raw?.monitor || monitor?.monitor || null,
      governance: raw?.governance || null,
      riskEnvelope: raw?.risk_envelope || raw?.riskEnvelope || null,
      runtimeDiag: raw?.runtime_diag || null,
      runtimeStatus: String(raw?.runtime_status || raw?.runtimeStatus || "unknown"),
      lastUpdate: raw?.last_update || raw?.lastUpdate || null,
      equitySource:
        !mt4Fresh
          ? "stale_or_missing_heartbeat"
          : raw?.mt4_equity !== undefined || raw?.mt4Equity !== undefined
          ? "mt4_equity"
          : raw?.account_equity !== undefined || raw?.accountEquity !== undefined
            ? "account_equity"
            : raw?.monitor?.account?.equity !== undefined || monitor?.account?.equity !== undefined
              ? "monitor.account.equity"
              : "equity",
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
          bridgeState: "bridge_down",
          statusTier: "bridge_down",
          mt4Connected: false,
          mt4Fresh: false,
          isStale: true,
          signalDataFresh: false,
          signalDataReason: "state_proxy_error",
          tickStatus: "unknown",
          tickReason: "state_proxy_error",
          tickSymbolsCount: 0,
          tickMaxAgeSecs: null,
          runtimeStatus: "error",
          heartbeatStaleAfterSecs: 30,
          heartbeatAgeSecs: null,
          displayEquity: null,
          cachedEquity: null,
          lastHeartbeat: null,
          systemStatus: "error",
          equity: 0,
          positions: [],
          agentDecisions: [],
          cycleActive: false,
          cycleStartEquity: 0,
          cycleTarget: 0,
          signalsSent: 0,
          tradesExecuted: 0,
          lastSignal: null,
        },
      },
      { status: 503 },
    )
  }
}
