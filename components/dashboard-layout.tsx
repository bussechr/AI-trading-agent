"use client"

import type React from "react"

import { useState } from "react"
import Link from "next/link"
import { usePathname } from "next/navigation"
import { BrainCircuit, LayoutDashboard, Menu, Radar, TrendingUp, X } from "lucide-react"
import { useLiveBridgeState } from "@/lib/hooks/use-live-bridge-state"
import { bridgeStatusClasses, bridgeStatusDotClasses, bridgeStatusLabel, formatAgeSeconds } from "@/lib/trading/live-state"
import { cn } from "@/lib/utils"

const navigation = [
  { name: "Dashboard", href: "/", icon: LayoutDashboard, summary: "Bridge truth, runtime, live cards" },
  { name: "Signals", href: "/signals", icon: TrendingUp, summary: "Signal history and filters" },
  { name: "Options", href: "/options", icon: Radar, summary: "Volatility and options surfaces" },
  { name: "Performance", href: "/performance", icon: TrendingUp, summary: "Equity, drawdown, governance" },
  { name: "AI Training", href: "/ai-training", icon: BrainCircuit, summary: "Observe-only training telemetry" },
]

const pageTitles: Record<string, string> = {
  "/": "Dashboard",
  "/signals": "Signals",
  "/options": "Options",
  "/performance": "Performance",
  "/ai-training": "AI Training",
}

export function DashboardLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const { state, updatedAt } = useLiveBridgeState(5000)

  const currentPage = pageTitles[pathname] || "FX Trader"
  const pulseLabel = state?.lastHeartbeat
    ? formatAgeSeconds(state.heartbeatAgeSecs)
    : updatedAt
      ? new Date(updatedAt).toLocaleTimeString()
      : "n/a"

  return (
    <div className="min-h-screen bg-background text-foreground">
      {sidebarOpen && (
        <div className="fixed inset-0 z-40 bg-slate-950/45 backdrop-blur-sm lg:hidden" onClick={() => setSidebarOpen(false)} />
      )}

      <aside
        className={cn(
          "fixed inset-y-0 left-0 z-50 w-80 border-r border-sidebar-border bg-sidebar text-sidebar-foreground transition-transform duration-200 ease-out lg:translate-x-0",
          sidebarOpen ? "translate-x-0" : "-translate-x-full",
        )}
      >
        <div className="flex h-full flex-col">
          <div className="border-b border-sidebar-border px-6 py-6">
            <div className="flex items-start justify-between gap-3">
              <div>
                <div className="text-[11px] uppercase tracking-[0.26em] text-slate-400">FX Trader</div>
                <h2 className="mt-3 text-2xl font-semibold text-white">Control Surface</h2>
                <p className="mt-2 text-sm text-slate-400">Stable production dashboard on <span className="font-mono text-slate-200">127.0.0.1:3000</span></p>
              </div>
              <button onClick={() => setSidebarOpen(false)} className="lg:hidden text-slate-400 hover:text-white">
                <X className="h-5 w-5" />
              </button>
            </div>
          </div>

          <nav className="flex-1 space-y-2 px-4 py-5">
            {navigation.map((item) => {
              const isActive = pathname === item.href
              return (
                <Link
                  key={item.name}
                  href={item.href}
                  onClick={() => setSidebarOpen(false)}
                  className={cn(
                    "group block rounded-2xl border px-4 py-3 transition-colors",
                    isActive
                      ? "border-white/14 bg-white/10 text-white"
                      : "border-transparent bg-white/[0.03] text-slate-300 hover:border-white/10 hover:bg-white/[0.06] hover:text-white",
                  )}
                >
                  <div className="flex items-center gap-3">
                    <item.icon className="h-4 w-4" />
                    <span className="font-medium">{item.name}</span>
                  </div>
                  <div className="mt-2 text-sm text-slate-400">{item.summary}</div>
                </Link>
              )
            })}
          </nav>

          <div className="border-t border-sidebar-border px-4 py-5">
            <div className="rounded-2xl border border-white/10 bg-white/[0.04] p-4">
              <div className="flex items-center justify-between gap-3">
                <div>
                  <div className="text-[11px] uppercase tracking-[0.22em] text-slate-500">Bridge Status</div>
                  <div className="mt-2 flex items-center gap-2">
                    <span className={cn("h-2.5 w-2.5 rounded-full", bridgeStatusDotClasses(state?.statusTier))} />
                    <span className="text-sm font-medium text-white">{bridgeStatusLabel(state?.statusTier)}</span>
                  </div>
                </div>
                <span className={cn("rounded-full border px-2 py-1 text-[11px] font-medium", bridgeStatusClasses(state?.statusTier))}>
                  {String(state?.tickStatus || "unknown")}
                </span>
              </div>
              <div className="mt-3 space-y-1 text-xs text-slate-400">
                <div>Heartbeat: {pulseLabel}</div>
                <div>Ticks: {String(state?.tickReason || "waiting")}</div>
                <div>Loop: {Number.isFinite(Number(state?.runtimeDiag?.loop_latency_ms)) ? `${Number(state?.runtimeDiag?.loop_latency_ms).toFixed(0)} ms` : "n/a"}</div>
              </div>
            </div>
          </div>
        </div>
      </aside>

      <div className="lg:pl-80">
        <header className="sticky top-0 z-30 border-b border-border/70 bg-background/80 backdrop-blur-xl">
          <div className="flex h-20 items-center gap-4 px-5 lg:px-8">
            <button onClick={() => setSidebarOpen(true)} className="rounded-full border border-border bg-card p-2 text-muted-foreground hover:text-foreground lg:hidden">
              <Menu className="h-5 w-5" />
            </button>
            <div className="min-w-0 flex-1">
              <div className="text-[11px] uppercase tracking-[0.24em] text-muted-foreground">Route</div>
              <div className="mt-1 text-2xl font-semibold text-foreground">{currentPage}</div>
            </div>
            <div className="hidden items-center gap-3 md:flex">
              <div className="rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground">
                Public URL <span className="ml-2 font-mono text-foreground">127.0.0.1:3000</span>
              </div>
              <div className="rounded-full border border-border bg-card px-4 py-2 text-sm text-muted-foreground">
                Last pulse <span className="ml-2 font-mono text-foreground">{pulseLabel}</span>
              </div>
            </div>
          </div>
        </header>

        <main className="px-5 py-6 lg:px-8">{children}</main>
      </div>
    </div>
  )
}
