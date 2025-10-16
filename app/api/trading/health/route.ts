import { NextResponse } from "next/server"

const BRIDGE_URL = process.env.BRIDGE_URL || "http://127.0.0.1:5000"

export async function GET() {
  try {
    const response = await fetch(`${BRIDGE_URL}/health`, {
      cache: "no-store",
    })

    if (!response.ok) {
      throw new Error(`Bridge returned ${response.status}`)
    }

    const data = await response.json()

    return NextResponse.json({
      status: "success",
      ...data,
    })
  } catch (error) {
    return NextResponse.json(
      {
        status: "error",
        error: "Bridge not reachable",
      },
      { status: 503 },
    )
  }
}
