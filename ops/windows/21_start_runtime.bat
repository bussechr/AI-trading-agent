REM AGENT: ROLE: Launch the live runtime process, wait on runtime startup phases, and surface failure context.
REM AGENT: ENTRYPOINT: `ops/windows/21_start_runtime.bat --run|--background [EQUITY] [BRIDGE_PORT] [INSTANCE_ID]`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, bridge port, equity seed, instance identity, env from `_env.bat`.
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
set "BRIDGE_HOST=%TRADER_BRIDGE_HOST%"
if not defined BRIDGE_HOST set "BRIDGE_HOST=127.0.0.1"
set "BRIDGE_URL=http://%BRIDGE_HOST%:%BRIDGE_PORT%"
set "INSTANCE_INPUT=%~4"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=%FXSTACK_INSTANCE_ID%"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=baseline"
set "FXSTACK_INSTANCE_INPUT=%INSTANCE_INPUT%"
set "INSTANCE_ID="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$value=([string]$env:FXSTACK_INSTANCE_INPUT).Trim(); if($value -match '^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$'){ $value.ToLowerInvariant() }"`) do set "INSTANCE_ID=%%I"
set "FXSTACK_INSTANCE_INPUT="
if not defined INSTANCE_ID (
  echo [runtime] ERROR: INSTANCE_ID must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$.
  exit /b 2
)
set "FXSTACK_INSTANCE_ID=%INSTANCE_ID%"

REM AGENT STATE: Default the runtime to a smoke-safe mode unless an operator has already chosen one explicitly.
if not defined FXSTACK_AGENT_MODE (
  if /I "%FXSTACK_START_PROFILE%"=="staged_safe" (
    set "FXSTACK_AGENT_MODE=shadow"
  ) else if /I "%FXSTACK_START_PROFILE%"=="paper" (
    set "FXSTACK_AGENT_MODE=paper"
  ) else if /I "%FXSTACK_START_PROFILE%"=="live" (
    set "FXSTACK_AGENT_MODE=live"
  ) else (
    set "FXSTACK_AGENT_MODE=off"
  )
)

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   21_start_runtime.bat --run [EQUITY] [BRIDGE_PORT] [INSTANCE_ID]
echo   21_start_runtime.bat --background [EQUITY] [BRIDGE_PORT] [INSTANCE_ID]
exit /b 2

