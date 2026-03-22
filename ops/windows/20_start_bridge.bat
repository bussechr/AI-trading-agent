@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "PORT=%~2"
if not defined PORT set "PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   20_start_bridge.bat --run [PORT]
echo   20_start_bridge.bat --background [PORT]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "BRIDGE_LOG=%LOGDIR%\bridge_%PORT%.log"
set "BRIDGE_ERR_LOG=%LOGDIR%\bridge_%PORT%.err.log"
set "BRIDGE_PID=%LOGDIR%\bridge_%PORT%.pid"
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_BRIDGE_PORT=%PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
powershell -NoProfile -Command "$p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-m','src.trader.cli','bridge','serve','--host','127.0.0.1','--port','%PORT%' -RedirectStandardOutput '%BRIDGE_LOG%' -RedirectStandardError '%BRIDGE_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%BRIDGE_PID%' -Value $p.Id" >nul
call :wait_health %PORT%
exit /b %errorlevel%

:wait_health
set "P=%~1"
for /l %%I in (1,1,30) do (
  set "READY=0"
  for /f %%S in ('powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/ready' -Headers $hdr -TimeoutSec 2; if(($j.bridge_up -eq $true) -and ($j.database_ok -eq $true)){'1'} else {'0'}} catch {'0'}"') do set "READY=%%S"
  if "!READY!"=="1" (
    echo [bridge] ready on :%P%
    exit /b 0
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [bridge] ERROR: readiness timeout on :%P%
if defined BRIDGE_LOG if exist "%BRIDGE_LOG%" (
  echo [bridge] log: %BRIDGE_LOG%
  echo [bridge] --- recent log tail ---
  powershell -NoProfile -Command "Get-Content -Path '%BRIDGE_LOG%' -Tail 40"
)
if defined BRIDGE_ERR_LOG if exist "%BRIDGE_ERR_LOG%" (
  echo [bridge] err log: %BRIDGE_ERR_LOG%
  echo [bridge] --- recent error tail ---
  powershell -NoProfile -Command "Get-Content -Path '%BRIDGE_ERR_LOG%' -Tail 40"
)
exit /b 2

:run
set "TRADER_BRIDGE_IMPL=fxstack"
set "TRADER_BRIDGE_PORT=%PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
echo [bridge] starting on :%PORT%
"%TRADER_PYTHON_EXE%" -m src.trader.cli bridge serve --host 127.0.0.1 --port %PORT%
exit /b %errorlevel%
