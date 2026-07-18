# Dashboard Dataflow

## Primary Files
- [route.ts](../../app/api/trading/state/route.ts)
- [route.ts](../../app/api/trading/history/route.ts)
- [use-live-bridge-state.ts](../../lib/hooks/use-live-bridge-state.ts)
- [bridge.ts](../../lib/server/bridge.ts)
- [freshness.ts](../../lib/trading/freshness.ts)
- [status-tier.ts](../../lib/trading/status-tier.ts)
- [history-normalize.ts](../../lib/trading/history-normalize.ts)
- [use-trading-history.ts](../../lib/hooks/use-trading-history.ts)
- [dashboard-home.tsx](../../components/dashboard-home.tsx)
- [live-signals.tsx](../../components/live-signals.tsx)
- [live-status-rail.tsx](../../components/live-status-rail.tsx)

## Upstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)

## Downstream
- [../../components/dashboard-layout.tsx](../../components/dashboard-layout.tsx)

## Flow
- bridge state enters through `fetchBridgeJsonWithSource`, which resolves configured endpoint candidates, proves `/v2/handshake` compatibility before reading data, and returns the exact serving base per request
- `app/api/trading/state/route.ts` pins ticks, monitor, and governance reads to the same base that served `/v2/state`, then normalizes that one-instance snapshot into the dashboard contract
- freshness ages reject negative, non-finite, and far-future values; status-tier derivation only reports live when heartbeat, ticks, and runtime signals are all fresh
- `use-live-bridge-state.ts` polls that route, validates the minimum response envelope, and forces errors into a disconnected signal-withheld state
- history polling selects the bridge that serves `/v2/state`, pins every history slice to that exact base, and retains last-good data only within the same source while surfacing failed and malformed-success slices
- AI-ops polling retains last-good data independently while surfacing failed and malformed-success sources
- `DashboardHome` shows compact open-position view on `/`
- `LiveSignals` shows the full candidate stream on `/signals`
- `LiveStatusRail` summarizes freshness, runtime, shadow, and adaptive execution status

## Handshakes
- dashboard route -> verified `/v2/state` source with dependent reads pinned to that exact bridge instance
- dashboard history route -> one verified `/v2/state` source with metrics, reports, commands, command events, and governance reads pinned to that exact bridge instance
- dashboard server -> `/v2/handshake` protocol compatibility (major mismatch and `min_compatible` exclusion are fatal)
- dashboard route -> `/v2/ready` fallback semantics via normalized startup failure shape
- client hook -> route polling cadence, minimum-envelope validation, and fail-closed fallback state contract

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [ops-entrypoints.md](ops-entrypoints.md)
