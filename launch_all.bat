@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

set "DO_PAUSE=1"
if /I "%LAUNCH_NO_PAUSE%"=="1" set "DO_PAUSE=0"

set "ACTION=%~1"
if not defined ACTION set "ACTION=live"

if /I "%ACTION%"=="live" goto live
if /I "%ACTION%"=="full" goto full
if /I "%ACTION%"=="stop" goto stop
if /I "%ACTION%"=="status" goto status
if /I "%ACTION%"=="help" goto help
goto help

:live
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "STEP=init"
if not defined FXSTACK_REQUIRE_CUDA set "FXSTACK_REQUIRE_CUDA=0"
set "STEP=select_database"
call :auto_db_fallback
if errorlevel 1 goto fail

echo ============================================================
echo  LAUNCH ALL ^(LIVE STACK^)
echo ============================================================
echo  Equity: %EQUITY%
echo  Require CUDA: %FXSTACK_REQUIRE_CUDA%
echo  Database: %FXSTACK_DATABASE_URL%
echo ============================================================

set "STEP=sync_python"
call "%~dp0ops\windows\01_sync_python.bat"
if errorlevel 1 goto fail
set "STEP=sync_node"
call "%~dp0ops\windows\02_sync_node.bat"
if errorlevel 1 goto fail
set "STEP=preflight"
call "%~dp0ops\windows\00_preflight.bat"
if errorlevel 1 goto fail
set "STEP=postgres_start"
call "%~dp0ops\windows\03_postgres_start.bat"
if errorlevel 1 goto fail
set "STEP=db_migrate_verify"
call "%~dp0ops\windows\04_db_migrate.bat"
if errorlevel 1 goto fail

set "STEP=start_bridge"
call "%~dp0ops\windows\20_start_bridge.bat" --background 58710
if errorlevel 1 goto fail
set "STEP=start_runtime"
call "%~dp0ops\windows\21_start_runtime.bat" --background %EQUITY% 58710
if errorlevel 1 goto fail
set "STEP=start_dashboard"
call "%~dp0ops\windows\22_start_dashboard.bat" --background 3000
if errorlevel 1 goto fail
set "STEP=start_monitor"
call "%~dp0ops\windows\23_start_monitor.bat" --background 58710 2
if errorlevel 1 goto fail

echo.
echo [ok] bridge   : http://127.0.0.1:58710/v2/health
echo [ok] dashboard: http://127.0.0.1:3000
echo [ok] stop cmd : launch_all.bat stop
if "%DO_PAUSE%"=="1" pause
exit /b 0

:full
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
call "%~dp0ops\windows\40_full_scale_e2e_validation.bat" %EQUITY%
exit /b %errorlevel%

:stop
call "%~dp0ops\windows\90_stop_all.bat"
exit /b %errorlevel%

:status
set "BRIDGE=down"
set "BRIDGE_HB="
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/health' -TimeoutSec 2; if($j.status){$j.status}else{'down'}} catch {'down'}"`) do set "BRIDGE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/health' -TimeoutSec 2; if($j.last_heartbeat){$j.last_heartbeat}else{''}} catch {''}"`) do set "BRIDGE_HB=%%S"
set "DASH=0"
for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000' -TimeoutSec 2).StatusCode} catch {0}"') do set "DASH=%%S"

echo Bridge status    : %BRIDGE%
echo Bridge heartbeat : %BRIDGE_HB%
echo Dashboard HTTP   : %DASH%
if "%DO_PAUSE%"=="1" pause
exit /b 0

:help
echo Usage:
echo   launch_all.bat live [EQUITY]
echo   launch_all.bat full [EQUITY]
echo   launch_all.bat status
echo   launch_all.bat stop
if "%DO_PAUSE%"=="1" pause
exit /b 2

:auto_db_fallback
setlocal enabledelayedexpansion
set "URL=%FXSTACK_DATABASE_URL%"
if /I "!URL:~0,6!"=="sqlite" (
  endlocal
  exit /b 0
)

if /I "!URL:localhost=!"=="!URL!" if /I "!URL:127.0.0.1=!"=="!URL!" (
  endlocal
  exit /b 0
)

set "HAS_PG=0"
if defined FXSTACK_PG_SERVICE_NAME (
  sc query "%FXSTACK_PG_SERVICE_NAME%" >nul 2>&1
  if !errorlevel! EQU 0 set "HAS_PG=1"
) else (
  for %%S in (postgresql-x64-17 postgresql-x64-16 postgresql-x64-15 postgresql-x64-14 postgresql-x64-13 postgresql postgresql-16 postgres) do (
    sc query "%%~S" >nul 2>&1
    if !errorlevel! EQU 0 set "HAS_PG=1"
  )
)

if "!HAS_PG!"=="1" (
  endlocal
  exit /b 0
)

endlocal & (
  set "FXSTACK_ALLOW_SQLITE=1"
  set "FXSTACK_DATABASE_URL=sqlite:///data/state/fxstack_runtime.db"
)
if not exist "%~dp0data\state" mkdir "%~dp0data\state" >nul 2>&1
echo [warn] no local postgres service detected; using sqlite fallback: %FXSTACK_DATABASE_URL%
exit /b 0

:fail
echo.
echo [error] launch failed at step: %STEP%
echo [error] Run: launch_all.bat stop
if "%DO_PAUSE%"=="1" pause
exit /b 1
