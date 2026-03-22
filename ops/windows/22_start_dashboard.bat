@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=3000"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   22_start_dashboard.bat --run [PORT]
echo   22_start_dashboard.bat --background [PORT]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "DASHBOARD_LOG=%LOGDIR%\dashboard_%PORT%.log"
set "DASHBOARD_ERR_LOG=%LOGDIR%\dashboard_%PORT%.err.log"
set "DASHBOARD_PID=%LOGDIR%\dashboard_%PORT%.pid"
powershell -NoProfile -Command "$p=Start-Process -FilePath 'cmd.exe' -WorkingDirectory '%ROOT%' -ArgumentList '/d','/c','pnpm exec next start -p %PORT%' -RedirectStandardOutput '%DASHBOARD_LOG%' -RedirectStandardError '%DASHBOARD_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%DASHBOARD_PID%' -Value $p.Id" >nul
call :wait_dash %PORT%
exit /b %errorlevel%

:wait_dash
set "P=%~1"
for /l %%I in (1,1,40) do (
  set "HTTP=0"
  for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%P%' -TimeoutSec 2).StatusCode} catch {0}"') do set "HTTP=%%S"
  if "!HTTP!"=="200" (
    echo [dashboard] ready on :%P%
    exit /b 0
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [dashboard] ERROR: readiness timeout on :%P%
if defined DASHBOARD_LOG if exist "%DASHBOARD_LOG%" (
  echo [dashboard] log: %DASHBOARD_LOG%
  echo [dashboard] --- recent log tail ---
  powershell -NoProfile -Command "Get-Content -Path '%DASHBOARD_LOG%' -Tail 40"
)
if defined DASHBOARD_ERR_LOG if exist "%DASHBOARD_ERR_LOG%" (
  echo [dashboard] err log: %DASHBOARD_ERR_LOG%
  echo [dashboard] --- recent error tail ---
  powershell -NoProfile -Command "Get-Content -Path '%DASHBOARD_ERR_LOG%' -Tail 40"
)
exit /b 2

:run
echo [dashboard] starting production server on :%PORT%
set "BUILD_ID=%ROOT%\.next\BUILD_ID"
if not exist "%BUILD_ID%" (
  echo [dashboard] ERROR: missing production build artifact: %BUILD_ID%
  echo [dashboard] Run launch_all.bat live or ops\windows\02_sync_node.bat before starting the dashboard.
  exit /b 2
)
pnpm exec next start -p %PORT%
exit /b %errorlevel%
