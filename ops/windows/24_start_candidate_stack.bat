@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined FXSTACK_CANDIDATE_BRIDGE_PORT set "FXSTACK_CANDIDATE_BRIDGE_PORT=58711"
if not defined FXSTACK_CANDIDATE_EQUITY set "FXSTACK_CANDIDATE_EQUITY=10000"
if defined FXSTACK_CANDIDATE_DATABASE_URL set "FXSTACK_DATABASE_URL=%FXSTACK_CANDIDATE_DATABASE_URL%"

echo [candidate] db migrate/verify...
call "%~dp004_db_migrate.bat"
if errorlevel 1 (
  echo [candidate] ERROR: db migrate/verify failed.
  exit /b 2
)

echo [candidate] starting bridge on :%FXSTACK_CANDIDATE_BRIDGE_PORT%
call "%~dp020_start_bridge.bat" --background %FXSTACK_CANDIDATE_BRIDGE_PORT%
if errorlevel 1 (
  echo [candidate] ERROR: bridge startup failed.
  exit /b 2
)

echo [candidate] starting runtime on :%FXSTACK_CANDIDATE_BRIDGE_PORT%
call "%~dp021_start_runtime.bat" --background %FXSTACK_CANDIDATE_EQUITY% %FXSTACK_CANDIDATE_BRIDGE_PORT%
if errorlevel 1 (
  echo [candidate] ERROR: runtime startup failed.
  exit /b 2
)

echo [candidate] OK
exit /b 0
