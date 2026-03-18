# Full-Scale GPU-First Backtest Runbook (WSL, Offline E2E)

This runbook executes a full offline `fxstack` pipeline:

`data fetch -> gate -> ingest -> features -> labels -> training -> activation -> tests -> backtests`

## Profile

- Runner: WSL/Linux
- Universe: `EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD`
- GPU policy: preferred with CPU fallback for XGBoost
- Runtime scope: offline only (no MT4 live execution in this cycle)

## Environment

```bash
export FXSTACK_REQUIRE_CUDA=1
export FXSTACK_XGB_DEVICE=auto
export FXSTACK_XGB_TREE_METHOD=hist
export FXSTACK_XGB_ALLOW_CPU_FALLBACK=1
```

## Smoke Gate

```bash
bash ops/linux/40_full_scale_backtest_gpu.sh --stage smoke
```

## Full Scale

```bash
bash ops/linux/40_full_scale_backtest_gpu.sh \
  --stage full \
  --start 2024-01-01T00:00:00Z \
  --end 2026-03-17T00:00:00Z
```

## Outputs

Per run:

- `docs/backtests/<timestamp>/summary.json`
- `docs/backtests/<timestamp>/phases.jsonl`
- `docs/backtests/<timestamp>/phase_*.json` and `*.log`
- `docs/backtests/<timestamp>/backtest_full/per_pair.json`
- `docs/backtests/<timestamp>/backtest_full/aggregate.json`
- `docs/backtests/<timestamp>/backtest_full/signals_sample.csv`

## Failure and Fallback Behavior

- Any phase failure is fail-fast and stops the run.
- XGBoost behavior:
  - `FXSTACK_XGB_DEVICE=auto`: use CUDA when available, otherwise CPU.
  - `FXSTACK_XGB_DEVICE=cuda` with `FXSTACK_XGB_ALLOW_CPU_FALLBACK=1`: retry on CPU if CUDA fit fails.
  - `FXSTACK_XGB_DEVICE=cuda` with `FXSTACK_XGB_ALLOW_CPU_FALLBACK=0`: fail hard if CUDA is not usable.
- Deep models (Transformer/TCN) honor `FXSTACK_REQUIRE_CUDA` via existing runtime checks.

## Acceptance Checklist

- `stack gpu-check` passes with `require_cuda=true`
- Dukascopy gate passes for expected files/thresholds
- Model activation passes with `--require-all`
- Targeted tests return exit code `0`
- `backtest_full/aggregate.json` shows finite metrics and non-zero trades
