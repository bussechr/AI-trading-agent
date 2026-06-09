REM AGENT: ROLE: Launch the optimize-only autonomous self-correction supervisor.
REM AGENT: ENTRYPOINT: `ops/windows/29_start_self_correction_loop.bat --run|--background|--once`.
REM AGENT: PRIMARY INPUTS: `_env.bat`, optional interval minutes, existing scored-signal/backtest artifacts.
REM AGENT: PRIMARY OUTPUTS: artifacts/self_correction/latest.json, history.jsonl, cycle logs, optional DB experiment proposals.
REM AGENT: HANDSHAKES: runs `trader agent improve`; forces shadow/advisory mode and never enables broker execution.
@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "INTERVAL=%~2"
if not defined INTERVAL set "INTERVAL=%FXSTACK_SELF_CORRECT_INTERVAL_MINUTES%"
if not defined INTERVAL set "INTERVAL=360"

set "FXSTACK_AGENT_MODE=shadow"
set "FX_AGENT_EXECUTION_MODE=shadow"
set "FXSTACK_AUTONOMOUS_SELF_CORRECTION=1"
set "PYTHONUNBUFFERED=1"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run
if /I "%MODE%"=="--once" goto once

echo Usage:
echo   29_start_self_correction_loop.bat --run [INTERVAL_MINUTES]
echo   29_start_self_correction_loop.bat --background [INTERVAL_MINUTES]
echo   29_start_self_correction_loop.bat --once
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "SELF_CORRECT_LOG=%LOGDIR%\self_correction.log"
set "SELF_CORRECT_ERR_LOG=%LOGDIR%\self_correction.err.log"
set "SELF_CORRECT_PID=%LOGDIR%\self_correction.pid"
call :reset_self_correction "%SELF_CORRECT_PID%" || exit /b %errorlevel%
powershell -NoProfile -Command "$p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-u tools/autonomous_self_correction_supervisor.py --interval-minutes %INTERVAL%' -RedirectStandardOutput '%SELF_CORRECT_LOG%' -RedirectStandardError '%SELF_CORRECT_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%SELF_CORRECT_PID%' -Value ([string]$p.Id)" >nul
echo [self-correction] started optimize-only loop interval_minutes=%INTERVAL%
echo [self-correction] pid_file=%SELF_CORRECT_PID%
echo [self-correction] latest=%ROOT%\artifacts\self_correction\latest.json
exit /b 0

:run
call :reset_self_correction "" || exit /b %errorlevel%
"%TRADER_PYTHON_EXE%" -u tools/autonomous_self_correction_supervisor.py --interval-minutes %INTERVAL%
exit /b %errorlevel%

:once
"%TRADER_PYTHON_EXE%" -u tools/autonomous_self_correction_supervisor.py --once
exit /b %errorlevel%

:reset_self_correction
setlocal
set "PID_FILE=%~1"
if defined PID_FILE if exist "%PID_FILE%" (
  for /f "usebackq delims=" %%P in ("%PID_FILE%") do call :kill_repo_owned_pid %%P
  del /q "%PID_FILE%" >nul 2>&1
)
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  $cmd -like '*tools/autonomous_self_correction_supervisor.py*'" ^
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
  "if($cmd -notlike '*tools/autonomous_self_correction_supervisor.py*'){ exit 0 }" ^
  "Start-Process -FilePath 'taskkill.exe' -ArgumentList '/F','/T','/PID',([string]$targetPid) -WindowStyle Hidden -Wait | Out-Null"
endlocal
exit /b 0
