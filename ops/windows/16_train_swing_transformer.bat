@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_TRAIN_ARTIFACT_ROOT set "FXSTACK_TRAIN_ARTIFACT_ROOT=fx-quant-stack/artifacts"
if not defined FXSTACK_TRAIN_FEATURE_ROOT set "FXSTACK_TRAIN_FEATURE_ROOT=fx-quant-stack/data/features"
if not defined FXSTACK_TRAIN_LABEL_ROOT set "FXSTACK_TRAIN_LABEL_ROOT=fx-quant-stack/data/labels"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train-swing-transformer] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train swing-transformer --pair %%P --timeframe D --feature-root %FXSTACK_TRAIN_FEATURE_ROOT% --label-root %FXSTACK_TRAIN_LABEL_ROOT% --out %FXSTACK_TRAIN_ARTIFACT_ROOT%/%%P/swing_transformer
  if errorlevel 1 (
    echo [train-swing-transformer] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train-swing-transformer] OK
exit /b 0
