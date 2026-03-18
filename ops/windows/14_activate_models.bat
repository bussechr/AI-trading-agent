@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [activate] registry: fx-quant-stack/artifacts/registry
"%TRADER_PYTHON_EXE%" -m src.trader.cli models activate --registry-root fx-quant-stack/artifacts/registry --manifest fx-quant-stack/artifacts/active_models.json --require-all
if errorlevel 1 (
  echo [activate] ERROR: model activation failed.
  exit /b 2
)

echo [activate] OK
exit /b 0
