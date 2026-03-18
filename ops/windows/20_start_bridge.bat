@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   20_start_bridge.bat --run [PORT]
echo   20_start_bridge.bat --background [PORT]
exit /b 2

:bg
start "MT4 Bridge Server :%PORT%" /d "%ROOT%" cmd /c "call \"%~f0\" --run %PORT%"
call :wait_health %PORT%
exit /b %errorlevel%

:wait_health
set "P=%~1"
for /l %%I in (1,1,30) do (
  set "HTTP=0"
  for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%P%/v2/health' -TimeoutSec 2).StatusCode} catch {0}"') do set "HTTP=%%S"
  if "!HTTP!"=="200" (
    echo [bridge] ready on :%P%
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)

echo [bridge] ERROR: readiness timeout on :%P%
exit /b 2

:run
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_BRIDGE_PORT=%PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
echo [bridge] starting on :%PORT%
"%TRADER_PYTHON_EXE%" -m src.trader.cli bridge serve --host 127.0.0.1 --port %PORT%
exit /b %errorlevel%
