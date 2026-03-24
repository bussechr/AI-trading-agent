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

for %%P in (58710 58711 3000) do (
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %%P } | ForEach-Object { $_.OwningProcess }"`) do (
    call :kill_repo_owned_pid %%K
  )
)

rem Kill repo-scoped workers even if PID files and port ownership are stale.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $exe=[string]($_.ExecutablePath);" ^
  "  $root=[System.IO.Path]::GetFullPath('%ROOT%');" ^
  "  $owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "  $dashboard=($cmd -like '*next start -p *') -or ($cmd -like '*.next*standalone*server.js*') -or ($cmd -like '*node_modules*next*dist*bin*next* build*');" ^
  "  ($cmd -like '*-m src.trader.cli bridge serve*') -or" ^
  "  ($cmd -like '*-m src.trader.cli runtime run*') -or" ^
  "  ($cmd -like '*-m src.trader.cli monitor confidence*') -or" ^
  "  ($owned -and $dashboard)" ^
  "} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
if /I "%FXSTACK_STOP_KILL_ALL_PYTHON%"=="1" (
  echo [stop] WARN: FXSTACK_STOP_KILL_ALL_PYTHON=1, applying global python.exe kill
  taskkill /f /im python.exe >nul 2>&1
)
call :clear_runtime_snapshot >nul 2>&1

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
  "$name=[string]($proc.Name);" ^
  "$owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "$worker=($cmd -like '*src.trader.cli bridge serve*') -or ($cmd -like '*src.trader.cli runtime run*') -or ($cmd -like '*src.trader.cli monitor confidence*') -or ($cmd -like '*next start -p*') -or ($cmd -like '*.next*standalone*server.js*') -or ($cmd -like '*next* build*');" ^
  "if(-not $owned -and $name -match '^(python|python3|node|cmd)\\.exe$' -and $worker){ $owned=$true }" ^
  "if(-not $owned){ exit 0 }" ^
  "$killPid=$targetPid;" ^
  "if($worker -and $proc.ParentProcessId -gt 0){ $parent=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $proc.ParentProcessId) -ErrorAction SilentlyContinue; if($parent){ $pcmd=[string]($parent.CommandLine); $pworker=($pcmd -like '*src.trader.cli bridge serve*') -or ($pcmd -like '*src.trader.cli runtime run*') -or ($pcmd -like '*src.trader.cli monitor confidence*') -or ($pcmd -like '*next start -p*') -or ($pcmd -like '*.next*standalone*server.js*') -or ($pcmd -like '*next* build*'); if($pworker){ $killPid=$parent.ProcessId } } }" ^
  "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$killPid) -WindowStyle Hidden -Wait | Out-Null"
endlocal
exit /b 0

:clear_runtime_snapshot
setlocal
if not defined TRADER_PYTHON_EXE exit /b 0
if /I "%TRADER_PYTHON_EXE%"=="python" exit /b 0
"%TRADER_PYTHON_EXE%" -c "from fxstack.runtime.service import RuntimeService; from fxstack.settings import get_settings; s=get_settings(); svc=RuntimeService(database_url=s.database_url, default_session_id=s.default_session_id, command_ttl_secs=s.command_ttl_secs, requeue_age_secs=s.startup_requeue_age_secs, db_connect_retries=1); svc.patch_state({'runtime_status':'stopped','runtime_last_cycle_ts':0.0,'runtime_diag':{},'monitor':{},'agent_decisions':[],'agent_diagnostics':{},'system_status':'disconnected','last_heartbeat':None,'positions':[],'symbol_readiness':{},'symbol_ready_count':0,'unsupported_pairs':[],'equity':0.0,'margin':0.0,'freemargin':0.0,'__prune_stale__':True})" >nul 2>&1
endlocal
exit /b 0
