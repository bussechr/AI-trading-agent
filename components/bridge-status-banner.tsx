"use client"

import { useState } from "react"
import { AlertCircle, SignalHigh, Terminal } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import type { LiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { formatAgeSeconds } from "@/lib/trading/live-state"

interface BridgeStatusBannerProps {
  state: LiveBridgeState | null
  error: string | null
  bridgeUrl?: string
}

export function BridgeStatusBanner({ state, error, bridgeUrl = "http://127.0.0.1:58710" }: BridgeStatusBannerProps) {
  const [showInstructions, setShowInstructions] = useState(false)

  if (!state || state.statusTier === "bridge_up_mt4_live") return null

  const bridgeDown = state.statusTier === "bridge_down"
  const title = bridgeDown ? "Bridge Control Plane Unreachable" : "Bridge Reachable, MT4 Feed Stale"
  const description = bridgeDown
    ? error || "The dashboard cannot reach the FastAPI bridge, so live state is unavailable."
    : `Bridge is up, but heartbeat or tick freshness is stale. Tick reason: ${String(state.tickReason || "unknown")}.`

  return (
    <Alert variant={bridgeDown ? "destructive" : "default"} className="mb-6 border-amber-500/20 bg-amber-500/8">
      {bridgeDown ? <AlertCircle className="h-4 w-4" /> : <SignalHigh className="h-4 w-4" />}
      <AlertTitle>{title}</AlertTitle>
      <AlertDescription className="mt-2 space-y-2">
        <p>{description}</p>
        <p className="text-sm text-muted-foreground">
          Bridge: <span className="font-mono text-foreground">{bridgeUrl}</span>
          {" | "}
          Heartbeat: <span className="text-foreground">{formatAgeSeconds(state.heartbeatAgeSecs)}</span>
          {" | "}
          Ticks: <span className="text-foreground">{String(state.tickStatus || "unknown")}</span>
        </p>

        {showInstructions ? (
          <div className="mt-4 space-y-3 rounded-2xl border border-border/70 bg-background/70 p-4 font-mono text-sm">
            <div>
              <div className="mb-1 text-xs text-muted-foreground">1. Start the supported live stack</div>
              <code className="block rounded-lg bg-slate-950 p-2 text-slate-100">launch_all.bat live 10000</code>
            </div>
            <div>
              <div className="mb-1 text-xs text-muted-foreground">2. Check the stack status</div>
              <code className="block rounded-lg bg-slate-950 p-2 text-slate-100">launch_all.bat status</code>
            </div>
            <div>
              <div className="mb-1 text-xs text-muted-foreground">3. Reset local services if the bridge is still stale</div>
              <code className="block rounded-lg bg-slate-950 p-2 text-slate-100">launch_all.bat stop</code>
            </div>
            <div>
              <div className="mb-1 text-xs text-muted-foreground">4. MT4 terminal checks</div>
              <code className="block rounded-lg bg-slate-950 p-2 text-slate-100">
                Enable AutoTrading, attach BridgeEA, confirm DLL/WebRequest permissions, then relaunch.
              </code>
            </div>
          </div>
        ) : (
          <Button variant="outline" size="sm" onClick={() => setShowInstructions(true)} className="mt-2">
            <Terminal className="mr-2 h-3 w-3" />
            Show Recovery Steps
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
