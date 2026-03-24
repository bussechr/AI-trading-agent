@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

"%TRADER_PYTHON_EXE%" -m tools.weekly_full_retrain_and_activate %*
exit /b %errorlevel%
