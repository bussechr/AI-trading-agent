# FX Trading System - Quick Start

This project runs on the v2 `fxstack` stack only.

## Prerequisites

- Active Python environment installed via `cd fx-quant-stack && uv sync --extra dev`.
- Node dependencies for dashboard (`pnpm install`).
- MT4 terminal configured with WebRequest allowlist:
  - `http://127.0.0.1:58710`

## Core Commands

Primary operator path:

```bash
launch_all.bat live 10000
```

Status and shutdown:

```bash
launch_all.bat status
launch_all.bat stop
```

Manual dashboard start only after a production build exists:

```bash
ops/windows/02_sync_node.bat
ops/windows/22_start_dashboard.bat --run 3000
```

Developer preview only:

```bash
pnpm dev
# serves on http://127.0.0.1:3001
```

## Windows Launchers

- `launch_all.bat live [EQUITY]` for staged startup.
- `launch_all.bat status` for bridge/runtime/dashboard status.
- `launch_all.bat stop` to stop the staged stack.
- `ops/windows/90_stop_all.bat` to stop all services directly.

## Full Validation Paths

- Full E2E validation: `ops/windows/40_full_scale_e2e_validation.bat [EQUITY]`
- GPU-first offline backtest (WSL): `ops/linux/40_full_scale_backtest_gpu.sh --stage smoke|full`

## Health Checks

```bash
curl http://127.0.0.1:58710/v2/ready
curl http://127.0.0.1:58710/v2/state
curl http://127.0.0.1:58710/v2/metrics
```

**Bridge auth is required by default.** Set `FXSTACK_BRIDGE_API_KEY=<secret>` before
launching, and include `-H "X-API-Key: $FXSTACK_BRIDGE_API_KEY"` on every non-public
request. To explicitly disable auth for local dev only, set
`FXSTACK_BRIDGE_AUTH_REQUIRED=false`. If the key is empty while auth is required, the
bridge fails secure: every non-public endpoint returns 503 and a critical log line
explains how to fix it.

## Notes

- Runtime and bridge implementations are fixed to `fxstack`.
- `http://127.0.0.1:3000` is the stable production dashboard URL and should be served by `next start`, not `next dev`.
- Root `pyproject.toml` and `requirements.txt` are legacy compatibility surfaces, not the active setup path.
- Use `docs/IG_MT4_SETUP.md` for MT4 wiring details.
- Use `docs/FULL_PROCESS_AUDIT_RUNBOOK.md` for GO/HOLD audit flow.
