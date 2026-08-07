import { NextResponse } from "next/server"
import {
  fetchBridgeJsonWithSource,
  NO_STORE_RESPONSE_HEADERS,
  parseBoundedInt,
  requireBridgeRecordArrayField,
} from "@/lib/server/bridge"
import { summarizeClosedTrades } from "@/lib/trading/closed-trades-normalize"
import { ageSecsFromTimestamp } from "@/lib/trading/freshness"

export const dynamic = "force-dynamic"
export const revalidate = 0

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const limit = parseBoundedInt(searchParams.get("limit"), 200, 1, 1000)
    const result = await fetchBridgeJsonWithSource<any>([`/v2/closed-trades?limit=${limit}`])
    const payload = result.payload
    const trades = requireBridgeRecordArrayField(payload, "trades")
    const now = Date.now()
    const summary = summarizeClosedTrades(trades, (trade) => {
      const closeAgeSecs = ageSecsFromTimestamp(trade?.close_time ?? trade?.close_time_epoch, now)
      return closeAgeSecs !== null && closeAgeSecs <= 24 * 60 * 60
    })

    return NextResponse.json(
      {
        status: "success",
        bridgeUrl: result.baseUrl,
        trades,
        summary,
      },
      { headers: NO_STORE_RESPONSE_HEADERS },
    )
  } catch (error: any) {
    return NextResponse.json(
      {
        status: "error",
        error: error?.message || "Closed-trade history unavailable",
        bridgeUrl: null,
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
      { status: 503, headers: NO_STORE_RESPONSE_HEADERS },
    )
  }
}
