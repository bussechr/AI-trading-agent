REM AGENT: ROLE: Stop repo-owned bridge/runtime/dashboard/feature-push/monitor Windows processes and clear the runtime snapshot.
REM AGENT: ENTRYPOINT: `ops/windows/90_stop_all.bat`.
REM AGENT: PRIMARY INPUTS: PID files, repo-scoped process inspection, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: stopped repo-owned processes and cleared runtime snapshot state.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, repo log PID files, runtime service import for snapshot clear.
REM AGENT: CALLED BY: operators and recovery workflows.
REM AGENT: STATE / SIDE EFFECTS: kills repo-owned Windows processes and patches runtime state to `stopped`.
REM AGENT: HANDSHAKES: repo-scoped Windows stop semantics and runtime state patch reset.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/runtime/service.py` -> `docs/agents/runtime-loop.md`
@echo off
setlocal
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [stop] stopping known windows...

for /f "delims=" %%F in ('dir /b /a:-d "%ROOT%\logs\*.pid" 2^>nul') do (
  if exist "%ROOT%\logs\%%~F" (
    for /f "usebackq delims=" %%P in ("%ROOT%\logs\%%~F") do (
      call :kill_repo_owned_pid %%P
    )
    del /q "%ROOT%\logs\%%~F" >nul 2>&1
  )
)

for %%P in (%TRADER_BRIDGE_PORT% %TRADER_DASHBOARD_PORT% %FXSTACK_CANDIDATE_BRIDGE_PORT%) do (
  if not "%%P"=="" (
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %%P } | ForEach-Object { $_.OwningProcess }"`) do (
    call :kill_repo_owned_pid %%K
  )
  )
)

rem Kill repo-scoped workers even if PID files and port ownership are stale.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $exe=[string]($_.ExecutablePath);" ^
  "  $root=[System.IO.Path]::GetFullPath('%ROOT%');" ^
  "  $owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "  $dashboard=($cmd -like '*node_modules*next*dist*bin*next* start -p *') -or ($cmd -like '*.next*standalone*server.js*') -or ($cmd -like '*node_modules*next*dist*bin*next* build*');" ^
  "  $worker=($cmd -like '*-m src.trader.cli bridge serve*') -or ($cmd -like '*-m src.trader.cli runtime run*') -or ($cmd -like '*24_start_feature_push_worker.bat --run*') -or ($cmd -like '*-m src.trader.cli features push-worker*') -or ($cmd -like '*-m src.trader.cli monitor confidence*') -or $dashboard;" ^
  "  $owned -and $worker" ^
  "} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
if /I "%FXSTACK_STOP_KILL_ALL_PYTHON%"=="1" (
  echo [stop] WARN: FXSTACK_STOP_KILL_ALL_PYTHON=1, applying global python.exe kill
  taskkill /f /im python.exe >nul 2>&1
)
call :clear_runtime_snapshot >nul 2>&1
if exist "%ROOT%\logs\active_stack_env.bat" del /q "%ROOT%\logs\active_stack_env.bat" >nul 2>&1
if exist "%ROOT%\logs\active_candidate_env.bat" del /q "%ROOT%\logs\active_candidate_env.bat" >nul 2>&1

echo [stop] done
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
  "$worker=($cmd -like '*src.trader.cli bridge serve*') -or ($cmd -like '*src.trader.cli runtime run*') -or ($cmd -like '*src.trader.cli features push-worker*') -or ($cmd -like '*24_start_feature_push_worker.bat --run*') -or ($cmd -like '*src.trader.cli monitor confidence*') -or ($cmd -like '*node_modules*next*dist*bin*next* start -p*') -or ($cmd -like '*.next*standalone*server.js*') -or ($cmd -like '*next* build*');" ^
  "if(-not ($owned -and $worker)){ exit 0 }" ^
  "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$targetPid) -WindowStyle Hidden -Wait | Out-Null"
endlocal
exit /b 0

:clear_runtime_snapshot
setlocal
if not defined TRADER_PYTHON_EXE exit /b 0
"%TRADER_PYTHON_EXE%" -c "from fxstack.runtime.service import RuntimeService; from fxstack.settings import get_settings; s=get_settings(); svc=RuntimeService(database_url=s.database_url, default_session_id=s.default_session_id, command_ttl_secs=s.command_ttl_secs, requeue_age_secs=s.startup_requeue_age_secs, db_connect_retries=1); svc.patch_state({'runtime_status':'stopped','runtime_last_cycle_ts':0.0,'runtime_diag':{},'monitor':{},'agent_decisions':[],'agent_diagnostics':{},'system_status':'disconnected','last_heartbeat':None,'positions':[],'symbol_readiness':{},'symbol_ready_count':0,'unsupported_pairs':[],'equity':0.0,'margin':0.0,'freemargin':0.0,'__prune_stale__':True})" >nul 2>&1
endlocal
exit /b 0
