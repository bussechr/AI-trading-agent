REM AGENT: ROLE: Launch the Feast feature-push worker loop and keep the online store warm for live/runtime reads.
REM AGENT: ENTRYPOINT: `ops/windows/24_start_feature_push_worker.bat --run|--background [SLEEP_SECS] [--instance-id=ID]`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, Feast env from `_env.bat`, optional sleep interval and stack identity.
REM AGENT: PRIMARY OUTPUTS: background worker process, PID/log files, push-worker loop.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, `src.trader.cli features push-worker`, Feast repo config.
REM AGENT: CALLED BY: operators and `21_start_runtime.bat` when Feast/push is enabled.
REM AGENT: STATE / SIDE EFFECTS: starts/kills repo-owned feature-push worker processes and writes PID/log files.
REM AGENT: HANDSHAKES: runtime outbox -> Feast online store -> runtime online feature reads.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/feast/push.py`
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "SLEEP_SECS=%~2"
if not defined SLEEP_SECS set "SLEEP_SECS=%FXSTACK_FEATURE_PUSH_WORKER_SLEEP_SECS%"
if not defined SLEEP_SECS set "SLEEP_SECS=5"
set "INSTANCE_INPUT=%~3"
if /I "!INSTANCE_INPUT:~0,14!"=="--instance-id=" set "INSTANCE_INPUT=!INSTANCE_INPUT:~14!"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=%FXSTACK_INSTANCE_ID%"
if not defined INSTANCE_INPUT set "INSTANCE_INPUT=baseline"
set "FXSTACK_INSTANCE_INPUT=%INSTANCE_INPUT%"
set "INSTANCE_ID="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$value=([string]$env:FXSTACK_INSTANCE_INPUT).Trim(); if($value -match '^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$'){ $value.ToLowerInvariant() }"`) do set "INSTANCE_ID=%%I"
set "FXSTACK_INSTANCE_INPUT="
if not defined INSTANCE_ID (
  echo [feature-push-worker] ERROR: INSTANCE_ID must match ^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$.
  exit /b 2
)
set "FXSTACK_INSTANCE_ID=%INSTANCE_ID%"
if not defined FXSTACK_FEATURE_PUSH_WORKER_ID set "FXSTACK_FEATURE_PUSH_WORKER_ID=feature-push-worker"
set "INSTANCE_WORKER_ID=%FXSTACK_FEATURE_PUSH_WORKER_ID%"
if /I not "%INSTANCE_ID%"=="baseline" set "INSTANCE_WORKER_ID=%FXSTACK_FEATURE_PUSH_WORKER_ID%-%INSTANCE_ID%"
if not defined FXSTACK_FEATURE_PUSH_BATCH_SIZE set "FXSTACK_FEATURE_PUSH_BATCH_SIZE=50"
if not defined FXSTACK_FEATURE_PUSH_MAX_RETRIES set "FXSTACK_FEATURE_PUSH_MAX_RETRIES=5"
if not defined FXSTACK_FEATURE_PUSH_WORKER_STARTUP_TIMEOUT_SECS set "FXSTACK_FEATURE_PUSH_WORKER_STARTUP_TIMEOUT_SECS=60"
set "WORKER_LOOP=%~dp0feature_push_worker_loop.py"
set "WORKER_DB_URL=%FXSTACK_DATABASE_URL%"

