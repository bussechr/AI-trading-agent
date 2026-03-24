@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

"%TRADER_PYTHON_EXE%" -m tools.live_execution_smoke %*
exit /b %errorlevel%
