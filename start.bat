@echo off
setlocal enabledelayedexpansion
title MT4 Trading System
cd /d "%~dp0"

echo ============================================================
echo  MT4 TRADING SYSTEM - ONE-CLICK LAUNCHER
echo ============================================================
echo.

set "PY_EXE="
if defined TRADER_PYTHON_EXE if exist "%TRADER_PYTHON_EXE%" set "PY_EXE=%TRADER_PYTHON_EXE%"
if not defined PY_EXE if exist "%~dp0.venv\Scripts\python.exe" set "PY_EXE=%~dp0.venv\Scripts\python.exe"
if not defined PY_EXE if exist "C:\Python311\python.exe" set "PY_EXE=C:\Python311\python.exe"
if not defined PY_EXE if exist "C:\Python312\python.exe" set "PY_EXE=C:\Python312\python.exe"
if not defined PY_EXE set "PY_EXE=python"

set TRADER_PYTHON_EXE=%PY_EXE%
set PYTHONPATH=%~dp0
set MT4_BRIDGE_PROTOCOL=v2
if not defined MT4_BRIDGE_ENABLE_V1_COMPAT set MT4_BRIDGE_ENABLE_V1_COMPAT=1
if not defined TRADER_RUNTIME_DB_PATH set TRADER_RUNTIME_DB_PATH=%~dp0data\state\runtime_v2.db

echo Python: %TRADER_PYTHON_EXE%
echo Legacy v1 compatibility: %MT4_BRIDGE_ENABLE_V1_COMPAT%
echo Runtime DB: %TRADER_RUNTIME_DB_PATH%
echo.

echo [0/4] Python dependency preflight...
"%TRADER_PYTHON_EXE%" -c "import importlib.util,sys;mods=['flask','flask_cors','yaml','numpy','pandas','scipy','requests'];missing=[m for m in mods if importlib.util.find_spec(m) is None];print('Missing modules: ' + (', '.join(missing) if missing else 'none'));sys.exit(1 if missing else 0)"
if errorlevel 1 (
    echo        Installing missing modules from requirements.txt...
    "%TRADER_PYTHON_EXE%" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo.
        echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        echo  ERROR: Python dependency install failed.
        echo  Selected interpreter: %TRADER_PYTHON_EXE%
        echo  Manual fix: "%TRADER_PYTHON_EXE%" -m pip install -r requirements.txt
        echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
        pause
        exit /b 1
    )
)

:: Kill anything on port 58710
echo [1/4] Cleaning up old instances...
taskkill /f /fi "WINDOWTITLE eq MT4 Bridge Server*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq MT4 Trading Agent*" >nul 2>&1
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":58710.*LISTENING" 2^>nul') do (
    taskkill /f /pid %%p >nul 2>&1
)
timeout /t 2 /nobreak >nul

:: Start bridge
echo [2/4] Starting Bridge Server (port 58710, mode=v2-only)...
start "MT4 Bridge Server" /d "%~dp0" cmd /c run_bridge.bat

:: Wait for startup and VALIDATE
echo [3/4] Waiting for bridge to initialize...
set RETRIES=0
:check_bridge
timeout /t 2 /nobreak >nul
set HTTP_STATUS=0
set "HEALTH_BODY="
set "HEALTH_RAW="
for /f "usebackq delims=" %%s in (`powershell -NoProfile -Command "$status=0;$body='';try{$r=Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:58710/v2/health' -TimeoutSec 2;$status=[int]$r.StatusCode;$body=[string]$r.Content}catch{if ($_.Exception.Response){$status=[int]$_.Exception.Response.StatusCode.value__;$sr=New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream());$body=$sr.ReadToEnd()}};$body=$body -replace \"`r|`n\",\" \";Write-Output ($status.ToString()+'###'+$body)"`) do set "HEALTH_RAW=%%s"
for /f "tokens=1* delims=#" %%a in ("!HEALTH_RAW!") do (
    set HTTP_STATUS=%%a
    set "HEALTH_BODY=%%b"
)
if "!HTTP_STATUS!"=="200" (
    echo        SUCCESS: Bridge health endpoint is ready.
    goto start_agent
)
set /a RETRIES+=1
if %RETRIES% LSS 5 (
    if defined HEALTH_BODY (
        echo        ...waiting (attempt %RETRIES%/5, /v2/health=!HTTP_STATUS!, body=!HEALTH_BODY!)
    ) else (
        echo        ...waiting (attempt %RETRIES%/5, /v2/health=!HTTP_STATUS!)
    )
    goto check_bridge
)

if "%MT4_BRIDGE_ENABLE_V1_COMPAT%"=="1" (
    echo        /v2/health stayed non-200, but legacy compatibility is enabled.
    echo        Continuing to start agent in compatibility mode.
    goto start_agent
)

echo.
echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
echo  ERROR: Bridge failed readiness check!
echo  /v2/health did not return HTTP 200 after 5 attempts.
if defined HEALTH_BODY echo  Last health payload: !HEALTH_BODY!
echo  Check the "MT4 Bridge Server" window for errors.
echo !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
pause
exit /b 1

:start_agent
:: Start agent
set EQUITY=10000
if not "%1"=="" set EQUITY=%1
echo [4/4] Starting Agent (equity=%EQUITY%, proto=%MT4_BRIDGE_PROTOCOL%)...
start "MT4 Trading Agent" /d "%~dp0" cmd /c run_agent.bat %EQUITY%

echo.
echo ============================================================
echo  ALL SYSTEMS LAUNCHED SUCCESSFULLY
echo ============================================================
echo  To stop: close the Bridge and Agent windows
echo ============================================================
pause
