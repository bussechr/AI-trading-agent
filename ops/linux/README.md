# Linux/WSL Launcher Stack

WSL-native orchestration for full offline `fxstack` backtest validation.

## Entrypoints

- `ops/linux/_env.sh`: shared environment defaults and python discovery
- `ops/linux/40_full_scale_backtest_gpu.sh`: staged fail-fast offline E2E backtest

## Examples

```bash
bash ops/linux/40_full_scale_backtest_gpu.sh --stage smoke
```

```bash
bash ops/linux/40_full_scale_backtest_gpu.sh --stage full --start 2024-01-01T00:00:00Z --end 2026-03-17T00:00:00Z
```

## Evidence

Each run writes to `docs/backtests/<timestamp>/` including `summary.json`, per-phase logs, and full backtest artifacts.
