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
if /I "%ACTION%"=="endpoints" goto endpoints
if /I "%ACTION%"=="help" goto help
goto help

:live
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "REQUESTED_BRIDGE_PORT=%~3"
set "REQUESTED_DASHBOARD_PORT=%~4"
set "STACK_MUTATED=0"
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
echo  Agent mode: %FXSTACK_AGENT_MODE%
echo ============================================================

set "STEP=preclean_stop"
call "%~dp0ops\windows\90_stop_all.bat" >nul 2>&1
set "STACK_MUTATED=1"
set "STEP=resolve_endpoints"
call :resolve_endpoints "%REQUESTED_BRIDGE_PORT%" "%REQUESTED_DASHBOARD_PORT%"
if errorlevel 1 goto fail
echo [endpoints] bridge=%MT4_BRIDGE_URL% dashboard=%TRADER_DASHBOARD_URL%
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
call "%~dp0ops\windows\20_start_bridge.bat" --background %TRADER_BRIDGE_PORT%
if errorlevel 1 goto fail
set "STEP=start_runtime"
call "%~dp0ops\windows\21_start_runtime.bat" --background %EQUITY% %TRADER_BRIDGE_PORT%
if errorlevel 1 goto fail
set "STEP=start_dashboard"
call "%~dp0ops\windows\22_start_dashboard.bat" --background %TRADER_DASHBOARD_PORT%
if errorlevel 1 goto fail
set "STEP=start_monitor"
call "%~dp0ops\windows\23_start_monitor.bat" --background %TRADER_BRIDGE_PORT% 2
if errorlevel 1 (
  echo [warn] monitor did not start cleanly; core stack is still up.
)

echo.
echo [ok] bridge   : %MT4_BRIDGE_URL%/v2/health
echo [ok] dashboard: %TRADER_DASHBOARD_URL%
echo [ok] logs     : logs\bridge_%TRADER_BRIDGE_PORT%.log ^| logs\runtime_%TRADER_BRIDGE_PORT%.log ^| logs\dashboard_%TRADER_DASHBOARD_PORT%.log ^| logs\monitor_%TRADER_BRIDGE_PORT%.log
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
for /f "usebackq tokens=1-10 delims=|" %%A in (`powershell -NoProfile -Command "function Clean([object]$value){$text=(''+$value).Replace('|','/'); if($text){$text}else{'-'}}; $hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri '%MT4_BRIDGE_URL%/v2/ready' -Headers $hdr -TimeoutSec 2; $bridge=if($j.bridge_up -eq $true){'up'}else{'down'}; $db=if($j.database_ok -eq $true){'ready'}else{'degraded'}; $runtime=if($j.runtime_ready -eq $true){'ready'}else{Clean $j.runtime_status}; $cycle=if($null -ne $j.runtime_cycle_age_secs){'{0:N1} s' -f [double]$j.runtime_cycle_age_secs}else{'n/a'}; $mt4=if($j.mt4_fresh -eq $true){'live'}else{Clean $j.mt4_status}; $heartbeat=if($null -ne $j.heartbeat_age_secs){'{0:N1} s' -f [double]$j.heartbeat_age_secs}else{'n/a'}; $ticks=if($j.ticks_fresh -eq $true){'live'}else{Clean $j.tick_status}; @($bridge,$db,$runtime,$cycle,(Clean $j.runtime_phase),(Clean $j.runtime_phase_pair),(Clean $j.runtime_failure_reason),$mt4,$heartbeat,$ticks) -join '|'} catch {'down|unknown|unknown|n/a|-|-|-|unknown|n/a|unknown'}"`) do (
  set "BRIDGE_API=%%A"
  set "DB_STATE=%%B"
  set "RUNTIME_STATE=%%C"
  set "RUNTIME_AGE=%%D"
  set "RUNTIME_PHASE=%%E"
  set "RUNTIME_PAIR=%%F"
  set "RUNTIME_FAILURE=%%G"
  set "MT4_STATE=%%H"
  set "BRIDGE_HB=%%I"
  set "TICKS_STATE=%%J"
)
if "%RUNTIME_PHASE%"=="-" set "RUNTIME_PHASE="
if "%RUNTIME_PAIR%"=="-" set "RUNTIME_PAIR="
if "%RUNTIME_FAILURE%"=="-" set "RUNTIME_FAILURE="
set "DASH=0"
for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri '%TRADER_DASHBOARD_URL%' -TimeoutSec 2).StatusCode} catch {0}"') do set "DASH=%%S"

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

