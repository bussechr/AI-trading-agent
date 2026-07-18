import { NextResponse } from "next/server"
import { fetchBridgeJson, parseBoundedInt, requireBridgeRecordArrayField } from "@/lib/server/bridge"
import { ageSecsFromTimestamp } from "@/lib/trading/freshness"

function asFiniteNumber(value: any, fallback = 0): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const limit = parseBoundedInt(searchParams.get("limit"), 200, 1, 1000)
    const payload: any = await fetchBridgeJson([`/v2/closed-trades?limit=${limit}`])
    const trades = requireBridgeRecordArrayField(payload, "trades")
    const now = Date.now()

    let realizedNet = 0
    let wins = 0
    let losses = 0
    let realizedNet24h = 0
    let wins24h = 0
    let losses24h = 0

    for (const trade of trades) {
      const net = asFiniteNumber(trade?.net_profit)
      const closeAgeSecs = ageSecsFromTimestamp(trade?.close_time ?? trade?.close_time_epoch, now)
      realizedNet += net
      if (net > 0) wins += 1
      else if (net < 0) losses += 1
      if (closeAgeSecs !== null && closeAgeSecs <= 24 * 60 * 60) {
        realizedNet24h += net
        if (net > 0) wins24h += 1
        else if (net < 0) losses24h += 1
      }
    }

    const closedTrades = wins + losses
    const closedTrades24h = wins24h + losses24h

    return NextResponse.json({
      status: "success",
      trades,
      summary: {
        closedTrades,
        wins,
        losses,
        winRate: closedTrades > 0 ? (wins / closedTrades) * 100 : null,
        realizedNet,
        averageNet: closedTrades > 0 ? realizedNet / closedTrades : null,
        closedTrades24h,
        wins24h,
        losses24h,
        winRate24h: closedTrades24h > 0 ? (wins24h / closedTrades24h) * 100 : null,
        realizedNet24h,
        averageNet24h: closedTrades24h > 0 ? realizedNet24h / closedTrades24h : null,
      },
    })
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Closed-trade history unavailable",
        trades: [],
        summary: {
          closedTrades: 0,
          wins: 0,
          losses: 0,
          winRate: null,
          realizedNet: 0,
          averageNet: null,
          closedTrades24h: 0,
          wins24h: 0,
          losses24h: 0,
          winRate24h: null,
          realizedNet24h: 0,
          averageNet24h: null,
        },
      },
      { status: 503 },
    )
  }
}
