@echo off
setlocal

title Trade Confidence Monitor
cd /d "%~dp0"

set BRIDGE_URL=http://127.0.0.1:58710
if not "%1"=="" set BRIDGE_URL=%1

set POLL_SECS=2
if not "%2"=="" set POLL_SECS=%2

echo ============================================================
echo  TRADE CONFIDENCE MONITOR
echo ============================================================
echo  Bridge URL : %BRIDGE_URL%
echo  Poll secs  : %POLL_SECS%
echo ============================================================
echo.

if exist "C:\Python311\python.exe" (
    "C:\Python311\python.exe" -m src.trader.cli monitor confidence --bridge-url %BRIDGE_URL% --poll-seconds %POLL_SECS%
) else if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" -m src.trader.cli monitor confidence --bridge-url %BRIDGE_URL% --poll-seconds %POLL_SECS%
) else (
    python -m src.trader.cli monitor confidence --bridge-url %BRIDGE_URL% --poll-seconds %POLL_SECS%
)

pause
