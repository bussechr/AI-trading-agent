import { DashboardLayout } from "@/components/dashboard-layout"
import { SignalsTable } from "@/components/signals-table"
import { SignalFilters } from "@/components/signal-filters"

export default function SignalsPage() {
  return (
    <DashboardLayout>
      <div className="space-y-6">
        <div>
          <h1 className="text-3xl font-bold text-foreground">Trading Signals</h1>
          <p className="text-muted-foreground">Complete history of chaos-based trading signals</p>
        </div>

        <SignalFilters />
        <SignalsTable />
      </div>
    </DashboardLayout>
  )
}
