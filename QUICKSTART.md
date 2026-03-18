# FX Trading System - Quick Start

This project runs on the v2 `fxstack` stack only.

## Prerequisites

- Python environment installed (`poetry install` or project venv).
- Node dependencies for dashboard (`pnpm install`).
- MT4 terminal configured with WebRequest allowlist:
  - `http://127.0.0.1:58710`

## Core Commands

Terminal 1 (bridge):

```bash
python -m src.trader.cli bridge serve --host 127.0.0.1 --port 58710
```

Terminal 2 (runtime):

```bash
python -m src.trader.cli runtime run --equity 10000 --sleep 10
```

Terminal 3 (dashboard):

```bash
pnpm dev
```

## Windows Launchers

- `start.bat [EQUITY]` for staged startup.
- `run_bridge.bat [PORT]` to run bridge only.
- `run_agent.bat [EQUITY] [BRIDGE_PORT]` to run runtime only.
- `ops/windows/90_stop_all.bat` to stop all services.

## Full Validation Paths

- Full E2E validation: `ops/windows/40_full_scale_e2e_validation.bat [EQUITY]`
- GPU-first offline backtest (WSL): `ops/linux/40_full_scale_backtest_gpu.sh --stage smoke|full`

## Health Checks

```bash
curl http://127.0.0.1:58710/v2/health
curl http://127.0.0.1:58710/v2/state
curl http://127.0.0.1:58710/v2/metrics
```

## Notes

- Runtime and bridge implementations are fixed to `fxstack`.
- Use `docs/IG_MT4_SETUP.md` for MT4 wiring details.
- Use `docs/FULL_PROCESS_AUDIT_RUNBOOK.md` for GO/HOLD audit flow.
