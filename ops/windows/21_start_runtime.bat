@echo off
REM AGENT: ROLE: Launch the live runtime process, wait on runtime startup phases, and surface failure context.
REM AGENT: ENTRYPOINT: `ops/windows/21_start_runtime.bat --validate|--binding-only|--validate-models|--run|--background [EQUITY] [BRIDGE_PORT]`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, bridge port, equity seed, baseline-only identity, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: runtime process, PID/log files, readiness/failure console output.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, bridge `/v2/ready`, isolated installed `fxstack.runtime.runner`.
REM AGENT: CALLED BY: operators, launch scripts, deployment workflows.
REM AGENT: STATE / SIDE EFFECTS: starts/kills runtime processes, writes PID/log files, queries bridge readiness.
REM AGENT: HANDSHAKES: runtime startup progress via `/v2/ready`, runtime failure context, env inheritance into the runtime child process.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/runtime/runner.py` -> `docs/agents/runtime-loop.md`
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "INSTANCE_INPUT=%~4"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=%FXSTACK_INSTANCE_ID%"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=baseline"
set "FXSTACK_INSTANCE_INPUT=%INSTANCE_INPUT%"
powershell -NoProfile -Command "if([string]::Equals([string]$env:FXSTACK_INSTANCE_INPUT,'baseline',[System.StringComparison]::Ordinal)){exit 0}else{exit 2}" >nul 2>&1
if errorlevel 1 (
  echo [runtime] ERROR: same-host runtime instance is quarantined; production admits only literal baseline.
  exit /b 2
)
set "FXSTACK_INSTANCE_INPUT="
set "INSTANCE_ID=baseline"
set "FXSTACK_INSTANCE_ID=baseline"
call :validate_runtime_loop_sleep
if errorlevel 1 exit /b %errorlevel%
call :resolve_launch_posture
if errorlevel 1 exit /b %errorlevel%
if /I "%MODE%"=="--binding-only" (
  "%TRADER_PYTHON_EXE%" -I -B -m fxstack.runtime.live_launch_authority_preflight --strategy-family "%FXSTACK_ENTRY_STRATEGY_FAMILY%" --binding-only
  exit /b !errorlevel!
)
if /I "%MODE%"=="--validate" (
  echo [runtime] launch posture valid profile=%FXSTACK_START_PROFILE% mode=%FXSTACK_AGENT_MODE% provider_shadow_only=%FXSTACK_PROVIDER_SHADOW_ONLY% shadow_24h=%FXSTACK_RUN_SHADOW_24H%
  exit /b 0
)
if /I not "%MODE%"=="--validate-models" if /I not "%MODE%"=="--run" if /I not "%MODE%"=="--background" goto usage
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "BRIDGE_PORT=%~3"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"
set "BRIDGE_HOST=%TRADER_BRIDGE_HOST%"
if not defined BRIDGE_HOST set "BRIDGE_HOST=127.0.0.1"
set "BRIDGE_URL=http://%BRIDGE_HOST%:%BRIDGE_PORT%"
set "MT4_BRIDGE_URL=%BRIDGE_URL%"
if /I not "%MODE%"=="--validate-models" if /I "%FXSTACK_START_PROFILE%"=="live" (
  call :validate_live_release_authority
  if errorlevel 1 exit /b !errorlevel!
)
call :preflight_active_models
if errorlevel 1 exit /b !errorlevel!
if /I "%MODE%"=="--validate-models" (
  if /I "%FXSTACK_ENTRY_STRATEGY_FAMILY%"=="mtvclc" (
    echo [runtime] launch posture valid; model preflight is not applicable to MTVCLC and runtime admission is resolved by runner startup.
    exit /b 0
  )
  echo [runtime] launch posture and active-model preflight valid profile=%FXSTACK_START_PROFILE% mode=%FXSTACK_AGENT_MODE%
  exit /b 0
)

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

:usage
echo Usage:
echo   21_start_runtime.bat --validate
echo   21_start_runtime.bat --binding-only
echo   21_start_runtime.bat --validate-models
echo   21_start_runtime.bat --run [EQUITY] [BRIDGE_PORT]
echo   21_start_runtime.bat --background [EQUITY] [BRIDGE_PORT]
exit /b 2

REM AGENT HANDSHAKE: The selected strategy launcher owns the bounded decision cadence; malformed or sub-second values fail before process mutation.
:validate_runtime_loop_sleep
set "RUNTIME_LOOP_SLEEP=%FXSTACK_RUNTIME_LOOP_SLEEP_SECS%"
if not defined RUNTIME_LOOP_SLEEP set "RUNTIME_LOOP_SLEEP=10"
set "FXSTACK_RUNTIME_LOOP_SLEEP_INPUT=%RUNTIME_LOOP_SLEEP%"
powershell -NoProfile -Command "$value=0; if([int]::TryParse([string]$env:FXSTACK_RUNTIME_LOOP_SLEEP_INPUT,[ref]$value) -and $value -ge 1 -and $value -le 60){exit 0}; exit 2" >nul 2>&1
set "FXSTACK_RUNTIME_LOOP_SLEEP_INPUT="
if errorlevel 1 (
  echo [runtime] ERROR: FXSTACK_RUNTIME_LOOP_SLEEP_SECS must be an integer from 1 through 60.
  exit /b 2
)
set "FXSTACK_RUNTIME_LOOP_SLEEP_SECS=%RUNTIME_LOOP_SLEEP%"
exit /b 0

REM AGENT HANDSHAKE: Validate the activation manifest and local payloads read-only before any runtime process or state mutation.
:preflight_active_models
if /I "%FXSTACK_ENTRY_STRATEGY_FAMILY%"=="mtvclc" (
  echo [runtime] skipping model-only active-model preflight for entry strategy family MTVCLC.
  exit /b 0
)
set "ACTIVE_MODEL_MANIFEST=%FXSTACK_MODEL_ACTIVATION_MANIFEST%"
if not defined ACTIVE_MODEL_MANIFEST set "ACTIVE_MODEL_MANIFEST=fx-quant-stack/artifacts/active_models.json"
echo [runtime] preflighting active models manifest=%ACTIVE_MODEL_MANIFEST% pairs=%FXSTACK_PAIRS%
"%TRADER_PYTHON_EXE%" -I -B -m fxstack.runtime.model_manifest_preflight --project-root "%ROOT%" --manifest "%ACTIVE_MODEL_MANIFEST%" --pairs "%FXSTACK_PAIRS%"
if errorlevel 1 (
  echo [runtime] ERROR: active-model preflight failed; runtime was not reset or started.
  exit /b 2
)
exit /b 0

REM AGENT HANDSHAKE: Resolve and validate execution posture before any process reset, PID/log write, or runtime spawn.
:resolve_launch_posture
if /I "%FXSTACK_START_PROFILE%"=="staged_safe" (
  if /I not "%FXSTACK_AGENT_MODE%"=="shadow" (
    echo [runtime] ERROR: FXSTACK_START_PROFILE=staged_safe requires FXSTACK_AGENT_MODE=shadow.
    exit /b 2
  )
  set "FXSTACK_START_PROFILE=staged_safe"
  set "FXSTACK_AGENT_MODE=shadow"
  exit /b 0
)
if /I "%FXSTACK_START_PROFILE%"=="paper" (
  echo [runtime] ERROR: FXSTACK_START_PROFILE=paper is unavailable in the production runtime distribution.
  exit /b 2
)
if /I "%FXSTACK_START_PROFILE%"=="live" (
  if /I not "%FXSTACK_AGENT_MODE%"=="live" (
    echo [runtime] ERROR: live startup requires explicit FXSTACK_AGENT_MODE=live.
    exit /b 2
  )
  if not "%FXSTACK_LIVE_ARMED%"=="1" (
    echo [runtime] ERROR: live startup requires explicit FXSTACK_LIVE_ARMED=1.
    exit /b 2
  )
  if /I "%FXSTACK_ENTRY_STRATEGY_FAMILY%"=="mtvclc" (
    if not "%FXSTACK_PROVIDER_SHADOW_ONLY%"=="0" (
      echo [runtime] ERROR: live MTVCLC requires FXSTACK_PROVIDER_SHADOW_ONLY=0.
      exit /b 2
    )
    if not "%FXSTACK_RUN_SHADOW_24H%"=="0" (
      echo [runtime] ERROR: live MTVCLC requires FXSTACK_RUN_SHADOW_24H=0.
      exit /b 2
    )
  )
  call :validate_live_scopes
  if errorlevel 1 exit /b 2
  call :validate_runtime_risk_limits
  if errorlevel 1 exit /b 2
  set "FXSTACK_START_PROFILE=live"
  set "FXSTACK_AGENT_MODE=live"
  exit /b 0
)
echo [runtime] ERROR: FXSTACK_START_PROFILE must be staged_safe or live.
exit /b 2

:validate_live_scopes
if not defined FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST.
  exit /b 2
)
set "LIVE_PAIR_SCOPE=%FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST: =%"
if not defined LIVE_PAIR_SCOPE (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST.
  exit /b 2
)
if not defined FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST.
  exit /b 2
)
set "LIVE_SLEEVE_SCOPE=%FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST: =%"
if not defined LIVE_SLEEVE_SCOPE (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST.
  exit /b 2
)
if not defined FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST.
  exit /b 2
)
set "LIVE_INTENT_SCOPE=%FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST: =%"
if not defined LIVE_INTENT_SCOPE (
  echo [runtime] ERROR: live startup requires an explicit non-empty FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST.
  exit /b 2
)
exit /b 0

REM AGENT HANDSHAKE: Live cannot spawn with disabled or non-finite lot, drawdown, gross, or net caps.
:validate_runtime_risk_limits
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0validate_runtime_risk_limits.ps1"
if errorlevel 1 exit /b 2
exit /b 0

REM AGENT HANDSHAKE: Every live launch validates signed release authority before
REM process reset. MTVCLC also repeats the same public verification in runner startup.
:validate_live_release_authority
if defined FXSTACK_LIVE_RELEASE_BINDING_SHA256 (
  "%TRADER_PYTHON_EXE%" -I -B -m fxstack.runtime.live_launch_authority_preflight --strategy-family "%FXSTACK_ENTRY_STRATEGY_FAMILY%" --expected-binding "!FXSTACK_LIVE_RELEASE_BINDING_SHA256!"
) else (
  "%TRADER_PYTHON_EXE%" -I -B -m fxstack.runtime.live_launch_authority_preflight --strategy-family "%FXSTACK_ENTRY_STRATEGY_FAMILY%"
)
if errorlevel 1 (
  echo [runtime] ERROR: active signed release authority validation failed; runtime was not reset or started.
  exit /b 2
)
exit /b 0

REM AGENT FLOW: Background mode owns process reset, runtime spawn, and readiness wait. `:run` is the foreground debugging path.
:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "RUNTIME_STEM=runtime_%BRIDGE_PORT%"
set "RUNTIME_LOG=%LOGDIR%\%RUNTIME_STEM%.log"
set "RUNTIME_ERR_LOG=%LOGDIR%\%RUNTIME_STEM%.err.log"
set "RUNTIME_PID=%LOGDIR%\%RUNTIME_STEM%.pid"
call :reset_runtime_processes "%INSTANCE_ID%" "%RUNTIME_PID%"
if errorlevel 1 exit /b !errorlevel!
set "PREVIOUS_RUNTIME_BOOT_ID="
for /f "usebackq delims=" %%B in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri '%BRIDGE_URL%/v2/ready' -Headers $hdr -TimeoutSec 2; $boot=(''+$j.runtime_boot_id).Trim(); if($boot){Write-Output $boot}} catch {}"`) do set "PREVIOUS_RUNTIME_BOOT_ID=%%B"
set "MT4_BRIDGE_URL=%BRIDGE_URL%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=%FXSTACK_AGENT_MODE%"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $env:PYTHONUNBUFFERED='1'; $match='fxstack.runtime.runner'; $quotedRoot=[char]34 + '%ROOT%' + [char]34; $quotedFeatureRoot=[char]34 + '%FXSTACK_RUNTIME_FEATURE_ROOT%' + [char]34; $arguments='-I -u -m fxstack.runtime.runner --equity %EQUITY% --sleep %RUNTIME_LOOP_SLEEP% --instance-root ' + $quotedRoot + ' --instance-id %INSTANCE_ID% --feature-root ' + $quotedFeatureRoot; $p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList $arguments -RedirectStandardOutput '%RUNTIME_LOG%' -RedirectStandardError '%RUNTIME_ERR_LOG%' -WindowStyle Hidden -PassThru; $workerId=$p.Id; for($i=0; $i -lt 50; $i++){ $child=Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $p.Id) -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -like ('*' + $match + '*') } | Select-Object -First 1; if($child){ $workerId=$child.ProcessId; break }; Start-Sleep -Milliseconds 200 }; if(-not $workerId){throw 'runtime_pid_unavailable'}; Set-Content -LiteralPath '%RUNTIME_PID%' -Value ([string]$workerId)" >nul
if errorlevel 1 (
  echo [runtime] ERROR: runtime process spawn failed.
  call :emit_runtime_failure_context %BRIDGE_PORT%
  exit /b 2
)
set "EXPECTED_RUNTIME_PID="
if exist "%RUNTIME_PID%" for /f "usebackq delims=" %%P in ("%RUNTIME_PID%") do set "EXPECTED_RUNTIME_PID=%%P"
for /f "delims=0123456789" %%A in ("!EXPECTED_RUNTIME_PID!") do set "EXPECTED_RUNTIME_PID="
if not defined EXPECTED_RUNTIME_PID (
  echo [runtime] ERROR: spawned runtime PID was not recorded.
  call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
  call :emit_runtime_failure_context %BRIDGE_PORT%
  exit /b 2
)
call :wait_runtime %BRIDGE_PORT% "!PREVIOUS_RUNTIME_BOOT_ID!" "!EXPECTED_RUNTIME_PID!"
if errorlevel 1 exit /b !errorlevel!
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

