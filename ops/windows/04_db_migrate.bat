@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [db] migrate...
"%TRADER_PYTHON_EXE%" -I -m fxstack.runtime.db_tools migrate --project-root "%ROOT%"
if errorlevel 1 (
  echo [db] ERROR: migrate failed.
  exit /b 2
)

echo [db] verify...
"%TRADER_PYTHON_EXE%" -I -m fxstack.runtime.db_tools verify --project-root "%ROOT%"
if errorlevel 1 (
  echo [db] ERROR: verify failed.
  exit /b 2
)

echo [db] OK
exit /b 0