REM AGENT FLOW: Background mode owns process reset, runtime spawn, and readiness wait. `:run` is the foreground debugging path.
:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "RUNTIME_STEM=runtime_%BRIDGE_PORT%"
if /I not "%INSTANCE_ID%"=="baseline" set "RUNTIME_STEM=runtime_%INSTANCE_ID%_%BRIDGE_PORT%"
set "RUNTIME_LOG=%LOGDIR%\%RUNTIME_STEM%.log"
set "RUNTIME_ERR_LOG=%LOGDIR%\%RUNTIME_STEM%.err.log"
set "RUNTIME_PID=%LOGDIR%\%RUNTIME_STEM%.pid"
call :reset_runtime_processes "%INSTANCE_ID%" "%RUNTIME_PID%"
if errorlevel 1 exit /b !errorlevel!
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=%BRIDGE_URL%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=%FXSTACK_AGENT_MODE%"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
powershell -NoProfile -Command "$env:PYTHONUNBUFFERED='1'; $match='src.trader.cli runtime run'; $quotedRoot=[char]34 + '%ROOT%' + [char]34; $arguments='-u -m src.trader.cli runtime run --equity %EQUITY% --sleep 10 --instance-root ' + $quotedRoot + ' --instance-id %INSTANCE_ID%'; $p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList $arguments -RedirectStandardOutput '%RUNTIME_LOG%' -RedirectStandardError '%RUNTIME_ERR_LOG%' -WindowStyle Hidden -PassThru; $workerId=$p.Id; for($i=0; $i -lt 50; $i++){ $child=Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $p.Id) -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -like ('*' + $match + '*') } | Select-Object -First 1; if($child){ $workerId=$child.ProcessId; break }; Start-Sleep -Milliseconds 200 }; Set-Content -Path '%RUNTIME_PID%' -Value ([string]$workerId)" >nul
call :wait_runtime %BRIDGE_PORT%
if errorlevel 1 exit /b %errorlevel%
set "START_FEATURE_WORKER=0"
if /I "%FXSTACK_FEAST_ENABLED%"=="1" set "START_FEATURE_WORKER=1"
if /I "%FXSTACK_FEATURE_PUSH_ENABLED%"=="1" set "START_FEATURE_WORKER=1"
if "%START_FEATURE_WORKER%"=="1" (
  set "FEATURE_PUSH_SLEEP=%FXSTACK_FEATURE_PUSH_WORKER_SLEEP_SECS%"
  if not defined FEATURE_PUSH_SLEEP set "FEATURE_PUSH_SLEEP=5"
  call "%~dp024_start_feature_push_worker.bat" --background !FEATURE_PUSH_SLEEP! --instance-id=%INSTANCE_ID%
  if errorlevel 1 exit /b !errorlevel!
)
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
  for /f "usebackq tokens=1-6 delims=|" %%A in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri '%BRIDGE_URL%/v2/ready' -Headers $hdr -TimeoutSec 2; $ready=if($j.runtime_ready -eq $true){'1'} else {'0'}; $status=(''+$j.runtime_status).Replace('|','/'); $phase=(''+$j.runtime_phase).Replace('|','/'); $pair=(''+$j.runtime_phase_pair).Replace('|','/'); $age=if($null -ne $j.runtime_last_progress_age_secs){('{0:N1}' -f [double]$j.runtime_last_progress_age_secs)} else {''}; $failure=(''+$j.runtime_failure_reason).Replace('|','/'); Write-Output ($ready + '|' + $status + '|' + $phase + '|' + $pair + '|' + $age + '|' + $failure)} catch {'0|unknown||||'}"`) do (
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
    call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  if /I "!RUNTIME_STATUS!"=="stalled" (
    echo [runtime] ERROR: runtime startup stalled via bridge :%P%
    echo [runtime] phase=!RUNTIME_PHASE! pair=!RUNTIME_PAIR! progress_age_secs=!RUNTIME_PROGRESS_AGE!
    call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [runtime] ERROR: runtime startup timeout via bridge :%P%
if defined RUNTIME_PHASE echo [runtime] phase=%RUNTIME_PHASE% pair=%RUNTIME_PAIR% progress_age_secs=%RUNTIME_PROGRESS_AGE%
call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
call :emit_runtime_failure_context %P%
exit /b 2

:emit_runtime_failure_context
setlocal
set "P=%~1"
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri '%BRIDGE_URL%/v2/ready' -Headers $hdr -TimeoutSec 2; $j | ConvertTo-Json -Compress -Depth 4} catch {''}"`) do echo [runtime] ready payload: %%S
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
call :reset_runtime_processes "%INSTANCE_ID%" ""
if errorlevel 1 exit /b !errorlevel!
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=%BRIDGE_URL%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=%FXSTACK_AGENT_MODE%"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
echo [runtime] starting instance=%INSTANCE_ID% equity_seed=%EQUITY% (fallback only; MT4 heartbeat equity is authoritative) bridge=%BRIDGE_URL%
"%TRADER_PYTHON_EXE%" -u -m src.trader.cli runtime run --equity %EQUITY% --sleep 10 --instance-root "%ROOT%" --instance-id %INSTANCE_ID%
exit /b %errorlevel%

:reset_runtime_processes
setlocal enabledelayedexpansion
set "TARGET_INSTANCE=%~1"
set "PID_FILE=%~2"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P "%TARGET_INSTANCE%"
  del /q "%PID_FILE%" >nul 2>&1
)
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_owned_instance_processes.ps1" -Root "%ROOT%" -Role runtime -InstanceId "%TARGET_INSTANCE%" 2^>nul`) do call :kill_repo_owned_pid %%P "%TARGET_INSTANCE%"
endlocal
exit /b 0

:kill_repo_owned_pid
setlocal enabledelayedexpansion
set "TARGET_PID=%~1"
set "TARGET_INSTANCE=%~2"
if not defined TARGET_PID exit /b 0
set "MATCHED_PID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_owned_instance_processes.ps1" -Root "%ROOT%" -Role runtime -InstanceId "%TARGET_INSTANCE%" -ProcessId %TARGET_PID% 2^>nul`) do set "MATCHED_PID=%%P"
if not defined MATCHED_PID exit /b 0
powershell -NoProfile -Command "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID','%TARGET_PID%' -WindowStyle Hidden -Wait | Out-Null"
endlocal
exit /b 0

:cleanup_failed_start
setlocal
set "PID_FILE=%~1"
set "TARGET_INSTANCE=%~2"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P "%TARGET_INSTANCE%"
  del /q "%PID_FILE%" >nul 2>&1
)
endlocal
exit /b 0
