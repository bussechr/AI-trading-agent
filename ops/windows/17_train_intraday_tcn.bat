@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train-intraday-tcn] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train intraday-tcn --pair %%P --timeframe M5 --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --out fx-quant-stack/artifacts/%%P/intraday_tcn
  if errorlevel 1 (
    echo [train-intraday-tcn] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train-intraday-tcn] OK
exit /b 0
