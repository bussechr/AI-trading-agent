REM AGENT: ROLE: Launch the confidence monitor loop against the live bridge.
REM AGENT: ENTRYPOINT: `ops/windows/23_start_monitor.bat --run|--background`.
REM AGENT: PRIMARY INPUTS: `%ROOT%`, `%TRADER_PYTHON_EXE%`, bridge port, poll cadence, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: monitor process and PID/log files.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, `src.trader.cli monitor confidence`.
REM AGENT: CALLED BY: operators and launch workflows.
REM AGENT: STATE / SIDE EFFECTS: starts/kills monitor processes, writes PID/log files.
REM AGENT: HANDSHAKES: monitor reads bridge state/ready endpoints through the Python CLI.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `ops/windows/25_monitor_everything.ps1` -> `docs/agents/bridge-and-api-handshakes.md`
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "BRIDGE_PORT=%~2"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"
set "POLL_SECS=%~3"
if not defined POLL_SECS set "POLL_SECS=2"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   23_start_monitor.bat --run [BRIDGE_PORT] [POLL_SECS]
echo   23_start_monitor.bat --background [BRIDGE_PORT] [POLL_SECS]
exit /b 2

REM AGENT FLOW: Background mode owns process reset and detached launch; `:run` is the foreground debugging path.
:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "MONITOR_LOG=%LOGDIR%\monitor_%BRIDGE_PORT%.log"
set "MONITOR_ERR_LOG=%LOGDIR%\monitor_%BRIDGE_PORT%.err.log"
set "MONITOR_PID=%LOGDIR%\monitor_%BRIDGE_PORT%.pid"
call :reset_monitor_processes %BRIDGE_PORT% "%MONITOR_PID%" || exit /b %errorlevel%
powershell -NoProfile -Command "$env:PYTHONUNBUFFERED='1'; $match='src.trader.cli monitor confidence'; $p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-u -m src.trader.cli monitor confidence --bridge-url http://127.0.0.1:%BRIDGE_PORT% --poll-seconds %POLL_SECS%' -RedirectStandardOutput '%MONITOR_LOG%' -RedirectStandardError '%MONITOR_ERR_LOG%' -WindowStyle Hidden -PassThru; $workerId=$p.Id; for($i=0; $i -lt 50; $i++){ $child=Get-CimInstance Win32_Process -Filter ('ParentProcessId=' + $p.Id) -ErrorAction SilentlyContinue | Where-Object { ([string]$_.CommandLine) -like ('*' + $match + '*') } | Select-Object -First 1; if($child){ $workerId=$child.ProcessId; break }; Start-Sleep -Milliseconds 200 }; Set-Content -Path '%MONITOR_PID%' -Value ([string]$workerId)" >nul
exit /b 0

:run
"%TRADER_PYTHON_EXE%" -m src.trader.cli monitor confidence --bridge-url http://127.0.0.1:%BRIDGE_PORT% --poll-seconds %POLL_SECS%
exit /b %errorlevel%

:reset_monitor_processes
setlocal
set "TARGET_PORT=%~1"
set "PID_FILE=%~2"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P
  del /q "%PID_FILE%" >nul 2>&1
)
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $monitor=($cmd -like '*-m src.trader.cli monitor confidence*') -and ($cmd -like '*http://127.0.0.1:%TARGET_PORT%*');" ^
  "  $monitor" ^
  "} | ForEach-Object { try { Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$_.ProcessId) -WindowStyle Hidden -Wait | Out-Null } catch {} }" >nul 2>&1
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
  "$monitor=($cmd -like '*-m src.trader.cli monitor confidence*') -or ($cmd -like '*src.trader.cli monitor confidence*');" ^
  "if(-not $monitor){ exit 0 }" ^
  "$killPid=$targetPid;" ^
  "if($proc.ParentProcessId -gt 0){ $parent=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $proc.ParentProcessId) -ErrorAction SilentlyContinue; if($parent){ $pcmd=[string]($parent.CommandLine); $pmonitor=($pcmd -like '*-m src.trader.cli monitor confidence*') -or ($pcmd -like '*src.trader.cli monitor confidence*'); if($pmonitor){ $killPid=$parent.ProcessId } } }" ^
  "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$killPid) -WindowStyle Hidden -Wait | Out-Null"
endlocal
exit /b 0
