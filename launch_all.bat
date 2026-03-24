@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"
call "%~dp0ops\windows\_env.bat" >nul 2>&1
if errorlevel 1 (
  echo [error] failed to load ops/windows/_env.bat
  exit /b 1
)

set "DO_PAUSE=0"
if defined LAUNCH_NO_PAUSE if /I not "%LAUNCH_NO_PAUSE%"=="0" set "DO_PAUSE=0"

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
set "STEP=enforce_live_database"
call :enforce_live_database
if errorlevel 1 goto fail

echo ============================================================
echo  LAUNCH ALL ^(LIVE STACK^)
echo ============================================================
echo  Equity: %EQUITY%
echo  Require CUDA: %FXSTACK_REQUIRE_CUDA%
echo  Database: %FXSTACK_DATABASE_URL%
echo ============================================================

set "STEP=preclean_stop"
call "%~dp0ops\windows\90_stop_all.bat" >nul 2>&1
set "STEP=sync_python"
if /I "%FXSTACK_PACKAGE_MODE%"=="1" (
  echo [sync-python] package mode; using bundled python runtime...
) else (
  call "%~dp0ops\windows\01_sync_python.bat"
  if errorlevel 1 goto fail
)
set "STEP=sync_node"
if /I "%FXSTACK_PACKAGE_MODE%"=="1" (
  echo [sync-node] package mode; using packaged dashboard runtime...
) else (
  call "%~dp0ops\windows\02_sync_node.bat"
  if errorlevel 1 goto fail
)
set "STEP=preflight"
call "%~dp0ops\windows\00_preflight.bat"
if errorlevel 1 goto fail
set "STEP=postgres_start"
call "%~dp0ops\windows\03_postgres_start.bat"
if errorlevel 1 goto fail
set "STEP=refine_database"
call :refine_local_db_fallback
if errorlevel 1 goto fail
set "STEP=enforce_live_database"
call :enforce_live_database
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
if errorlevel 1 (
  echo [warn] monitor did not start cleanly; core stack is still up.
)

echo.
echo [ok] bridge   : http://127.0.0.1:58710/v2/health
echo [ok] dashboard: http://127.0.0.1:3000
echo [ok] logs     : logs\bridge_58710.log ^| logs\runtime_58710.log ^| logs\dashboard_3000.log ^| logs\monitor_58710.log
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
set "BRIDGE_API=down"
set "DB_STATE=unknown"
set "RUNTIME_STATE=unknown"
set "RUNTIME_AGE="
set "RUNTIME_PHASE="
set "RUNTIME_PAIR="
set "RUNTIME_FAILURE="
set "MT4_STATE=unknown"
set "BRIDGE_HB="
set "TICKS_STATE=unknown"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.bridge_up -eq $true){'up'} else {'down'}} catch {'down'}"`) do set "BRIDGE_API=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.database_ok -eq $true){'ready'} else {'degraded'}} catch {'unknown'}"`) do set "DB_STATE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.runtime_ready -eq $true){'ready'} else {''+$j.runtime_status}} catch {'unknown'}"`) do set "RUNTIME_STATE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($null -ne $j.runtime_cycle_age_secs){('{0:N1} s' -f [double]$j.runtime_cycle_age_secs)} else {'n/a'}} catch {'n/a'}"`) do set "RUNTIME_AGE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; (''+$j.runtime_phase)} catch {''}"`) do set "RUNTIME_PHASE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; (''+$j.runtime_phase_pair)} catch {''}"`) do set "RUNTIME_PAIR=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; (''+$j.runtime_failure_reason)} catch {''}"`) do set "RUNTIME_FAILURE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.mt4_fresh -eq $true){'live'} else {''+$j.mt4_status}} catch {'unknown'}"`) do set "MT4_STATE=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($null -ne $j.heartbeat_age_secs){('{0:N1} s' -f [double]$j.heartbeat_age_secs)} else {'n/a'}} catch {'n/a'}"`) do set "BRIDGE_HB=%%S"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:58710/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.ticks_fresh -eq $true){'live'} else {''+$j.tick_status}} catch {'unknown'}"`) do set "TICKS_STATE=%%S"
set "DASH=0"
for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:3000' -TimeoutSec 2).StatusCode} catch {0}"') do set "DASH=%%S"

echo Bridge API       : %BRIDGE_API%
echo Database         : %DB_STATE%
echo Runtime          : %RUNTIME_STATE%
echo Runtime cycle    : %RUNTIME_AGE%
if /I not "%RUNTIME_STATE%"=="ready" (
  if defined RUNTIME_PHASE echo Runtime phase    : %RUNTIME_PHASE%
  if defined RUNTIME_PAIR echo Runtime pair     : %RUNTIME_PAIR%
  if defined RUNTIME_FAILURE echo Runtime failure  : %RUNTIME_FAILURE%
)
echo MT4              : %MT4_STATE%
echo Heartbeat age    : %BRIDGE_HB%
echo Ticks            : %TICKS_STATE%
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

:refine_local_db_fallback
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

"%TRADER_PYTHON_EXE%" -m src.trader.cli db ping >nul 2>&1
if !errorlevel! EQU 0 (
  endlocal
  exit /b 0
)

endlocal & (
  set "FXSTACK_ALLOW_SQLITE=1"
  set "FXSTACK_DATABASE_URL=sqlite:///data/state/fxstack_runtime.db"
)
if not exist "%~dp0data\state" mkdir "%~dp0data\state" >nul 2>&1
echo [warn] local postgres connectivity failed after python sync; using sqlite fallback: %FXSTACK_DATABASE_URL%
exit /b 0

:enforce_live_database
if /I "%FXSTACK_LIVE_ALLOW_SQLITE_FALLBACK%"=="1" exit /b 0
setlocal
set "URL=%FXSTACK_DATABASE_URL%"
if /I "%URL:~0,6%"=="sqlite" (
  echo [error] live mode refuses sqlite fallback.
  echo [error] Restore local postgres or explicitly set FXSTACK_LIVE_ALLOW_SQLITE_FALLBACK=1.
  endlocal
  exit /b 2
)
endlocal
exit /b 0

:fail
echo.
echo [error] launch failed at step: %STEP%
echo [error] Run: launch_all.bat stop
if "%DO_PAUSE%"=="1" pause
exit /b 1
