# Dashboard Dataflow

## Primary Files
- [route.ts](../../app/api/trading/state/route.ts)
- [route.ts](../../app/api/trading/history/route.ts)
- [route.ts](../../app/api/trading/closed-trades/route.ts)
- [route.ts](../../app/api/trading/ops/route.ts)
- [use-live-bridge-state.ts](../../lib/hooks/use-live-bridge-state.ts)
- [bridge.ts](../../lib/server/bridge.ts)
- [freshness.ts](../../lib/trading/freshness.ts)
- [status-tier.ts](../../lib/trading/status-tier.ts)
- [history-normalize.ts](../../lib/trading/history-normalize.ts)
- [closed-trades-normalize.ts](../../lib/trading/closed-trades-normalize.ts)
- [ai-training-normalize.ts](../../lib/trading/ai-training-normalize.ts)
- [use-trading-history.ts](../../lib/hooks/use-trading-history.ts)
- [use-closed-trades.ts](../../lib/hooks/use-closed-trades.ts)
- [use-ops-telemetry.ts](../../lib/hooks/use-ops-telemetry.ts)
- [dashboard-home.tsx](../../components/dashboard-home.tsx)
- [closed-trade-performance.tsx](../../components/closed-trade-performance.tsx)
- [ai-training-panel.tsx](../../components/ai-training-panel.tsx)
- [live-signals.tsx](../../components/live-signals.tsx)
- [live-status-rail.tsx](../../components/live-status-rail.tsx)

## Upstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)

## Downstream
- [../../components/dashboard-layout.tsx](../../components/dashboard-layout.tsx)

## Flow
- bridge state enters through `fetchBridgeJsonWithSource`, which resolves configured endpoint candidates, proves `/v2/handshake` compatibility before reading data, and returns the exact serving base per request
- all four dashboard telemetry routes are explicitly dynamic and attach `Cache-Control: no-store, max-age=0` to success and failure responses; live state, history, closed trades, and ops telemetry must never enter a framework or intermediary response cache
- `app/api/trading/state/route.ts` pins ticks, monitor, and governance reads to the same base that served `/v2/state`, then normalizes that one-instance snapshot into the dashboard contract
- freshness ages reject negative, non-finite, and far-future values; status-tier derivation only reports live when heartbeat, ticks, and runtime signals are all fresh
- `use-live-bridge-state.ts` polls that route, validates the minimum response envelope, and forces errors into a disconnected signal-withheld state
- history polling selects the bridge that serves `/v2/state`, pins every history slice to that exact base, and retains last-good data only within the same source while surfacing failed and malformed-success slices
- closed-trade polling receives the exact bridge URL that served each successful response; totals and averages include every broker-closed row, including zero-P&L breakevens, while wins and losses remain strict positive/negative subsets. Malformed or internally impossible summaries retain last-good history only for the same normalized URL and reset to an empty snapshot when the serving bridge changes
- AI-ops polling selects one bridge through `/v2/state`, fetches workflows and events from that exact source in one dashboard request, and retains a failed slice only within the same normalized bridge URL
- `DashboardHome` shows compact open-position view on `/`
- `LiveSignals` shows the full candidate stream on `/signals`
- `LiveStatusRail` summarizes freshness, runtime, canonical committee/adaptive state, broker-egress authority, and the runtime's equity-scaled planned lot size. Planned lots are never presented as approved or submitted exposure

## Handshakes
- dashboard route -> verified `/v2/state` source with dependent reads pinned to that exact bridge instance
- dashboard history route -> one verified `/v2/state` source with metrics, reports, commands, command events, and governance reads pinned to that exact bridge instance
- dashboard closed-trades route -> exact serving bridge identity plus validated broker-realized trade rows and internally consistent finite aggregate summary, with breakevens retained in total/average denominators -> source-scoped client retention
- dashboard AI-ops route -> one verified `/v2/state` source with workflow and event reads pinned to that exact bridge instance -> independently validated, source-scoped client retention
- dashboard server -> `/v2/handshake` protocol compatibility (major mismatch and `min_compatible` exclusion are fatal)
- dashboard route -> `/v2/ready` fallback semantics via normalized startup failure shape
- bridge state -> dashboard route -> status rail: `release_authority`, `execution_egress_enabled`, and `runtime_diag.entry_lot_sizing` remain distinct so operators can see why no immediate market BUY/SELL trade is authorized and distinguish a planned size from an approved/submitted size
- bridge readiness -> dashboard route: execution uncertainty remains three distinct facts after normalization—whether uncertainty exists, whether it is still account-wide, and the exact contained symbol quarantine—so a globally ready runtime never misreports the affected pair as executable
- client hook -> route polling cadence, minimum-envelope validation, and fail-closed fallback state contract

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [ops-entrypoints.md](ops-entrypoints.md)
