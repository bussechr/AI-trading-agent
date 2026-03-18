@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train all --pair %%P --swing-timeframe D --intraday-timeframe M5 --regime-timeframe H4 --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --artifact-root fx-quant-stack/artifacts --training-config fx-quant-stack/configs/training.yaml --registry-root fx-quant-stack/artifacts/registry --deep-stale-hours %FXSTACK_DEEP_MODEL_STALE_HOURS%
  if errorlevel 1 (
    echo [train] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train] OK
exit /b 0
