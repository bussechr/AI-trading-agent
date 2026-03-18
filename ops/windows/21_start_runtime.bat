@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "BRIDGE_PORT=%~3"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   21_start_runtime.bat --run [EQUITY] [BRIDGE_PORT]
echo   21_start_runtime.bat --background [EQUITY] [BRIDGE_PORT]
exit /b 2

:bg
start "MT4 Trading Agent :%BRIDGE_PORT%" /d "%ROOT%" cmd /c "call \"%~f0\" --run %EQUITY% %BRIDGE_PORT%"
call :wait_runtime %BRIDGE_PORT%
exit /b %errorlevel%

:wait_runtime
set "P=%~1"
for /l %%I in (1,1,40) do (
  set "HB="
  for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/state' -TimeoutSec 2; if($j.last_heartbeat){'1'} else {'0'}} catch {'0'}"`) do set "HB=%%S"
  if "!HB!"=="1" (
    echo [runtime] heartbeat detected via bridge :%P%
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)

echo [runtime] ERROR: runtime heartbeat timeout via bridge :%P%
exit /b 2

:run
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=http://127.0.0.1:%BRIDGE_PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=full_live"
echo [runtime] starting equity=%EQUITY% bridge=http://127.0.0.1:%BRIDGE_PORT%
"%TRADER_PYTHON_EXE%" -m src.trader.cli runtime run --equity %EQUITY% --sleep 10
exit /b %errorlevel%
