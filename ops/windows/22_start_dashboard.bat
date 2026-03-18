@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=3000"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   22_start_dashboard.bat --run [PORT]
echo   22_start_dashboard.bat --background [PORT]
exit /b 2

:bg
start "FX Dashboard :%PORT%" /d "%ROOT%" cmd /c "call \"%~f0\" --run %PORT%"
call :wait_dash %PORT%
exit /b %errorlevel%

:wait_dash
set "P=%~1"
for /l %%I in (1,1,40) do (
  set "HTTP=0"
  for /f %%S in ('powershell -NoProfile -Command "try {(Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:%P%' -TimeoutSec 2).StatusCode} catch {0}"') do set "HTTP=%%S"
  if "!HTTP!"=="200" (
    echo [dashboard] ready on :%P%
    exit /b 0
  )
  timeout /t 1 /nobreak >nul
)

echo [dashboard] ERROR: readiness timeout on :%P%
exit /b 2

:run
echo [dashboard] starting production server on :%PORT%
pnpm start -- -p %PORT%
exit /b %errorlevel%
