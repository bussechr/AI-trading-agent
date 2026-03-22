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
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "MONITOR_LOG=%LOGDIR%\monitor_%BRIDGE_PORT%.log"
set "MONITOR_ERR_LOG=%LOGDIR%\monitor_%BRIDGE_PORT%.err.log"
set "MONITOR_PID=%LOGDIR%\monitor_%BRIDGE_PORT%.pid"
powershell -NoProfile -Command "$p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-m','src.trader.cli','monitor','confidence','--bridge-url','http://127.0.0.1:%BRIDGE_PORT%','--poll-seconds','%POLL_SECS%' -RedirectStandardOutput '%MONITOR_LOG%' -RedirectStandardError '%MONITOR_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%MONITOR_PID%' -Value $p.Id" >nul
exit /b 0

:run
"%TRADER_PYTHON_EXE%" -m src.trader.cli monitor confidence --bridge-url http://127.0.0.1:%BRIDGE_PORT% --poll-seconds %POLL_SECS%
exit /b %errorlevel%
