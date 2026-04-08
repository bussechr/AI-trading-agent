# Ops Entrypoints

## Primary Files
- [_env.bat](../../ops/windows/_env.bat)
- [20_start_bridge.bat](../../ops/windows/20_start_bridge.bat)
- [21_start_runtime.bat](../../ops/windows/21_start_runtime.bat)
- [22_start_dashboard.bat](../../ops/windows/22_start_dashboard.bat)
- [23_start_monitor.bat](../../ops/windows/23_start_monitor.bat)
- [24_start_feature_push_worker.bat](../../ops/windows/24_start_feature_push_worker.bat)
- [25_monitor_everything.ps1](../../ops/windows/25_monitor_everything.ps1)
- [90_stop_all.bat](../../ops/windows/90_stop_all.bat)

## Upstream
- [AGENTS.md](../../AGENTS.md)

## Downstream
- [runtime-loop.md](runtime-loop.md)
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [dashboard-dataflow.md](dashboard-dataflow.md)

## Start Order
- `_env.bat`: shared env and interpreter resolution
- `20_start_bridge.bat`: bridge + readiness on `/v2/ready`
- `21_start_runtime.bat`: runtime + startup phase watchdog
- `22_start_dashboard.bat`: Next.js production server
- `23_start_monitor.bat`: monitor confidence loop
- `24_start_feature_push_worker.bat`: drains runtime feature-push intents into the Feast online store
- `25_monitor_everything.ps1`: consolidated watch of training, bridge, dashboard, runtime
- `90_stop_all.bat`: repo-scoped shutdown across Windows/WSL plus runtime snapshot clear

## Handshakes
- bridge readiness -> `/v2/ready`
- dashboard readiness -> HTTP `GET /`
- runtime readiness -> `/v2/ready` with startup phase fields
- feature push worker -> runtime outbox to Feast online store
- env propagation -> Windows batch exports mirrored into Python and Node child processes

## Related Docs
- [bridge-and-api-handshakes.md](bridge-and-api-handshakes.md)
- [../../fx-quant-stack/docs/runbooks.md](../../fx-quant-stack/docs/runbooks.md)
