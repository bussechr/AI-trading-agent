@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  for %%T in (M1 M5 M15 H4 D) do (
    echo [features] %%P %%T
    "%TRADER_PYTHON_EXE%" -m src.trader.cli features build --pair %%P --timeframe %%T --input-root fx-quant-stack/data/raw --output-root fx-quant-stack/data/features
    if errorlevel 1 (
      echo [features] ERROR: failed for %%P %%T
      exit /b 2
    )
  )
)

echo [features] OK
exit /b 0