REM AGENT HANDSHAKE: Accept bridge readiness only for a new boot generation owned by the runtime PID spawned above.
:wait_runtime
set "P=%~1"
set "PREVIOUS_RUNTIME_BOOT_ID=%~2"
set "EXPECTED_RUNTIME_PID=%~3"
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
  set "RUNTIME_BOOT_ID="
  set "RUNTIME_OBSERVED_PID="
  for /f "usebackq tokens=1-8 delims=|" %%A in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri '%BRIDGE_URL%/v2/ready' -Headers $hdr -TimeoutSec 2; $ready=if($j.runtime_ready -eq $true){'1'} else {'0'}; $status=(''+$j.runtime_status).Replace('|','/'); if(-not $status){$status='unknown'}; $phase=(''+$j.runtime_phase).Replace('|','/'); if(-not $phase){$phase='-'}; $pair=(''+$j.runtime_phase_pair).Replace('|','/'); if(-not $pair){$pair='-'}; $age=if($null -ne $j.runtime_last_progress_age_secs){('{0:N1}' -f [double]$j.runtime_last_progress_age_secs)} else {'-'}; $failure=(''+$j.runtime_failure_reason).Replace('|','/'); if(-not $failure){$failure='-'}; $boot=(''+$j.runtime_boot_id).Trim(); if(-not $boot){$boot='-'}; $runtimePid=(''+$j.runtime_startup_summary.runtime_pid).Trim(); if(-not $runtimePid){$runtimePid='0'}; Write-Output ($ready + '|' + $status + '|' + $phase + '|' + $pair + '|' + $age + '|' + $failure + '|' + $boot + '|' + $runtimePid)} catch {'0|unknown|-|-|-|-|-|0'}"`) do (
    set "RUNNING=%%A"
    set "RUNTIME_STATUS=%%B"
    set "RUNTIME_PHASE=%%C"
    set "RUNTIME_PAIR=%%D"
    set "RUNTIME_PROGRESS_AGE=%%E"
    set "RUNTIME_FAILURE=%%F"
    set "RUNTIME_BOOT_ID=%%G"
    set "RUNTIME_OBSERVED_PID=%%H"
  )
  set "RUNTIME_GENERATION_MATCH=0"
  if not "!RUNTIME_BOOT_ID!"=="-" if /I not "!RUNTIME_BOOT_ID!"=="!PREVIOUS_RUNTIME_BOOT_ID!" if "!RUNTIME_OBSERVED_PID!"=="!EXPECTED_RUNTIME_PID!" set "RUNTIME_GENERATION_MATCH=1"
  if "!RUNTIME_GENERATION_MATCH!"=="1" if /I "!RUNTIME_STATUS!"=="failed" (
    echo [runtime] ERROR: runtime startup failed via bridge :%P%
    echo [runtime] phase=!RUNTIME_PHASE! pair=!RUNTIME_PAIR! reason=!RUNTIME_FAILURE!
    call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  if "!RUNTIME_GENERATION_MATCH!"=="1" if /I "!RUNTIME_STATUS!"=="stalled" (
    echo [runtime] ERROR: runtime startup stalled via bridge :%P%
    echo [runtime] phase=!RUNTIME_PHASE! pair=!RUNTIME_PAIR! progress_age_secs=!RUNTIME_PROGRESS_AGE!
    call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  powershell -NoProfile -Command "if(Get-Process -Id !EXPECTED_RUNTIME_PID! -ErrorAction SilentlyContinue){exit 0}; exit 1" >nul
  if errorlevel 1 (
    echo [runtime] ERROR: spawned runtime process !EXPECTED_RUNTIME_PID! exited before readiness.
    call :cleanup_failed_start "%RUNTIME_PID%" "%INSTANCE_ID%"
    call :emit_runtime_failure_context %P%
    exit /b 2
  )
  if "!RUNTIME_GENERATION_MATCH!"=="1" if "!RUNNING!"=="1" (
    echo [runtime] runtime_status=running boot_id=!RUNTIME_BOOT_ID! pid=!RUNTIME_OBSERVED_PID! with fresh cycle timestamp detected via bridge :%P%
    exit /b 0
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
set "MT4_BRIDGE_URL=%BRIDGE_URL%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=%FXSTACK_AGENT_MODE%"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
set "PYTHONUNBUFFERED=1"
echo [runtime] starting instance=%INSTANCE_ID% equity_seed=%EQUITY% (fallback only; MT4 heartbeat equity is authoritative) bridge=%BRIDGE_URL% loop_sleep_secs=%RUNTIME_LOOP_SLEEP%
"%TRADER_PYTHON_EXE%" -I -u -m fxstack.runtime.runner --equity %EQUITY% --sleep %RUNTIME_LOOP_SLEEP% --instance-root "%ROOT%" --instance-id %INSTANCE_ID% --feature-root "%FXSTACK_RUNTIME_FEATURE_ROOT%"
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
