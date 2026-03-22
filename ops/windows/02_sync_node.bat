@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

where pnpm >nul 2>&1
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm not found.
  exit /b 2
)

echo [sync-node] installing lockfile dependencies...
call pnpm install --frozen-lockfile
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm install failed.
  exit /b 2
)

echo [sync-node] validating dashboard package graph...
call pnpm run doctor
if errorlevel 1 (
  echo [sync-node] ERROR: dashboard doctor failed.
  exit /b 2
)

echo [sync-node] building dashboard bundle...
call pnpm build
if errorlevel 1 (
  echo [sync-node] ERROR: pnpm build failed.
  exit /b 2
)

echo [sync-node] OK
exit /b 0
