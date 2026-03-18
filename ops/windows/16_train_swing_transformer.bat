@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train-swing-transformer] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train swing-transformer --pair %%P --timeframe D --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --out fx-quant-stack/artifacts/%%P/swing_transformer
  if errorlevel 1 (
    echo [train-swing-transformer] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train-swing-transformer] OK
exit /b 0
