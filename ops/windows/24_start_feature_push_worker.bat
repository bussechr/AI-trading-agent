REM AGENT: ROLE: Launch the Feast feature-push worker loop and keep the online store warm for live/runtime reads.
REM AGENT: ENTRYPOINT: `ops/windows/24_start_feature_push_worker.bat --run|--background [SLEEP_SECS]`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, Feast env from `_env.bat`, optional sleep interval.
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
if not defined FXSTACK_FEATURE_PUSH_WORKER_ID set "FXSTACK_FEATURE_PUSH_WORKER_ID=feature-push-worker"
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
echo   24_start_feature_push_worker.bat --run [SLEEP_SECS]
echo   24_start_feature_push_worker.bat --background [SLEEP_SECS]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "WORKER_LOG=%LOGDIR%\feature_push_worker.log"
set "WORKER_ERR_LOG=%LOGDIR%\feature_push_worker.err.log"
set "WORKER_PID=%LOGDIR%\feature_push_worker.pid"
call :reset_worker_processes "%WORKER_PID%" || exit /b %errorlevel%
if exist "%WORKER_ERR_LOG%" del /q "%WORKER_ERR_LOG%" >nul 2>&1
if exist "%WORKER_LOG%" del /q "%WORKER_LOG%" >nul 2>&1
powershell -NoProfile -Command "$p=Start-Process -FilePath '%~f0' -WorkingDirectory '%ROOT%' -ArgumentList @('--run','%SLEEP_SECS%') -RedirectStandardOutput '%WORKER_LOG%' -RedirectStandardError '%WORKER_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%WORKER_PID%' -Value ([string]$p.Id)" >nul
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
    call :cleanup_failed_start "%WORKER_PID%"
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
call :cleanup_failed_start "%WORKER_PID%"
if exist "%WORKER_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_LOG%' -Tail 40"
if exist "%WORKER_ERR_LOG%" powershell -NoProfile -Command "Get-Content -Path '%WORKER_ERR_LOG%' -Tail 40"
exit /b 2

:run
echo [feature-push-worker] starting loop sleep_secs=%SLEEP_SECS%
powershell -NoProfile -Command "$workerArgs=@('-u','%WORKER_LOOP%','--repo-root','%FXSTACK_FEAST_REPO_ROOT%','--sleep-secs','%SLEEP_SECS%','--worker-id','%FXSTACK_FEATURE_PUSH_WORKER_ID%','--limit','%FXSTACK_FEATURE_PUSH_BATCH_SIZE%','--max-retries','%FXSTACK_FEATURE_PUSH_MAX_RETRIES%'); if('%FXSTACK_DATABASE_URL%'.Trim().Length -gt 0){ $workerArgs += @('--database-url','%FXSTACK_DATABASE_URL%') }; & '%TRADER_PYTHON_EXE%' @workerArgs"
exit /b %errorlevel%

:reset_worker_processes
setlocal
set "PID_FILE=%~1"
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
  "  $worker=($cmd -like '*24_start_feature_push_worker.bat --run*') -or ($cmd -like '*src.trader.cli features push-worker*') -or ($cmd -like '*feature_push_worker_loop.py*');" ^
  "  $owned -and $worker" ^
  "} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
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
  "$worker=($cmd -like '*24_start_feature_push_worker.bat --run*') -or ($cmd -like '*src.trader.cli features push-worker*') -or ($cmd -like '*feature_push_worker_loop.py*');" ^
  "if($owned -and $worker){ Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue }"
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
