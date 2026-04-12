@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if /I "%FXSTACK_PACKAGE_MODE%"=="1" (
  if not defined NODE_EXE (
    echo [sync-node] ERROR: bundled node runtime not configured.
    exit /b 2
  )
  if not exist "%NODE_EXE%" (
    echo [sync-node] ERROR: bundled node runtime missing: %NODE_EXE%
    exit /b 2
  )
  if exist "%ROOT%\.next\standalone\server.js" (
    echo [sync-node] package mode; packaged standalone dashboard runtime ready.
    exit /b 0
  )
  if not exist "%ROOT%\.next\BUILD_ID" (
    echo [sync-node] ERROR: packaged dashboard build missing.
    exit /b 2
  )
  if not exist "%ROOT%\node_modules\next\dist\bin\next" (
    echo [sync-node] ERROR: packaged Next.js runtime missing.
    exit /b 2
  )
  echo [sync-node] package mode; packaged dashboard runtime ready.
  exit /b 0
)

where pnpm >nul 2>&1
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm not found.
  exit /b 2
)

echo [sync-node] installing lockfile dependencies...
call cmd /c "pnpm install --frozen-lockfile"
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm install failed.
  exit /b 2
)

echo [sync-node] validating dashboard package graph...
call cmd /c "pnpm run doctor"
if errorlevel 1 (
  echo [sync-node] ERROR: dashboard doctor failed.
  exit /b 2
)

call :dashboard_build_required
if errorlevel 1 (
  exit /b 2
)
if /I "%DASHBOARD_BUILD_REQUIRED%"=="0" (
  echo [sync-node] existing dashboard build is current; skipping rebuild.
  echo [sync-node] OK
  exit /b 0
)

echo [sync-node] building dashboard bundle...
call cmd /c "pnpm build"
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm build failed.
  exit /b 2
)

echo [sync-node] OK
exit /b 0

:dashboard_build_required
set "DASHBOARD_BUILD_REQUIRED=1"
if not exist "%ROOT%\.next\BUILD_ID" exit /b 0

set "BUILD_CHECK_RESULT="
for /f "usebackq delims=" %%S in (`powershell -NoProfile -Command "$ErrorActionPreference='Stop'; $root='%ROOT%'; $buildId=Join-Path $root '.next\BUILD_ID'; if(-not (Test-Path $buildId)){ 'BUILD'; exit 0 }; $buildTime=(Get-Item $buildId).LastWriteTimeUtc; $paths=@('package.json','pnpm-lock.yaml','next.config.js','next.config.mjs','postcss.config.js','postcss.config.mjs','tailwind.config.js','tailwind.config.ts','app','components','lib','scripts'); foreach($relative in $paths){ $target=Join-Path $root $relative; if(-not (Test-Path $target)){ continue }; if((Get-Item $target).PSIsContainer){ $newer=Get-ChildItem -Path $target -Recurse -File -ErrorAction SilentlyContinue | Where-Object { $_.LastWriteTimeUtc -gt $buildTime } | Select-Object -First 1; if($null -ne $newer){ 'BUILD'; exit 0 } } elseif((Get-Item $target).LastWriteTimeUtc -gt $buildTime){ 'BUILD'; exit 0 } }; 'SKIP'"`) do set "BUILD_CHECK_RESULT=%%S"

if /I "%BUILD_CHECK_RESULT%"=="BUILD" (
  set "DASHBOARD_BUILD_REQUIRED=1"
  exit /b 0
)
if /I "%BUILD_CHECK_RESULT%"=="SKIP" (
  set "DASHBOARD_BUILD_REQUIRED=0"
  exit /b 0
)

echo [sync-node] ERROR: unable to determine dashboard build freshness.
exit /b 2
