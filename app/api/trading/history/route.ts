import { NextResponse } from "next/server"
import {
  fetchBridgeJsonBatchPinned,
  parseBoundedInt,
  requireBridgeObject,
  requireBridgeRecordArrayField,
  type BridgePinnedBatchItem,
} from "@/lib/server/bridge"

type HistorySlice = Record<string, any>

function normalizeSlice(
  item: BridgePinnedBatchItem | undefined,
  label: string,
  normalize: (payload: unknown) => HistorySlice,
): HistorySlice {
  if (!item) return { status: "error", error: `${label} response is missing` }
  if (!item.ok) return { status: "error", error: item.error || `${label} unavailable` }
  try {
    return { status: "success", ...normalize(item.payload) }
  } catch (error: unknown) {
    const reason = error instanceof Error ? error.message : String(error)
    return { status: "error", error: reason || `${label} returned a malformed payload` }
  }
}

export async function GET(request: Request) {
  try {
    const { searchParams } = new URL(request.url)
    const reportsLimit = parseBoundedInt(searchParams.get("reports_limit"), 5000, 1, 5000)
    const commandsLimit = parseBoundedInt(searchParams.get("commands_limit"), 500, 1, 5000)
    const eventsLimit = parseBoundedInt(searchParams.get("events_limit"), 500, 1, 10000)
    const governanceLimit = parseBoundedInt(searchParams.get("governance_limit"), 500, 1, 2000)

    const batch = await fetchBridgeJsonBatchPinned(
      ["/v2/state"],
      [
        { key: "metrics", paths: ["/v2/metrics"] },
        { key: "reports", paths: [`/v2/reports?limit=${reportsLimit}`] },
        { key: "commands", paths: [`/v2/commands/history?limit=${commandsLimit}`] },
        { key: "commandEvents", paths: [`/v2/commands/events?limit=${eventsLimit}`] },
        { key: "governance", paths: [`/v2/governance/events?limit=${governanceLimit}`] },
      ],
    )

    const sources = {
      metrics: normalizeSlice(batch.results.metrics, "metrics", (payload) => ({
        data: requireBridgeObject(payload, "metrics payload"),
      })),
      reports: normalizeSlice(batch.results.reports, "reports", (payload) => ({
        reports: requireBridgeRecordArrayField(payload, "reports"),
      })),
      commands: normalizeSlice(batch.results.commands, "commands", (payload) => ({
        commands: requireBridgeRecordArrayField(payload, "commands"),
      })),
      commandEvents: normalizeSlice(batch.results.commandEvents, "command events", (payload) => ({
        events: requireBridgeRecordArrayField(payload, "events"),
      })),
      governance: normalizeSlice(batch.results.governance, "governance", (payload) => ({
        events: requireBridgeRecordArrayField(payload, "events"),
      })),
    }

    return NextResponse.json({
      status: "success",
      bridgeUrl: batch.baseUrl,
      sources,
    })
  } catch (error: unknown) {
    const reason = error instanceof Error ? error.message : String(error)
    return NextResponse.json(
      {
        status: "error",
        error: reason || "Unable to select a bridge for trading history",
        bridgeUrl: null,
        sources: null,
      },
      { status: 503 },
    )
  }
}
