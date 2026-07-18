@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_TRAIN_ARTIFACT_ROOT set "FXSTACK_TRAIN_ARTIFACT_ROOT=fx-quant-stack/artifacts"
if not defined FXSTACK_TRAIN_FEATURE_ROOT set "FXSTACK_TRAIN_FEATURE_ROOT=fx-quant-stack/data/features"
if not defined FXSTACK_TRAIN_LABEL_ROOT set "FXSTACK_TRAIN_LABEL_ROOT=fx-quant-stack/data/labels"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train-intraday-tcn] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train intraday-tcn --pair %%P --timeframe M5 --feature-root %FXSTACK_TRAIN_FEATURE_ROOT% --label-root %FXSTACK_TRAIN_LABEL_ROOT% --out %FXSTACK_TRAIN_ARTIFACT_ROOT%/%%P/intraday_tcn
  if errorlevel 1 (
    echo [train-intraday-tcn] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train-intraday-tcn] OK
exit /b 0
