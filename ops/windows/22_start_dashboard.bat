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
if not defined PORT set "PORT=%TRADER_DASHBOARD_PORT%"
set "DASHBOARD_HOST=%TRADER_DASHBOARD_HOST%"
if not defined DASHBOARD_HOST set "DASHBOARD_HOST=127.0.0.1"
set "DASHBOARD_URL=http://%DASHBOARD_HOST%:%PORT%"
set "BUILD_ID=%ROOT%\.next\BUILD_ID"
set "NEXT_BIN=%ROOT%\node_modules\next\dist\bin\next"
set "STANDALONE_SERVER=%ROOT%\.next\standalone\server.js"
set "DASHBOARD_LAUNCHER=%~dp022_start_dashboard.ps1"

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
call :reset_dashboard_processes %PORT% "%DASHBOARD_PID%"
if errorlevel 1 exit /b !errorlevel!
call :require_dashboard_runtime
if errorlevel 1 exit /b !errorlevel!
powershell -NoProfile -ExecutionPolicy Bypass -File "%DASHBOARD_LAUNCHER%" -NodeExe "%NODE_EXE%" -Root "%ROOT%" -Port "%PORT%" -HostName "%DASHBOARD_HOST%" -DashboardLog "%DASHBOARD_LOG%" -DashboardErrLog "%DASHBOARD_ERR_LOG%" -DashboardPid "%DASHBOARD_PID%" -NextBin "%NEXT_BIN%" -StandaloneServer "%STANDALONE_SERVER%" -PackageMode "%FXSTACK_PACKAGE_MODE%" >nul
call :wait_dash %PORT%
exit /b %errorlevel%

:wait_dash
set "P=%~1"
for /l %%I in (1,1,40) do (
  set "HTTP=0"
  for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri '%DASHBOARD_URL%' -TimeoutSec 2).StatusCode} catch {0}"') do set "HTTP=%%S"
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
call :reset_dashboard_processes %PORT%
if errorlevel 1 exit /b !errorlevel!
call :require_dashboard_runtime
if errorlevel 1 exit /b !errorlevel!
if /I "%FXSTACK_PACKAGE_MODE%"=="1" if exist "%STANDALONE_SERVER%" (
  set "PORT=%PORT%"
  set "HOSTNAME=%DASHBOARD_HOST%"
  "%NODE_EXE%" "%STANDALONE_SERVER%"
) else (
  "%NODE_EXE%" "%NEXT_BIN%" start -p %PORT% -H %DASHBOARD_HOST%
)
exit /b %errorlevel%

:require_dashboard_runtime
if /I "%FXSTACK_PACKAGE_MODE%"=="1" if exist "%STANDALONE_SERVER%" (
  call :resolve_node
  if errorlevel 1 exit /b !errorlevel!
  exit /b 0
)
if not exist "%NEXT_BIN%" (
  echo [dashboard] ERROR: missing Next.js CLI entrypoint: %NEXT_BIN%
  echo [dashboard] Run ops\windows\02_sync_node.bat before starting the dashboard.
  exit /b 2
)
call :resolve_node
if errorlevel 1 exit /b !errorlevel!
call :ensure_dashboard_build_current
if errorlevel 1 exit /b !errorlevel!
exit /b 0

:ensure_dashboard_build_current
call :dashboard_build_required
if errorlevel 1 exit /b !errorlevel!
if /I "%DASHBOARD_BUILD_REQUIRED%"=="0" exit /b 0
where pnpm >nul 2>&1
if errorlevel 1 (
  echo [dashboard] ERROR: pnpm not found; cannot refresh stale dashboard build.
  exit /b 2
)
if not exist "%BUILD_ID%" (
  echo [dashboard] production build missing; running pnpm build...
) else (
  echo [dashboard] production build is stale; rebuilding before start...
)
call cmd /c "pnpm build"
if errorlevel 1 (
  echo [dashboard] ERROR: pnpm build failed.
  exit /b 2
)
if not exist "%BUILD_ID%" (
  echo [dashboard] ERROR: dashboard build finished without producing %BUILD_ID%
  exit /b 2
)
exit /b 0

:dashboard_build_required
set "DASHBOARD_BUILD_REQUIRED=1"
if not exist "%BUILD_ID%" exit /b 0

set "BUILD_CHECK_RESULT="
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $root='%ROOT%'; $buildId=Join-Path $root '.next\BUILD_ID'; if(-not (Test-Path $buildId)){ 'BUILD'; exit 0 }; $buildTime=(Get-Item $buildId).LastWriteTimeUtc; $paths=@('package.json','pnpm-lock.yaml','next.config.js','next.config.mjs','postcss.config.js','postcss.config.mjs','tailwind.config.js','tailwind.config.ts','app','components','lib','scripts'); foreach($relative in $paths){ $target=Join-Path $root $relative; if(-not (Test-Path $target)){ continue }; if((Get-Item $target).PSIsContainer){ $newer=Get-ChildItem -Path $target -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTimeUtc -gt $buildTime } | Select-Object -First 1; if($null -ne $newer){ 'BUILD'; exit 0 } } elseif((Get-Item $target).LastWriteTimeUtc -gt $buildTime){ 'BUILD'; exit 0 } }; 'SKIP'"`) do set "BUILD_CHECK_RESULT=%%S"

if /I "%BUILD_CHECK_RESULT%"=="BUILD" (
  set "DASHBOARD_BUILD_REQUIRED=1"
  exit /b 0
)
if /I "%BUILD_CHECK_RESULT%"=="SKIP" (
  set "DASHBOARD_BUILD_REQUIRED=0"
  exit /b 0
)

echo [dashboard] ERROR: unable to determine dashboard build freshness.
exit /b 2

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
if not defined TARGET_PORT set "TARGET_PORT=%TRADER_DASHBOARD_PORT%"
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
  "  $dashboard=($cmd -like '*node_modules*next*dist*bin*next* start -p *') -or ($cmd -like '*.next*standalone*server.js*');" ^
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
powershell -NoProfile -Command "$listener=$null; try {$listener=[System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback,%TARGET_PORT%); $listener.Start(); exit 0} catch {exit 2} finally {if($null -ne $listener){try{$listener.Stop()}catch{}}}" >nul 2>&1
if errorlevel 1 (
  echo [dashboard] ERROR: port %TARGET_PORT% cannot be bound on loopback ^(it may be in a Windows excluded TCP range^)
  endlocal
  exit /b 2
)
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
  "$dashboard=($cmd -like '*node_modules*next*dist*bin*next* start -p *') -or ($cmd -like '*.next*standalone*server.js*');" ^
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
