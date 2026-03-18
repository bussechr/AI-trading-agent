@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [gpu-check] require_cuda=%FXSTACK_REQUIRE_CUDA%
"%TRADER_PYTHON_EXE%" -m src.trader.cli stack gpu-check
if errorlevel 1 (
  echo [gpu-check] ERROR: CUDA check failed.
  exit /b 2
)

echo [gpu-check] OK
exit /b 0
