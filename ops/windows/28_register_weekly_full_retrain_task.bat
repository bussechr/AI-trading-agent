@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "TASK_NAME=TradingAgentWeeklyFullRetrain"
set "TASK_TIME=%FXSTACK_WEEKLY_FULL_RETRAIN_TIME%"
if "%TASK_TIME%"=="" set "TASK_TIME=03:00"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp028_register_weekly_full_retrain_task.ps1" ^
  -TaskName "%TASK_NAME%" ^
  -TaskTime "%TASK_TIME%"

exit /b %errorlevel%