:endpoints
call :resolve_endpoints "%~2" "%~3"
if errorlevel 1 exit /b %errorlevel%
echo Bridge URL   : %MT4_BRIDGE_URL%
echo Dashboard URL: %TRADER_DASHBOARD_URL%
exit /b 0

:help
echo Usage:
echo   launch_all.bat live [EQUITY] [BRIDGE_PORT] [DASHBOARD_PORT]
echo   launch_all.bat full [EQUITY]
echo   launch_all.bat status
echo   launch_all.bat endpoints [BRIDGE_PORT] [DASHBOARD_PORT]
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
if "%STACK_MUTATED%"=="1" (
  echo [cleanup] stopping the partially started repo-owned stack...
  call "%~dp0ops\windows\90_stop_all.bat" >nul 2>&1
  echo [error] stack is stopped; inspect the step log above and logs\*.err.log
) else (
  echo [error] no stack processes were changed; inspect the failed precheck above.
)
if "%DO_PAUSE%"=="1" pause
exit /b 1

:resolve_endpoints
setlocal enabledelayedexpansion
set "BRIDGE_REQUEST=%~1"
set "DASHBOARD_REQUEST=%~2"
set "STRICT_BRIDGE="
set "STRICT_DASHBOARD="
if defined BRIDGE_REQUEST set "STRICT_BRIDGE=-StrictBridge"
if defined DASHBOARD_REQUEST set "STRICT_DASHBOARD=-StrictDashboard"
if not defined BRIDGE_REQUEST set "BRIDGE_REQUEST=%TRADER_BRIDGE_PORT%"
if not defined DASHBOARD_REQUEST set "DASHBOARD_REQUEST=%TRADER_DASHBOARD_PORT%"
set "RESOLVED_BRIDGE="
set "RESOLVED_DASHBOARD="
for /f "usebackq tokens=1,2 delims=|" %%A in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0ops\windows\resolve_stack_endpoints.ps1" -BridgePort !BRIDGE_REQUEST! -DashboardPort !DASHBOARD_REQUEST! -StateFile "%~dp0logs\active_stack_env.bat" !STRICT_BRIDGE! !STRICT_DASHBOARD!`) do (
  set "RESOLVED_BRIDGE=%%A"
  set "RESOLVED_DASHBOARD=%%B"
)
if not defined RESOLVED_BRIDGE (
  echo [endpoints] ERROR: no bindable bridge port found from !BRIDGE_REQUEST!
  endlocal
  exit /b 2
)
if not defined RESOLVED_DASHBOARD (
  echo [endpoints] ERROR: no bindable dashboard port found from !DASHBOARD_REQUEST!
  endlocal
  exit /b 2
)
if not "!RESOLVED_BRIDGE!"=="!BRIDGE_REQUEST!" echo [endpoints] preferred bridge port !BRIDGE_REQUEST! unavailable; selected !RESOLVED_BRIDGE!
if not "!RESOLVED_DASHBOARD!"=="!DASHBOARD_REQUEST!" echo [endpoints] preferred dashboard port !DASHBOARD_REQUEST! unavailable; selected !RESOLVED_DASHBOARD!
endlocal & (
  set "TRADER_BRIDGE_PORT=%RESOLVED_BRIDGE%"
  set "TRADER_DASHBOARD_PORT=%RESOLVED_DASHBOARD%"
)
set "MT4_BRIDGE_URL=http://%TRADER_BRIDGE_HOST%:%TRADER_BRIDGE_PORT%"
set "TRADER_BRIDGE_URL=%MT4_BRIDGE_URL%"
set "TRADER_DASHBOARD_URL=http://%TRADER_DASHBOARD_HOST%:%TRADER_DASHBOARD_PORT%"
exit /b 0
