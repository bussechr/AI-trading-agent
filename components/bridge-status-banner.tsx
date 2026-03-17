"use client"

import { AlertCircle, Terminal } from "lucide-react"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import { useState } from "react"

interface BridgeStatusBannerProps {
  error: string | null
  bridgeUrl?: string
}

export function BridgeStatusBanner({ error, bridgeUrl = "http://127.0.0.1:58710" }: BridgeStatusBannerProps) {
  const [showInstructions, setShowInstructions] = useState(false)

  if (!error) return null

  return (
    <Alert variant="destructive" className="mb-6">
      <AlertCircle className="h-4 w-4" />
      <AlertTitle>MT4 Bridge Not Connected</AlertTitle>
      <AlertDescription className="mt-2 space-y-2">
        <p>The Python bridge server is not running. Start it to see live trading data.</p>

        {showInstructions ? (
          <div className="mt-4 space-y-3 rounded-lg bg-black/20 p-4 font-mono text-sm">
            <div>
              <div className="mb-1 text-xs text-muted-foreground">1. Start the bridge server:</div>
              <code className="block rounded bg-black/40 p-2">python -m src.trader.cli bridge serve</code>
            </div>

            <div>
              <div className="mb-1 text-xs text-muted-foreground">2. Start MT4 with BridgeEA:</div>
              <code className="block rounded bg-black/40 p-2">Open MT4 → Attach BridgeEA to any chart</code>
            </div>

            <div>
              <div className="mb-1 text-xs text-muted-foreground">3. Run the trading agent:</div>
              <code className="block rounded bg-black/40 p-2">
                python -m src.trader.cli runtime run --config src/config/fx_el_minis.yaml --equity 10000
              </code>
            </div>

            <div className="mt-3 text-xs text-muted-foreground">
              Bridge URL: <span className="text-foreground">{bridgeUrl}</span>
            </div>
          </div>
        ) : (
          <Button variant="outline" size="sm" onClick={() => setShowInstructions(true)} className="mt-2">
            <Terminal className="mr-2 h-3 w-3" />
            Show Setup Instructions
          </Button>
        )}
      </AlertDescription>
    </Alert>
  )
}
