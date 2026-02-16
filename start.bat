@echo off
setlocal enabledelayedexpansion
title MT4 Trading System
cd /d "%~dp0"
set PY=C:\Python311\python.exe

echo ============================================================
echo  MT4 TRADING SYSTEM - ONE-CLICK LAUNCHER
echo ============================================================
echo.

:: Kill anything on port 58710
echo [1/4] Cleaning up old instances...
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":58710.*LISTENING" 2^>nul') do (
    taskkill /f /pid %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

:: Start bridge
echo [2/4] Starting Bridge Server (port 58710)...
set PYTHONPATH=%~dp0
start "MT4 Bridge Server" "%PY%" bridge_api\bridge.py

:: Wait for startup and VALIDATE
echo [3/4] Waiting for bridge to initialize...
set RETRIES=0
:check_bridge
timeout /t 2 /nobreak >nul
netstat -ano | findstr ":58710.*LISTENING" >nul
if %errorlevel%==0 (
    echo        SUCCESS: Bridge is listening on port 58710.
    goto start_agent
)
set /a RETRIES+=1
if %RETRIES% LSS 5 (
    echo        ...waiting (attempt %RETRIES%/5)
    goto check_bridge
)

echo.
echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
echo  ERROR: Bridge failed to start!
echo  Check the "MT4 Bridge Server" window for errors.
echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
pause
exit /b 1

:start_agent
:: Start agent
set EQUITY=10000
if not "%1"=="" set EQUITY=%1
echo [4/4] Starting Agent (equity=%EQUITY%)...
set PYTHONPATH=%~dp0\src
start "MT4 Trading Agent" "%PY%" src\run_fx.py --equity %EQUITY% --sleep 10

echo.
echo ============================================================
echo  ALL SYSTEMS LAUNCHED SUCCESSFULLY
echo ============================================================
echo  To stop: close the Bridge and Agent windows
echo ============================================================
pause
