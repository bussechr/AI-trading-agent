@echo off
title MT4 Trading Agent
echo Starting Python Trading Agent...
echo.
cd /d "%~dp0"
set PYTHONPATH=%~dp0\src

echo Usage: run_agent.bat [EQUITY]
echo Default Equity: 10000
echo.

set EQUITY=10000
if not "%1"=="" set EQUITY=%1

:: Find Python - check known locations first (Windows Store alias interferes with PATH)
if exist "C:\Python311\python.exe" (
    "C:\Python311\python.exe" src\run_fx.py --equity %EQUITY% --sleep 10
) else if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" src\run_fx.py --equity %EQUITY% --sleep 10
) else (
    :: Last resort: try PATH (may hit Windows Store alias)
    python src\run_fx.py --equity %EQUITY% --sleep 10
)
pause
