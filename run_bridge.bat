@echo off
setlocal enabledelayedexpansion
title MT4 Bridge Server
echo Starting MT4 Bridge Server...
echo.
cd /d "%~dp0"
set PYTHONPATH=%~dp0
if not defined MT4_BRIDGE_ENABLE_V1_COMPAT set MT4_BRIDGE_ENABLE_V1_COMPAT=1
if not defined TRADER_RUNTIME_DB_PATH set TRADER_RUNTIME_DB_PATH=%~dp0data\state\runtime_v2.db

echo Usage: run_bridge.bat
echo Mode: v2-only
echo Legacy v1 compatibility: %MT4_BRIDGE_ENABLE_V1_COMPAT%
echo Runtime DB: %TRADER_RUNTIME_DB_PATH%
echo.

echo Syncing MQL4 bridge sources into MT4 terminal data folders...
set "MT4_TERMINAL_ROOT=%APPDATA%\MetaQuotes\Terminal"
set "SYNC_COUNT=0"
if exist "%MT4_TERMINAL_ROOT%" (
    for /d %%T in ("%MT4_TERMINAL_ROOT%\*") do (
        if exist "%%~fT\MQL4" (
            if exist "%%~fT\MQL4\Experts" (
                copy /Y "%~dp0MQL4\Experts\BridgeEA.mq4" "%%~fT\MQL4\Experts\BridgeEA.mq4" >nul 2>&1
            )
            if exist "%%~fT\MQL4\Indicators" (
                copy /Y "%~dp0MQL4\Indicators\BridgeVisualizer.mq4" "%%~fT\MQL4\Indicators\BridgeVisualizer.mq4" >nul 2>&1
            )
            if exist "%%~fT\MQL4\Include" (
                copy /Y "%~dp0MQL4\Include\BridgeHttp.mqh" "%%~fT\MQL4\Include\BridgeHttp.mqh" >nul 2>&1
                copy /Y "%~dp0MQL4\Include\BridgeUtils.mqh" "%%~fT\MQL4\Include\BridgeUtils.mqh" >nul 2>&1
            )
            set /a SYNC_COUNT+=1
            echo   synced terminal profile: %%~nxT
        )
    )
)
if "!SYNC_COUNT!"=="0" (
    echo   no MT4 terminal profile found under "%MT4_TERMINAL_ROOT%"
) else (
    echo   sync complete for !SYNC_COUNT! profile^(s^)
    echo   NOTE: Recompile BridgeEA.mq4 and BridgeVisualizer.mq4 in MetaEditor after source updates.
)
echo.

set "PY_EXE="
if defined TRADER_PYTHON_EXE if exist "%TRADER_PYTHON_EXE%" set "PY_EXE=%TRADER_PYTHON_EXE%"
if not defined PY_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PY_EXE if exist "C:\Python311\python.exe" set "PY_EXE=C:\Python311\python.exe"
if not defined PY_EXE if exist "C:\Python312\python.exe" set "PY_EXE=C:\Python312\python.exe"
if not defined PY_EXE set "PY_EXE=python"

echo Python: %PY_EXE%
echo.

echo Running bridge preflight checks...
"%PY_EXE%" -c "import importlib.util,sys;mods=['flask','flask_cors','yaml'];missing=[m for m in mods if importlib.util.find_spec(m) is None];print('Missing modules: ' + (', '.join(missing) if missing else 'none'));sys.exit(1 if missing else 0)"
if errorlevel 1 (
    echo.
    echo ERROR: Selected Python is missing dependencies required for RuntimeService.
    echo Install with:
    echo   "%PY_EXE%" -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Verifying RuntimeService imports...
"%PY_EXE%" -c "import os,sys;sys.path.insert(0, os.getcwd());from src.trader.application.runtime_service import RuntimeService;from src.trader.interfaces.config import load_trader_config;print('RuntimeService import: OK')"
if errorlevel 1 (
    echo.
    echo ERROR: RuntimeService import failed in selected Python interpreter.
    echo This will force /v2/health=503 and block start.bat from launching the agent.
    pause
    exit /b 1
)

"%PY_EXE%" -m src.trader.cli bridge serve
pause
