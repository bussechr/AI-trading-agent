@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "MODE=%~1"
if not defined MODE set "MODE=watch"

set "REGISTRY_ROOT=%~2"
if not defined REGISTRY_ROOT (
  for /f "usebackq delims=" %%R in (`powershell -NoProfile -Command "$dirs = Get-ChildItem -Path 'fx-quant-stack/artifacts_shadow' -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -like 'registry_*' } | Sort-Object LastWriteTimeUtc -Descending; if($dirs){ $dirs[0].FullName }"`) do set "REGISTRY_ROOT=%%R"
)

if not defined REGISTRY_ROOT (
  set "REGISTRY_ROOT=%ROOT%\fx-quant-stack\artifacts_shadow\registry_full_20260323"
)

set "PAIR_LIST=%FXSTACK_PAIRS%"
if not defined PAIR_LIST set "PAIR_LIST=EURUSD,USDJPY,GBPUSD,AUDUSD,USDCAD,USDCHF,EURGBP,EURJPY,NZDUSD"

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp025_monitor_everything.ps1" -RegistryRoot "%REGISTRY_ROOT%" -PairList "%PAIR_LIST%" -Mode "%MODE%" -BridgeUrl "%MT4_BRIDGE_URL%" -DashboardUrl "%TRADER_DASHBOARD_URL%"
exit /b %errorlevel%
