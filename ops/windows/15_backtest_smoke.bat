@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [backtest-smoke] %%P M5
  "%TRADER_PYTHON_EXE%" -m src.trader.cli backtest run --pair %%P --timeframe M5 --feature-root fx-quant-stack/data/features
  if errorlevel 1 (
    echo [backtest-smoke] ERROR: failed for %%P
    exit /b 2
  )
)

echo [backtest-smoke] OK
exit /b 0
