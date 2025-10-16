import { DashboardLayout } from "@/components/dashboard-layout"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Terminal, Download, ExternalLink } from "lucide-react"
import { Button } from "@/components/ui/button"

export default function DashboardPage() {
  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-foreground">Trading Dashboard</h1>
          <p className="text-muted-foreground">Real-time chaos-based FX trading signals</p>
        </div>

        <Alert className="border-amber-500/50 bg-amber-500/10">
          <Terminal className="h-4 w-4" />
          <AlertTitle>Local Setup Required</AlertTitle>
          <AlertDescription className="mt-2 space-y-4">
            <p>
              This dashboard connects to your local Python bridge and MT4. It must run on your machine, not in the v0
              preview.
            </p>

            <div className="space-y-3 rounded-lg bg-black/20 p-4 font-mono text-sm">
              <div>
                <div className="mb-1 text-xs text-muted-foreground">1. Download the project:</div>
                <div className="flex items-center gap-2">
                  <Button variant="outline" size="sm" asChild>
                    <a href="#" className="flex items-center gap-2">
                      <Download className="h-3 w-3" />
                      Download ZIP
                    </a>
                  </Button>
                  <span className="text-xs text-muted-foreground">or push to GitHub and clone</span>
                </div>
              </div>

              <div>
                <div className="mb-1 text-xs text-muted-foreground">2. Install dependencies:</div>
                <code className="block rounded bg-black/40 p-2">npm install</code>
              </div>

              <div>
                <div className="mb-1 text-xs text-muted-foreground">3. Start the Python bridge:</div>
                <code className="block rounded bg-black/40 p-2">python bridge_api/bridge.py</code>
              </div>

              <div>
                <div className="mb-1 text-xs text-muted-foreground">4. Start MT4 with BridgeEA:</div>
                <code className="block rounded bg-black/40 p-2">
                  Open MT4 → Attach MQL4/Experts/BridgeEA.mq4 to any chart
                  <br />
                  IG Account: 96940 | Server: IG-LIVE2
                </code>
              </div>

              <div>
                <div className="mb-1 text-xs text-muted-foreground">5. Run the trading agent:</div>
                <code className="block rounded bg-black/40 p-2">
                  python -m src.agents.fx_el_hawkes_agent --config src/config/fx_el_minis.yaml
                </code>
              </div>

              <div>
                <div className="mb-1 text-xs text-muted-foreground">6. Start the dashboard locally:</div>
                <code className="block rounded bg-black/40 p-2">npm run dev</code>
                <div className="mt-1 text-xs text-muted-foreground">Then open http://localhost:3000</div>
              </div>
            </div>

            <div className="flex items-start gap-2 rounded-lg bg-blue-500/10 p-3 text-sm">
              <ExternalLink className="mt-0.5 h-4 w-4 flex-shrink-0 text-blue-500" />
              <div>
                <div className="font-medium text-blue-500">Why local?</div>
                <div className="text-xs text-muted-foreground">
                  The v0 preview runs on HTTPS and cannot connect to your local HTTP bridge (127.0.0.1:5000) due to
                  browser security. Running locally allows the dashboard to communicate with your Python bridge and MT4.
                </div>
              </div>
            </div>
          </AlertDescription>
        </Alert>

        <div className="rounded-lg border border-dashed border-muted-foreground/20 p-12 text-center">
          <Terminal className="mx-auto h-12 w-12 text-muted-foreground/50" />
          <h3 className="mt-4 text-lg font-semibold text-foreground">Dashboard Preview</h3>
          <p className="mt-2 text-sm text-muted-foreground">
            Follow the setup instructions above to see live trading data
          </p>
        </div>
      </div>
    </DashboardLayout>
  )
}
