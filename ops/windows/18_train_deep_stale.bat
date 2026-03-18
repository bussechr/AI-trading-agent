@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [train-deep-stale] stale_hours=%FXSTACK_DEEP_MODEL_STALE_HOURS%
"%TRADER_PYTHON_EXE%" -m src.trader.cli train deep-stale --swing-timeframe D --intraday-timeframe M5 --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --artifact-root fx-quant-stack/artifacts --stale-hours %FXSTACK_DEEP_MODEL_STALE_HOURS%
if errorlevel 1 (
  echo [train-deep-stale] ERROR: deep stale retrain failed.
  exit /b 2
)

echo [train-deep-stale] OK
exit /b 0
