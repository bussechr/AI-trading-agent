@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_TRAIN_ARTIFACT_ROOT set "FXSTACK_TRAIN_ARTIFACT_ROOT=fx-quant-stack/artifacts"
if not defined FXSTACK_TRAIN_FEATURE_ROOT set "FXSTACK_TRAIN_FEATURE_ROOT=fx-quant-stack/data/features"
if not defined FXSTACK_TRAIN_LABEL_ROOT set "FXSTACK_TRAIN_LABEL_ROOT=fx-quant-stack/data/labels"

echo [train-deep-stale] stale_hours=%FXSTACK_DEEP_MODEL_STALE_HOURS%
"%TRADER_PYTHON_EXE%" -m src.trader.cli train deep-stale --swing-timeframe D --intraday-timeframe M5 --feature-root %FXSTACK_TRAIN_FEATURE_ROOT% --label-root %FXSTACK_TRAIN_LABEL_ROOT% --artifact-root %FXSTACK_TRAIN_ARTIFACT_ROOT% --stale-hours %FXSTACK_DEEP_MODEL_STALE_HOURS%
if errorlevel 1 (
  echo [train-deep-stale] ERROR: deep stale retrain failed.
  exit /b 2
)

echo [train-deep-stale] OK
exit /b 0
