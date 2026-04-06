REM AGENT: ROLE: Launch the Next.js production dashboard and wait for HTTP readiness on `/`.
REM AGENT: ENTRYPOINT: `ops/windows/22_start_dashboard.bat --run|--background`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%NODE_EXE%`, production build artifacts, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: dashboard process, PID/log files, HTTP readiness result.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, `.next` build artifacts, `node.exe`.
REM AGENT: CALLED BY: operators and launch workflows.
REM AGENT: STATE / SIDE EFFECTS: starts/kills dashboard processes, writes PID/log files.
REM AGENT: HANDSHAKES: dashboard HTTP readiness and env propagation into Next.js server.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `components/dashboard-layout.tsx` -> `docs/agents/dashboard-dataflow.md`
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=3000"
set "BUILD_ID=%ROOT%\.next\BUILD_ID"
set "NEXT_BIN=%ROOT%\node_modules\next\dist\bin\next"
set "STANDALONE_SERVER=%ROOT%\.next\standalone\server.js"

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
call :reset_dashboard_processes %PORT% "%DASHBOARD_PID%" || exit /b %errorlevel%
call :require_dashboard_runtime || exit /b %errorlevel%
if exist "%STANDALONE_SERVER%" (
  powershell -NoProfile -Command "$env:PORT='%PORT%'; $env:HOSTNAME='127.0.0.1'; $quoted='\"%STANDALONE_SERVER%\"'; $p=Start-Process -FilePath '%NODE_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList $quoted -RedirectStandardOutput '%DASHBOARD_LOG%' -RedirectStandardError '%DASHBOARD_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%DASHBOARD_PID%' -Value $p.Id" >nul
) else (
  powershell -NoProfile -Command "$p=Start-Process -FilePath '%NODE_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList ('\"%NEXT_BIN%\" start -p %PORT%') -RedirectStandardOutput '%DASHBOARD_LOG%' -RedirectStandardError '%DASHBOARD_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%DASHBOARD_PID%' -Value $p.Id" >nul
)
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
call :cleanup_failed_start "%DASHBOARD_PID%"
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
call :reset_dashboard_processes %PORT% || exit /b %errorlevel%
call :require_dashboard_runtime || exit /b %errorlevel%
if exist "%STANDALONE_SERVER%" (
  set "PORT=%PORT%"
  set "HOSTNAME=127.0.0.1"
  "%NODE_EXE%" "%STANDALONE_SERVER%"
) else (
  "%NODE_EXE%" "%NEXT_BIN%" start -p %PORT%
)
exit /b %errorlevel%

:require_dashboard_runtime
if exist "%STANDALONE_SERVER%" (
  call :resolve_node || exit /b %errorlevel%
  exit /b 0
)
if not exist "%BUILD_ID%" (
  echo [dashboard] ERROR: missing production build artifact: %BUILD_ID%
  echo [dashboard] Run launch_all.bat live or ops\windows\02_sync_node.bat before starting the dashboard.
  exit /b 2
)
if not exist "%NEXT_BIN%" (
  echo [dashboard] ERROR: missing Next.js CLI entrypoint: %NEXT_BIN%
  echo [dashboard] Run ops\windows\02_sync_node.bat before starting the dashboard.
  exit /b 2
)
call :resolve_node || exit /b %errorlevel%
exit /b 0

:resolve_node
if defined NODE_EXE if exist "%NODE_EXE%" exit /b 0
for /f "delims=" %%N in ('where node 2^>nul') do if not defined NODE_EXE set "NODE_EXE=%%N"
if defined NODE_EXE if exist "%NODE_EXE%" exit /b 0
if exist "C:\Program Files\nodejs\node.exe" set "NODE_EXE=C:\Program Files\nodejs\node.exe"
if defined NODE_EXE if exist "%NODE_EXE%" exit /b 0
echo [dashboard] ERROR: unable to resolve node.exe
exit /b 2

:reset_dashboard_processes
setlocal
set "TARGET_PORT=%~1"
set "PID_FILE=%~2"
if not defined TARGET_PORT set "TARGET_PORT=3000"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P
  del /q "%PID_FILE%" >nul 2>&1
)
powershell -NoProfile -Command ^
  "$root=[System.IO.Path]::GetFullPath('%ROOT%');" ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $exe=[string]($_.ExecutablePath);" ^
  "  $owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "  $dashboard=($cmd -like '*next start -p *') -or ($cmd -like '*.next*standalone*server.js*');" ^
  "  $owned -and $dashboard" ^
  "} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %TARGET_PORT% } | ForEach-Object { $_.OwningProcess }"`) do (
  call :kill_repo_owned_pid %%K
)
for /l %%I in (1,1,10) do (
  set "PORT_BUSY=0"
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %TARGET_PORT% } | Select-Object -ExpandProperty OwningProcess"`) do set "PORT_BUSY=%%K"
  if "!PORT_BUSY!"=="0" goto port_clear
  powershell -NoProfile -Command "Start-Sleep -Milliseconds 500" >nul
)
if not "!PORT_BUSY!"=="0" (
  echo [dashboard] ERROR: port %TARGET_PORT% is already occupied by PID !PORT_BUSY!
  endlocal
  exit /b 2
)
:port_clear
endlocal
exit /b 0

:kill_repo_owned_pid
setlocal
set "TARGET_PID=%~1"
if not defined TARGET_PID exit /b 0
powershell -NoProfile -Command ^
  "$root=[System.IO.Path]::GetFullPath('%ROOT%');" ^
  "$targetPid=%TARGET_PID%;" ^
  "$proc=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $targetPid) -ErrorAction SilentlyContinue;" ^
  "if(-not $proc){exit 0}" ^
  "$cmd=[string]($proc.CommandLine);" ^
  "$exe=[string]($proc.ExecutablePath);" ^
  "$owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "$dashboard=($cmd -like '*next start -p *') -or ($cmd -like '*.next*standalone*server.js*');" ^
  "if($owned -and $dashboard){ Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue }"
endlocal
exit /b 0

:cleanup_failed_start
setlocal
set "PID_FILE=%~1"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P
  del /q "%PID_FILE%" >nul 2>&1
)
endlocal
exit /b 0
