@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_TRAIN_ARTIFACT_ROOT set "FXSTACK_TRAIN_ARTIFACT_ROOT=fx-quant-stack/artifacts"
if not defined FXSTACK_TRAIN_REGISTRY_ROOT set "FXSTACK_TRAIN_REGISTRY_ROOT=fx-quant-stack/artifacts/registry"
if not defined FXSTACK_TRAIN_FEATURE_ROOT set "FXSTACK_TRAIN_FEATURE_ROOT=fx-quant-stack/data/features"
if not defined FXSTACK_TRAIN_LABEL_ROOT set "FXSTACK_TRAIN_LABEL_ROOT=fx-quant-stack/data/labels"
if not defined FXSTACK_TRAIN_CONFIG set "FXSTACK_TRAIN_CONFIG=fx-quant-stack/configs/training.yaml"
set "FXSTACK_FORCE_RETRAIN_ARG="
if /I "%FXSTACK_FORCE_RETRAIN%"=="1" set "FXSTACK_FORCE_RETRAIN_ARG=--force-retrain"
set "FXSTACK_TRAIN_BELIEF_ARG="
if /I "%FXSTACK_TRAIN_WITH_BELIEF%"=="1" set "FXSTACK_TRAIN_BELIEF_ARG=--with-belief"
if /I "%FXSTACK_TRAIN_WITH_BELIEF%"=="0" set "FXSTACK_TRAIN_BELIEF_ARG=--no-with-belief"

for %%P in (%FXSTACK_PAIRS_SP%) do (
  echo [train] %%P
  "%TRADER_PYTHON_EXE%" -m src.trader.cli train all --pair %%P --swing-timeframe D --intraday-timeframe M5 --regime-timeframe H4 --feature-root %FXSTACK_TRAIN_FEATURE_ROOT% --label-root %FXSTACK_TRAIN_LABEL_ROOT% --artifact-root %FXSTACK_TRAIN_ARTIFACT_ROOT% --training-config %FXSTACK_TRAIN_CONFIG% --registry-root %FXSTACK_TRAIN_REGISTRY_ROOT% --deep-stale-hours %FXSTACK_DEEP_MODEL_STALE_HOURS% %FXSTACK_FORCE_RETRAIN_ARG% %FXSTACK_TRAIN_BELIEF_ARG%
  if errorlevel 1 (
    echo [train] ERROR: failed for %%P
    exit /b 2
  )
)

echo [train] OK
exit /b 0
