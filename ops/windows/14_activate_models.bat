@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_ACTIVATE_REGISTRY_ROOT set "FXSTACK_ACTIVATE_REGISTRY_ROOT=fx-quant-stack/artifacts/registry"
if not defined FXSTACK_ACTIVATE_MANIFEST set "FXSTACK_ACTIVATE_MANIFEST=fx-quant-stack/artifacts/active_models.json"

echo [activate] registry: %FXSTACK_ACTIVATE_REGISTRY_ROOT%
"%TRADER_PYTHON_EXE%" -m src.trader.cli models activate --registry-root %FXSTACK_ACTIVATE_REGISTRY_ROOT% --manifest %FXSTACK_ACTIVATE_MANIFEST% --require-all
if errorlevel 1 (
  echo [activate] ERROR: model activation failed.
  exit /b 2
)

echo [activate] OK
exit /b 0
