@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "BRIDGE_PORT=%~2"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"
set "POLL_SECS=%~3"
if not defined POLL_SECS set "POLL_SECS=2"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   23_start_monitor.bat --run [BRIDGE_PORT] [POLL_SECS]
echo   23_start_monitor.bat --background [BRIDGE_PORT] [POLL_SECS]
exit /b 2

:bg
start "Trade Confidence Monitor :%BRIDGE_PORT%" /d "%ROOT%" cmd /c "call \"%~f0\" --run %BRIDGE_PORT% %POLL_SECS%"
exit /b 0

:run
"%TRADER_PYTHON_EXE%" -m src.trader.cli monitor confidence --bridge-url http://127.0.0.1:%BRIDGE_PORT% --poll-seconds %POLL_SECS%
exit /b %errorlevel%
