@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [labels] %%P swing D
  "%TRADER_PYTHON_EXE%" -m src.trader.cli labels build --pair %%P --timeframe D --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --horizon-bars 24 --tp-atr-mult 2.0 --sl-atr-mult 1.5
  if errorlevel 1 (
    echo [labels] ERROR: swing labels failed for %%P
    exit /b 2
  )

  echo [labels] %%P intraday M5
  "%TRADER_PYTHON_EXE%" -m src.trader.cli labels build --pair %%P --timeframe M5 --feature-root fx-quant-stack/data/features --label-root fx-quant-stack/data/labels --horizon-bars 18 --tp-atr-mult 1.5 --sl-atr-mult 1.2
  if errorlevel 1 (
    echo [labels] ERROR: intraday labels failed for %%P
    exit /b 2
  )
)

echo [labels] OK
exit /b 0
