import { DashboardLayout } from "@/components/dashboard-layout"
import { VolatilitySurface } from "@/components/volatility-surface"
import { HestonStatus } from "@/components/heston-status"
import { OptionsChain } from "@/components/options-chain"

export default function OptionsPage() {
  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-foreground">Options & Volatility</h1>
          <p className="text-muted-foreground">FX options market data and Heston model calibration</p>
        </div>

        <HestonStatus />

        <div className="grid gap-6 lg:grid-cols-2">
          <VolatilitySurface />
          <OptionsChain />
        </div>
      </div>
    </DashboardLayout>
  )
}
