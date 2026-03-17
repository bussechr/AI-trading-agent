"use client"

import { Card } from "@/components/ui/card"
import { useTradingTelemetry } from "@/lib/hooks/use-trading-telemetry"

export function TradeStatistics() {
  const { telemetry, loading } = useTradingTelemetry(3000)
  const metrics = telemetry.metrics || {}
  const counters = metrics.counters || {}
  const pending = metrics.pending || {}
  const timeouts = metrics.timeouts || {}
  const throughput = metrics.throughput || {}
  const governance = metrics.governance || {}
  const lifecycleLatency = metrics.lifecycle_latency_ms || {}
  const envelope = metrics.risk_envelope || {}
  const state = telemetry.state
  const queueToTerminal = lifecycleLatency.queue_to_terminal || {}
  const deliveredToTerminal = lifecycleLatency.delivered_to_terminal || {}

  const acked = Number(counters.acked || 0)
  const failed = Number(counters.failed || 0)
  const expired = Number(counters.expired || 0)
  const terminal = acked + failed + expired
  const successRate = terminal > 0 ? (acked / terminal) * 100 : 0

  const stats = [
    {
      label: "Commands Total",
      value: loading ? "..." : String(Number(counters.commands_total || 0)),
      subtext: `Acked ${acked} / Failed ${failed}`,
    },
    {
      label: "Terminal Success",
      value: loading ? "..." : `${successRate.toFixed(1)}%`,
      subtext: `${terminal} terminal outcomes`,
    },
    {
      label: "ACK Timeout Rate",
      value: loading ? "..." : `${(Number(timeouts.ack_timeout_rate_5m || 0) * 100).toFixed(2)}%`,
      subtext: `${Number(timeouts.timeout_failures_5m || 0)} in last 5m`,
    },
    {
      label: "Executed Entries (5m)",
      value: loading ? "..." : String(Number(throughput.executed_entries_5m || 0)),
      subtext: "Adaptive envelope throughput KPI",
    },
    {
      label: "Queue->ACK p95",
      value: loading ? "..." : `${Number(queueToTerminal.p95 || 0).toFixed(0)} ms`,
      subtext: `p50 ${Number(queueToTerminal.p50 || 0).toFixed(0)} ms`,
    },
    {
      label: "Queue Pressure",
      value: loading ? "..." : String(Number(pending.count || 0)),
      subtext: `${Number(pending.oldest_pending_secs || 0).toFixed(1)}s oldest pending`,
    },
    {
      label: "Governance Events",
      value: loading ? "..." : String(Number(governance.events_24h || 0)),
      subtext: "Last 24h transitions",
    },
    {
      label: "Soft DD Band",
      value: loading ? "..." : `${(Number(envelope.soft_dd_pct || 0) * 100).toFixed(2)}%`,
      subtext: `Regime: ${String(envelope.regime || "unknown")}`,
    },
    {
      label: "Hard DD Band",
      value: loading ? "..." : `${(Number(envelope.hard_dd_pct || 0) * 100).toFixed(2)}%`,
      subtext: "Adaptive hard drawdown",
    },
    {
      label: "Daily Breaker",
      value: loading ? "..." : `${(Number(envelope.daily_breaker_pct || 0) * 100).toFixed(2)}%`,
      subtext: state?.governance?.paused
        ? "Paused by governance"
        : `Deliver->ACK p95 ${Number(deliveredToTerminal.p95 || 0).toFixed(0)} ms`,
    },
  ]

  return (
    <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
      {stats.map((stat) => (
        <Card key={stat.label} className="p-6">
          <div className="text-sm text-muted-foreground mb-1">{stat.label}</div>
          <div className="text-2xl font-bold text-foreground mb-1">{stat.value}</div>
          <div className="text-xs text-muted-foreground">{stat.subtext}</div>
        </Card>
      ))}
    </div>
  )
}
