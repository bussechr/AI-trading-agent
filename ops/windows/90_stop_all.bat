@echo off
setlocal
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [stop] stopping known windows...

for %%F in (
  "%ROOT%\logs\bridge_58710.pid"
  "%ROOT%\logs\bridge_58711.pid"
  "%ROOT%\logs\runtime_58710.pid"
  "%ROOT%\logs\runtime_58711.pid"
  "%ROOT%\logs\dashboard_3000.pid"
  "%ROOT%\logs\monitor_58710.pid"
  "%ROOT%\logs\monitor_58711.pid"
) do (
  if exist "%%~fF" (
    for /f "usebackq delims=" %%P in ("%%~fF") do (
      taskkill /f /pid %%P >nul 2>&1
    )
    del /q "%%~fF" >nul 2>&1
  )
)

for %%P in (58710 58711 3000) do (
  for /f "usebackq delims=" %%K in (`powershell -NoProfile -Command "Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue | Where-Object { $_.LocalPort -eq %%P } | ForEach-Object { $_.OwningProcess }"`) do (
    call :kill_repo_owned_pid %%K
  )
)

rem Kill repo-scoped workers even if PID files and port ownership are stale.
powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process | Where-Object {" ^
  "  $cmd=[string]($_.CommandLine);" ^
  "  ($cmd -like '*-m src.trader.cli bridge serve*') -or" ^
  "  ($cmd -like '*-m src.trader.cli runtime run*') -or" ^
  "  ($cmd -like '*-m src.trader.cli monitor confidence*') -or" ^
  "  ($cmd -like '*next start -p 3000*')" ^
  "} | ForEach-Object { try { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue } catch {} }" >nul 2>&1
if /I "%FXSTACK_STOP_KILL_ALL_PYTHON%"=="1" (
  echo [stop] WARN: FXSTACK_STOP_KILL_ALL_PYTHON=1, applying global python.exe kill
  taskkill /f /im python.exe >nul 2>&1
)

echo [stop] done
exit /b 0

:kill_repo_owned_pid
setlocal
set "TARGET_PID=%~1"
if not defined TARGET_PID exit /b 0
powershell -NoProfile -Command ^
  "$root=[System.IO.Path]::GetFullPath('%ROOT%');" ^
  "$targetPid=%TARGET_PID%;" ^
  "$proc=Get-CimInstance Win32_Process -Filter ('ProcessId=' + $targetPid) -ErrorAction SilentlyContinue;" ^
  "if(-not $proc){exit 0}" ^
  "$cmd=[string]($proc.CommandLine);" ^
  "$exe=[string]($proc.ExecutablePath);" ^
  "$name=[string]($proc.Name);" ^
  "$owned=($cmd -like ('*' + $root + '*')) -or ($exe -like ('*' + $root + '*'));" ^
  "if(-not $owned -and $name -match '^(python|python3|node|cmd)\\.exe$'){ if($cmd -like '*src.trader.cli*' -or $cmd -like '*next start -p*' -or $cmd -like '*monitor confidence*'){ $owned=$true } }" ^
  "if($owned){ Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue }"
endlocal
exit /b 0
