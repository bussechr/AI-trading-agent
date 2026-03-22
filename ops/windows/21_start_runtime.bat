@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
set "EQUITY=%~2"
if not defined EQUITY set "EQUITY=10000"
set "BRIDGE_PORT=%~3"
if not defined BRIDGE_PORT set "BRIDGE_PORT=%TRADER_BRIDGE_PORT%"

if /I "%MODE%"=="--background" goto bg
if /I "%MODE%"=="--run" goto run

echo Usage:
echo   21_start_runtime.bat --run [EQUITY] [BRIDGE_PORT]
echo   21_start_runtime.bat --background [EQUITY] [BRIDGE_PORT]
exit /b 2

:bg
set "LOGDIR=%ROOT%\logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%" >nul 2>&1
set "RUNTIME_LOG=%LOGDIR%\runtime_%BRIDGE_PORT%.log"
set "RUNTIME_ERR_LOG=%LOGDIR%\runtime_%BRIDGE_PORT%.err.log"
set "RUNTIME_PID=%LOGDIR%\runtime_%BRIDGE_PORT%.pid"
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=http://127.0.0.1:%BRIDGE_PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=full_live"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
powershell -NoProfile -Command "$p=Start-Process -FilePath '%TRADER_PYTHON_EXE%' -WorkingDirectory '%ROOT%' -ArgumentList '-m','src.trader.cli','runtime','run','--equity','%EQUITY%','--sleep','10' -RedirectStandardOutput '%RUNTIME_LOG%' -RedirectStandardError '%RUNTIME_ERR_LOG%' -WindowStyle Hidden -PassThru; Set-Content -Path '%RUNTIME_PID%' -Value $p.Id" >nul
call :wait_runtime %BRIDGE_PORT%
exit /b %errorlevel%

:wait_runtime
set "P=%~1"
for /l %%I in (1,1,40) do (
  set "RUNNING="
  for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$hdr=$null; if($env:FXSTACK_BRIDGE_API_KEY -and $env:FXSTACK_BRIDGE_API_KEY.Trim().Length -gt 0){$hdr=@{'X-API-Key'=$env:FXSTACK_BRIDGE_API_KEY.Trim()}}; try {$j=Invoke-RestMethod -Uri 'http://127.0.0.1:%P%/v2/ready' -Headers $hdr -TimeoutSec 2; if($j.runtime_ready -eq $true){'1'} else {'0'}} catch {'0'}"`) do set "RUNNING=%%S"
  if "!RUNNING!"=="1" (
    echo [runtime] runtime_status=running with fresh cycle timestamp detected via bridge :%P%
    exit /b 0
  )
  powershell -NoProfile -Command "Start-Sleep -Seconds 1" >nul
)

echo [runtime] ERROR: runtime startup timeout via bridge :%P%
if defined RUNTIME_LOG if exist "%RUNTIME_LOG%" (
  echo [runtime] log: %RUNTIME_LOG%
  echo [runtime] --- recent log tail ---
  powershell -NoProfile -Command "Get-Content -Path '%RUNTIME_LOG%' -Tail 40"
)
if defined RUNTIME_ERR_LOG if exist "%RUNTIME_ERR_LOG%" (
  echo [runtime] err log: %RUNTIME_ERR_LOG%
  echo [runtime] --- recent error tail ---
  powershell -NoProfile -Command "Get-Content -Path '%RUNTIME_ERR_LOG%' -Tail 40"
)
exit /b 2

:run
set "TRADER_RUNTIME_IMPL=fxstack"
set "MT4_BRIDGE_URL=http://127.0.0.1:%BRIDGE_PORT%"
set "MT4_BRIDGE_PROTOCOL=v2"
set "FX_AGENT_EXECUTION_MODE=full_live"
set "FXSTACK_RUNTIME_EQUITY_SEED=%EQUITY%"
echo [runtime] starting equity_seed=%EQUITY% (fallback only; MT4 heartbeat equity is authoritative) bridge=http://127.0.0.1:%BRIDGE_PORT%
"%TRADER_PYTHON_EXE%" -m src.trader.cli runtime run --equity %EQUITY% --sleep 10
exit /b %errorlevel%
