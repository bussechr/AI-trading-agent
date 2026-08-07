@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [gpu-check] require_cuda=%FXSTACK_REQUIRE_CUDA%
"%TRADER_PYTHON_EXE%" "%ROOT%\fx-quant-stack\scripts\gpu_check.py"
if errorlevel 1 (
  echo [gpu-check] ERROR: CUDA check failed.
  exit /b 2
)

echo [gpu-check] OK
exit /b 0
