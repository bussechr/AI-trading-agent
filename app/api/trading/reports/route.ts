import { NextResponse } from "next/server"

const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:5000"

export async function GET() {
  try {
    const response = await fetch(`${BRIDGE_URL}/reports`, {
      cache: "no-store",
    })

    if (!response.ok) {
      throw new Error(`Bridge returned ${response.status}`)
    }

    const data = await response.json()

    return NextResponse.json({
      status: "success",
      reports: data.reports || [],
    })
  } catch (error) {
    console.error("[v0] Failed to fetch reports:", error)
    return NextResponse.json(
      {
        status: "error",
        error: "Failed to connect to MT4 bridge",
        reports: [],
      },
      { status: 503 },
    )
  }
}
