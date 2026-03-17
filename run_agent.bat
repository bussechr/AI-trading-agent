@echo off
title MT4 Trading Agent
echo Starting Python Trading Agent via unified CLI...
echo.
cd /d "%~dp0"
set PYTHONPATH=%~dp0
if not defined TRADER_RUNTIME_DB_PATH set TRADER_RUNTIME_DB_PATH=%~dp0data\state\runtime_v2.db

echo Usage: run_agent.bat [EQUITY]
echo Default Equity: 10000
echo Protocol: v2-only
echo Runtime DB: %TRADER_RUNTIME_DB_PATH%
echo.

set EQUITY=10000
if not "%1"=="" set EQUITY=%1
set MT4_BRIDGE_PROTOCOL=v2
set FX_AGENT_EXECUTION_MODE=full_live
echo Protocol: %MT4_BRIDGE_PROTOCOL%
echo Execution mode: %FX_AGENT_EXECUTION_MODE%

set "PY_EXE="
if defined TRADER_PYTHON_EXE if exist "%TRADER_PYTHON_EXE%" set "PY_EXE=%TRADER_PYTHON_EXE%"
if not defined PY_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PY_EXE if exist "C:\Python311\python.exe" set "PY_EXE=C:\Python311\python.exe"
if not defined PY_EXE if exist "C:\Python312\python.exe" set "PY_EXE=C:\Python312\python.exe"
if not defined PY_EXE set "PY_EXE=python"

echo Python: %PY_EXE%
echo.

echo Running agent preflight checks...
"%PY_EXE%" -c "import importlib.util,sys;mods=['yaml','numpy','pandas','scipy','requests'];missing=[m for m in mods if importlib.util.find_spec(m) is None];print('Missing modules: ' + (', '.join(missing) if missing else 'none'));sys.exit(1 if missing else 0)"
if errorlevel 1 (
    echo.
    echo ERROR: Selected Python is missing dependencies required for runtime agent.
    echo Install with:
    echo   "%PY_EXE%" -m pip install -r requirements.txt
    pause
    exit /b 1
)

"%PY_EXE%" -m src.trader.cli runtime run --config src/config/fx_el_minis.yaml --equity %EQUITY% --sleep 10 --skip-validation
pause
