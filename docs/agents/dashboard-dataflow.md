# Dashboard Dataflow

## Primary Files
- [route.ts](../../app/api/trading/state/route.ts)
- [use-live-bridge-state.ts](../../lib/hooks/use-live-bridge-state.ts)
- [bridge.ts](../../lib/server/bridge.ts)
- [dashboard-home.tsx](../../components/dashboard-home.tsx)
- [live-signals.tsx](../../components/live-signals.tsx)
- [live-status-rail.tsx](../../components/live-status-rail.tsx)

## Upstream
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)

## Downstream
- [../../components/dashboard-layout.tsx](../../components/dashboard-layout.tsx)

## Flow
- bridge state enters through `fetchBridgeJson`
- `app/api/trading/state/route.ts` normalizes mixed bridge payloads into one dashboard contract
- `use-live-bridge-state.ts` polls that route and exposes typed derived state
- `DashboardHome` shows compact open-position view on `/`
- `LiveSignals` shows the full candidate stream on `/signals`
- `LiveStatusRail` summarizes freshness, runtime, shadow, and adaptive execution status

## Handshakes
- dashboard route -> `/v2/state`
- dashboard route -> `/v2/ready` fallback semantics via normalized startup failure shape
- client hook -> route polling cadence and fallback state contract

## Related Docs
- [runtime-loop.md](runtime-loop.md)
- [ops-entrypoints.md](ops-entrypoints.md)
