# FX Trading System - Quick Start

This project runs on the v2 `fxstack` stack only.

## Prerequisites

- Active Python environment installed via `cd fx-quant-stack && uv sync --extra dev --extra security --extra market_data_download --extra external_mlops --extra deep_inference --frozen`.
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

- `ops/windows/40_full_scale_e2e_validation.bat` is a nonzero production-host quarantine stub; it performs no validation and starts nothing.
- Full candidate validation runs on an external isolated host or VM. See `docs/FULL_SCALE_E2E_RUNBOOK.md`.
- GPU-first offline backtest (WSL): `ops/linux/40_full_scale_backtest_gpu.sh --stage smoke|full`

## Health Checks

Use `ops/windows/23_start_monitor.bat --run` or the dashboard. Bridge authentication is required by default, and the launcher generates separate ignored telemetry and command credentials when the operator has not supplied them. Do not paste those secrets into shell history or documentation. If required credentials are unavailable, protected endpoints fail secure.

## Notes

- Runtime and bridge implementations are fixed to `fxstack`.
- `launch_all.bat endpoints` resolves and persists the actual loopback URLs without starting a service; do not assume the preferred ports are free.
- `http://127.0.0.1:3000` is the stable production dashboard URL and should be served by `next start`, not `next dev`.
- `fx-quant-stack/pyproject.toml` and `fx-quant-stack/uv.lock` are the repository's only Python dependency manifest and lock. The root intentionally has no shadow Python project.
- Use `docs/IG_MT4_SETUP.md` for MT4 wiring details.
- Use `docs/FULL_PROCESS_AUDIT_RUNBOOK.md` for GO/HOLD audit flow.
