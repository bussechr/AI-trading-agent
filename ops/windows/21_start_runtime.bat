REM AGENT: ROLE: Launch the live runtime process, wait on runtime startup phases, and surface failure context.
REM AGENT: ENTRYPOINT: `ops/windows/21_start_runtime.bat --run|--background`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, bridge port, equity seed, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: runtime process, PID/log files, readiness/failure console output.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, bridge `/v2/ready`, `src.trader.cli runtime run`.
REM AGENT: CALLED BY: operators, launch scripts, deployment workflows.
REM AGENT: STATE / SIDE EFFECTS: starts/kills runtime processes, writes PID/log files, queries bridge readiness.
REM AGENT: HANDSHAKES: runtime startup progress via `/v2/ready`, runtime failure context, env inheritance into the runtime child process.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/runtime/runner.py` -> `docs/agents/runtime-loop.md`
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "BRIDGE_PORT=%~3"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   21_start_runtime.bat --run [EQUITY] [BRIDGE_PORT]
echo   21_start_runtime.bat --background [EQUITY] [BRIDGE_PORT]
exit /b 2

REM AGENT FLOW: Background mode owns process reset, runtime spawn, and readiness wait. `:run` is the foreground debugging path.
:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "RUNTIME_LOG=%LOGDIR%\runtime_%BRIDGE_PORT%.log"
set "RUNTIME_ERR_LOG=%LOGDIR%\runtime_%BRIDGE_PORT%.err.log"
set "RUNTIME_PID=%LOGDIR%\runtime_%BRIDGE_PORT%.pid"
call :reset_runtime_processes %BRIDGE_PORT% "%RUNTIME_PID%" || exit /b %errorlevel%
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=http://127.0.0.1:%BRIDGE_PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=full_live"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
powershell -NoProfile -Command "$env:PYTHONUNBUFFERED='1'; $match='src.trader.cli runtime run'; $p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-u -m src.trader.cli runtime run --equity %EQUITY% --sleep 10' -RedirectStandardOutput '%RUNTIME_LOG%' -RedirectStandardError '%RUNTIME_ERR_LOG%' -WindowStyle Hidden -PassThru; $workerId=$p.Id; for($i=0; $i -lt 50; $i++){ $child=Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $p.Id) -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -like ('*' + $match + '*') } | Select-Object -First 1; if($child){ $workerId=$child.ProcessId; break }; Start-Sleep -Milliseconds 200 }; Set-Content -Path '%RUNTIME_PID%' -Value ([string]$workerId)" >nul
call :wait_runtime %BRIDGE_PORT%
if errorlevel 1 exit /b %errorlevel%
if /I "%FXSTACK_FEAST_ENABLED%"=="1" call "%~dp024_start_feature_push_worker.bat" --background
if /I not "%FXSTACK_FEAST_ENABLED%"=="1" if /I "%FXSTACK_FEATURE_PUSH_ENABLED%"=="1" call "%~dp024_start_feature_push_worker.bat" --background
exit /b 0

REM AGENT HANDSHAKE: Runtime readiness is driven by bridge `/v2/ready`; this script never inspects runtime internals directly.
:wait_runtime
set "P=%~1"
set "MAX_WAIT=%FXSTACK_RUNTIME_STARTUP_TIMEOUT_SECS%"
if not defined MAX_WAIT set "MAX_WAIT=180"
for /f "delims=0123456789" %%A in ("%MAX_WAIT%") do set "MAX_WAIT=180"
if "%MAX_WAIT%"=="" set "MAX_WAIT=180"
for /l %%I in (1,1,%MAX_WAIT%) do (
  set "RUNNING="
  set "RUNTIME_STATUS=unknown"
  set "RUNTIME_PHASE="
  set "RUNTIME_PAIR="
  set "RUNTIME_PROGRESS_AGE="
  set "RUNTIME_FAILURE="
  for /f "usebackq tokens=1-6 delims=|" %%A in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/ready' -Headers $hdr -TimeoutSec 2; $ready=if($j.runtime_ready -eq $true){'1'} else {'0'}; $status=(''+$j.runtime_status).Replace('|','/'); $phase=(''+$j.runtime_phase).Replace('|','/'); $pair=(''+$j.runtime_phase_pair).Replace('|','/'); $age=if($null -ne $j.runtime_last_progress_age_secs){('{0:N1}' -f [double]$j.runtime_last_progress_age_secs)} else {''}; $failure=(''+$j.runtime_failure_reason).Replace('|','/'); Write-Output ($ready + '|' + $status + '|' + $phase + '|' + $pair + '|' + $age + '|' + $failure)} catch {'0|unknown||||'}"`) do (
    set "RUNNING=%%A"
    set "RUNTIME_STATUS=%%B"
    set "RUNTIME_PHASE=%%C"
    set "RUNTIME_PAIR=%%D"
    set "RUNTIME_PROGRESS_AGE=%%E"
    set "RUNTIME_FAILURE=%%F"
  )
  if "!RUNNING!"=="1" (
    echo [runtime] runtime_status=running with fresh cycle timestamp detected via bridge :%P%
    exit /b 0
  )
  if /I "!RUNTIME_STATUS!"=="failed" (
    echo [runtime] ERROR: runtime startup failed via bridge :%P%
    echo [runtime] phase=!RUNTIME_PHASE! pair=!RUNTIME_PAIR! reason=!RUNTIME_FAILURE!
    call :cleanup_failed_start "%RUNTIME_PID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  if /I "!RUNTIME_STATUS!"=="stalled" (
    echo [runtime] ERROR: runtime startup stalled via bridge :%P%
    echo [runtime] phase=!RUNTIME_PHASE! pair=!RUNTIME_PAIR! progress_age_secs=!RUNTIME_PROGRESS_AGE!
    call :cleanup_failed_start "%RUNTIME_PID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [runtime] ERROR: runtime startup timeout via bridge :%P%
if defined RUNTIME_PHASE echo [runtime] phase=%RUNTIME_PHASE% pair=%RUNTIME_PAIR% progress_age_secs=%RUNTIME_PROGRESS_AGE%
call :cleanup_failed_start "%RUNTIME_PID%"
call :emit_runtime_failure_context %P%
exit /b 2

:emit_runtime_failure_context
setlocal
set "P=%~1"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/ready' -Headers $hdr -TimeoutSec 2; $j | ConvertTo-Json -Compress -Depth 4} catch {''}"`) do echo [runtime] ready payload: %%S
if defined RUNTIME_LOG if exist "%RUNTIME_LOG%" (
  echo [runtime] log: %RUNTIME_LOG%
  echo [runtime] --- recent log tail ---
  powershell -NoProfile -Command "Get-Content -Path '%RUNTIME_LOG%' -Tail 40"
)
if defined RUNTIME_ERR_LOG if exist "%RUNTIME_ERR_LOG%" (
  echo [runtime] err log: %RUNTIME_ERR_LOG%
  echo [runtime] --- recent error tail ---
  powershell -NoProfile -Command "Get-Content -Path '%RUNTIME_ERR_LOG%' -Tail 40"
)
endlocal
exit /b 0

:run
call :reset_runtime_processes %BRIDGE_PORT% "" || exit /b %errorlevel%
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=http://127.0.0.1:%BRIDGE_PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=full_live"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
echo [runtime] starting equity_seed=%EQUITY% (fallback only; MT4 heartbeat equity is authoritative) bridge=http://127.0.0.1:%BRIDGE_PORT%
"%TRADER_PYTHON_EXE%" -u -m src.trader.cli runtime run --equity %EQUITY% --sleep 10
exit /b %errorlevel%

:reset_runtime_processes
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
  "  $runtime=($cmd -like '*-m src.trader.cli runtime run*') -or ($cmd -like '*src.trader.cli runtime run*');" ^
  "  $runtime" ^
  "} | ForEach-Object { try { Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$_.ProcessId) -WindowStyle Hidden -Wait | Out-Null } catch {} }" >nul 2>&1
call :kill_wsl_repo_owned_processes >nul 2>&1
endlocal
exit /b 0

:kill_wsl_repo_owned_processes
setlocal
where wsl.exe >nul 2>&1 || exit /b 0
powershell -NoProfile -Command ^
  "if (Get-Command wsl.exe -ErrorAction SilentlyContinue) {" ^
  "  $wslScript = 'pids=$(ps -eo pid=,args= | grep -E ''src\.trader\.cli runtime run'' | grep -v grep | awk ''{print $1}''); for pid in $pids; do [ -n \"$pid\" ] || continue; kill -TERM \"$pid\" 2>/dev/null || true; done; sleep 1; pids=$(ps -eo pid=,args= | grep -E ''src\.trader\.cli runtime run'' | grep -v grep | awk ''{print $1}''); for pid in $pids; do [ -n \"$pid\" ] || continue; kill -KILL \"$pid\" 2>/dev/null || true; done';" ^
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
  "$runtime=($cmd -like '*-m src.trader.cli runtime run*') -or ($cmd -like '*src.trader.cli runtime run*');" ^
  "if(-not $runtime){ exit 0 }" ^
  "$killPid=$targetPid;" ^
  "if($proc.ParentProcessId -gt 0){ $parent=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $proc.ParentProcessId) -ErrorAction SilentlyContinue; if($parent){ $pcmd=[string]($parent.CommandLine); $pruntime=($pcmd -like '*-m src.trader.cli runtime run*') -or ($pcmd -like '*src.trader.cli runtime run*'); if($pruntime){ $killPid=$parent.ProcessId } } }" ^
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