if /I not "%FXSTACK_FEAST_ENABLED%"=="1" if /I not "%FXSTACK_FEATURE_PUSH_ENABLED%"=="1" (
  echo [feature-push-worker] skipped: FXSTACK_FEAST_ENABLED and FXSTACK_FEATURE_PUSH_ENABLED are both disabled
  exit /b 0
)

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   24_start_feature_push_worker.bat --run [SLEEP_SECS] [--instance-id=ID]
echo   24_start_feature_push_worker.bat --background [SLEEP_SECS] [--instance-id=ID]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "WORKER_STEM=feature_push_worker"
if /I not "%INSTANCE_ID%"=="baseline" set "WORKER_STEM=feature_push_worker_%INSTANCE_ID%"
set "WORKER_LOG=%LOGDIR%\%WORKER_STEM%.log"
set "WORKER_ERR_LOG=%LOGDIR%\%WORKER_STEM%.err.log"
set "WORKER_PID=%LOGDIR%\%WORKER_STEM%.pid"
call :reset_worker_processes "%WORKER_PID%" "%INSTANCE_ID%"
if errorlevel 1 exit /b !errorlevel!
if exist "%WORKER_ERR_LOG%" del /q "%WORKER_ERR_LOG%" >nul 2>&1
if exist "%WORKER_LOG%" del /q "%WORKER_LOG%" >nul 2>&1
powershell -NoProfile -Command "$p=Start-Process -FilePath '%~f0' -WorkingDirectory '%ROOT%' -ArgumentList @('--run','%SLEEP_SECS%','--instance-id=%INSTANCE_ID%') -RedirectStandardOutput '%WORKER_LOG%' -RedirectStandardError '%WORKER_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%WORKER_PID%' -Value ([string]$p.Id)" >nul
for /l %%I in (1,1,%FXSTACK_FEATURE_PUSH_WORKER_STARTUP_TIMEOUT_SECS%) do (
  set "WORKER_UP=0"
  set "WORKER_FAILED=0"
  set "WORKER_READY=0"
  if exist "%WORKER_PID%" (
    for /f "usebackq delims=" %%P in ("%WORKER_PID%") do (
      powershell -NoProfile -Command "if(Get-Process -Id %%P -ErrorAction SilentlyContinue){exit 0}else{exit 1}" >nul 2>&1
      if not errorlevel 1 set "WORKER_UP=1"
    )
  )
  if exist "%WORKER_LOG%" findstr /I /C:"[feature-push-worker] ready" "%WORKER_LOG%" >nul 2>&1 && set "WORKER_READY=1"
  if exist "%WORKER_LOG%" findstr /I /C:"Traceback" /C:"RuntimeError:" /C:"last_run_rc=" "%WORKER_LOG%" >nul 2>&1 && set "WORKER_FAILED=1"
  if exist "%WORKER_ERR_LOG%" findstr /I /C:"Traceback" /C:"RuntimeError:" /C:"last_run_rc=" "%WORKER_ERR_LOG%" >nul 2>&1 && set "WORKER_FAILED=1"
  if "!WORKER_FAILED!"=="1" (
    echo [feature-push-worker] ERROR: startup failed
    call :cleanup_failed_start "%WORKER_PID%" "%INSTANCE_ID%"
    if exist "%WORKER_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_LOG%' -Tail 40"
    if exist "%WORKER_ERR_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_ERR_LOG%' -Tail 40"
    exit /b 2
  )
  if "!WORKER_READY!"=="1" (
    echo [feature-push-worker] ready
    exit /b 0
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)
echo [feature-push-worker] ERROR: failed to start
call :cleanup_failed_start "%WORKER_PID%" "%INSTANCE_ID%"
if exist "%WORKER_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_LOG%' -Tail 40"
if exist "%WORKER_ERR_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_ERR_LOG%' -Tail 40"
exit /b 2

:run
echo [feature-push-worker] starting instance=%INSTANCE_ID% worker_id=%INSTANCE_WORKER_ID% sleep_secs=%SLEEP_SECS%
powershell -NoProfile -Command "$workerArgs=@('-u','%WORKER_LOOP%','--repo-root','%FXSTACK_FEAST_REPO_ROOT%','--sleep-secs','%SLEEP_SECS%','--worker-id','%INSTANCE_WORKER_ID%','--instance-id','%INSTANCE_ID%','--limit','%FXSTACK_FEATURE_PUSH_BATCH_SIZE%','--max-retries','%FXSTACK_FEATURE_PUSH_MAX_RETRIES%'); if('%FXSTACK_DATABASE_URL%'.Trim().Length -gt 0){ $workerArgs += @('--database-url','%FXSTACK_DATABASE_URL%') }; & '%TRADER_PYTHON_EXE%' @workerArgs"
exit /b %errorlevel%

:reset_worker_processes
setlocal enabledelayedexpansion
set "PID_FILE=%~1"
set "TARGET_INSTANCE=%~2"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P "%TARGET_INSTANCE%"
  del /q "%PID_FILE%" >nul 2>&1
)
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_owned_instance_processes.ps1" -Root "%ROOT%" -Role feature-push -InstanceId "%TARGET_INSTANCE%" 2^>nul`) do call :kill_repo_owned_pid %%P "%TARGET_INSTANCE%"
endlocal
exit /b 0

:kill_repo_owned_pid
setlocal enabledelayedexpansion
set "TARGET_PID=%~1"
set "TARGET_INSTANCE=%~2"
if not defined TARGET_PID exit /b 0
set "MATCHED_PID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0find_owned_instance_processes.ps1" -Root "%ROOT%" -Role feature-push -InstanceId "%TARGET_INSTANCE%" -ProcessId %TARGET_PID% 2^>nul`) do set "MATCHED_PID=%%P"
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
