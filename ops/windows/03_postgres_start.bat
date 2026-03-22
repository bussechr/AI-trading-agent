@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "DB_URL=%FXSTACK_DATABASE_URL%"
if /I "%DB_URL:~0,6%"=="sqlite" (
  echo [postgres] sqlite database configured; skipping postgres service startup.
  echo [postgres] OK
  exit /b 0
)

set "PG_SERVICE="
if defined FXSTACK_PG_SERVICE_NAME (
  set "PG_SERVICE=%FXSTACK_PG_SERVICE_NAME%"
) else (
  for %%S in (postgresql-x64-17 postgresql-x64-16 postgresql-x64-15 postgresql-x64-14 postgresql-x64-13 postgresql postgresql-16 postgres) do (
    sc query "%%~S" >nul 2>&1
    if !errorlevel! EQU 0 if not defined PG_SERVICE set "PG_SERVICE=%%~S"
  )
)

if not defined PG_SERVICE (
  echo [postgres] ERROR: no local postgres service found.
  echo [postgres] Set FXSTACK_PG_SERVICE_NAME to your service name.
  exit /b 2
)

echo [postgres] using service: %PG_SERVICE%
sc query "%PG_SERVICE%" | findstr /I "RUNNING" >nul 2>&1
if errorlevel 1 (
  echo [postgres] starting service...
  sc start "%PG_SERVICE%" >nul 2>&1
)

for /l %%I in (1,1,30) do (
  "%TRADER_PYTHON_EXE%" -m src.trader.cli db ping >nul 2>&1
  if !errorlevel! EQU 0 (
    echo [postgres] ready.
    goto ok
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [postgres] ERROR: database readiness check failed.
exit /b 2

:ok
echo [postgres] OK
exit /b 0
