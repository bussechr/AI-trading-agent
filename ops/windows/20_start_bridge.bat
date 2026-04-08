REM AGENT: ROLE: Launch the bridge API process, wait for `/v2/ready`, and surface startup logs on failure.
REM AGENT: ENTRYPOINT: `ops/windows/20_start_bridge.bat --run|--background`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, bridge port, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: bridge process, PID/log files, readiness result.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, `src.trader.cli bridge serve`.
REM AGENT: CALLED BY: operators and launch workflows.
REM AGENT: STATE / SIDE EFFECTS: starts/kills bridge processes, writes PID/log files.
REM AGENT: HANDSHAKES: bridge `/v2/ready` readiness contract used by runtime, dashboard, and ops.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/api/app.py` -> `docs/agents/bridge-and-api-handshakes.md`
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   20_start_bridge.bat --run [PORT]
echo   20_start_bridge.bat --background [PORT]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "BRIDGE_LOG=%LOGDIR%\bridge_%PORT%.log"
set "BRIDGE_ERR_LOG=%LOGDIR%\bridge_%PORT%.err.log"
set "BRIDGE_PID=%LOGDIR%\bridge_%PORT%.pid"
call :reset_bridge_processes %PORT% "%BRIDGE_PID%" || exit /b %errorlevel%
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_BRIDGE_PORT=%PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
powershell -NoProfile -Command "$env:PYTHONUNBUFFERED='1'; $match='src.trader.cli bridge serve'; $p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-u -m src.trader.cli bridge serve --host 127.0.0.1 --port %PORT%' -RedirectStandardOutput '%BRIDGE_LOG%' -RedirectStandardError '%BRIDGE_ERR_LOG%' -WindowStyle Hidden -PassThru; $workerId=$p.Id; for($i=0; $i -lt 50; $i++){ $child=Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $p.Id) -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -like ('*' + $match + '*') } | Select-Object -First 1; if($child){ $workerId=$child.ProcessId; break }; Start-Sleep -Milliseconds 200 }; Set-Content -Path '%BRIDGE_PID%' -Value ([string]$workerId)" >nul
call :wait_health %PORT%
exit /b %errorlevel%

:wait_health
set "P=%~1"
for /l %%I in (1,1,30) do (
  set "READY=0"
  for /f %%S in ('powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/ready' -Headers $hdr -TimeoutSec 2; if(($j.bridge_up -eq $true) -and ($j.database_ok -eq $true)){'1'} else {'0'}} catch {'0'}"') do set "READY=%%S"
  if "!READY!"=="1" (
    echo [bridge] ready on :%P%
    exit /b 0
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [bridge] ERROR: readiness timeout on :%P%
call :cleanup_failed_start "%BRIDGE_PID%"
if defined BRIDGE_LOG if exist "%BRIDGE_LOG%" (
  echo [bridge] log: %BRIDGE_LOG%
  echo [bridge] --- recent log tail ---
  powershell -NoProfile -Command "Get-Content -Path '%BRIDGE_LOG%' -Tail 40"
)
if defined BRIDGE_ERR_LOG if exist "%BRIDGE_ERR_LOG%" (
  echo [bridge] err log: %BRIDGE_ERR_LOG%
  echo [bridge] --- recent error tail ---
  powershell -NoProfile -Command "Get-Content -Path '%BRIDGE_ERR_LOG%' -Tail 40"
)
exit /b 2

:run
call :reset_bridge_processes %PORT% || exit /b %errorlevel%
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_BRIDGE_PORT=%PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
echo [bridge] starting on :%PORT%
"%TRADER_PYTHON_EXE%" -u -m src.trader.cli bridge serve --host 127.0.0.1 --port %PORT%
exit /b %errorlevel%

:reset_bridge_processes
setlocal enabledelayedexpansion
set "TARGET_PORT=%~1"
set "PID_FILE=%~2"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P
  del /q "%PID_FILE%" >nul 2>&1
)
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $bridge=($cmd -like '*-m src.trader.cli bridge serve*') -and ($cmd -like '*--port %TARGET_PORT%*');" ^
  "  $bridge" ^
  "} | ForEach-Object { try { Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$_.ProcessId) -WindowStyle Hidden -Wait | Out-Null } catch {} }" >nul 2>&1
call :kill_wsl_repo_owned_processes %TARGET_PORT% >nul 2>&1
for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %TARGET_PORT% } | Select-Object -ExpandProperty OwningProcess"`) do (
  call :kill_repo_owned_pid %%K
)
set "WAIT_SECS=%FXSTACK_PROCESS_EXIT_WAIT_SECS%"
if not defined WAIT_SECS set "WAIT_SECS=10"
for /l %%I in (1,1,!WAIT_SECS!) do (
  set "PORT_BUSY=0"
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %TARGET_PORT% } | Select-Object -ExpandProperty OwningProcess"`) do set "PORT_BUSY=%%K"
  if "!PORT_BUSY!"=="0" goto bridge_port_clear
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)
if not "!PORT_BUSY!"=="0" (
  echo [bridge] ERROR: port %TARGET_PORT% is already occupied by PID !PORT_BUSY!
  endlocal
  exit /b 2
)
:bridge_port_clear
endlocal
exit /b 0

:kill_wsl_repo_owned_processes
setlocal
set "TARGET_PORT=%~1"
where wsl.exe >nul 2>&1 || exit /b 0
powershell -NoProfile -Command ^
  "if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {" ^
  "  $wslScript = 'pids=$(ps -eo pid=,args= | grep -E ''src\.trader\.cli bridge serve.*--port %TARGET_PORT%'' | grep -v grep | awk ''{print $1}''); for pid in $pids; do [ -n \"$pid\" ] || continue; kill -TERM \"$pid\" 2>/dev/null || true; done; sleep 1; pids=$(ps -eo pid=,args= | grep -E ''src\.trader\.cli bridge serve.*--port %TARGET_PORT%'' | grep -v grep | awk ''{print $1}''); for pid in $pids; do [ -n \"$pid\" ] || continue; kill -KILL \"$pid\" 2>/dev/null || true; done';" ^
  "  & wsl.exe bash -lc $wslScript" ^
  "}"
endlocal
exit /b 0

:kill_repo_owned_pid
setlocal
set "TARGET_PID=%~1"
if not defined TARGET_PID exit /b 0
powershell -NoProfile -Command ^
  "$targetPid=%TARGET_PID%;" ^
  "$proc=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $targetPid) -ErrorAction SilentlyContinue;" ^
  "if(-not $proc){exit 0}" ^
  "$cmd=[string]($proc.CommandLine);" ^
  "$bridge=($cmd -like '*-m src.trader.cli bridge serve*') -or ($cmd -like '*src.trader.cli bridge serve*');" ^
  "if(-not $bridge){ exit 0 }" ^
  "$killPid=$targetPid;" ^
  "if($proc.ParentProcessId -gt 0){ $parent=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $proc.ParentProcessId) -ErrorAction SilentlyContinue; if($parent){ $pcmd=[string]($parent.CommandLine); $pbridge=($pcmd -like '*-m src.trader.cli bridge serve*') -or ($pcmd -like '*src.trader.cli bridge serve*'); if($pbridge){ $killPid=$parent.ProcessId } } }" ^
  "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$killPid) -WindowStyle Hidden -Wait | Out-Null"
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
