@echo off
setlocal
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [stop] stopping known windows...
taskkill /f /fi "WINDOWTITLE eq MT4 Bridge Server :58710*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq MT4 Bridge Server :58711*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq MT4 Trading Agent :58710*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq MT4 Trading Agent :58711*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq FX Dashboard :3000*" >nul 2>&1
taskkill /f /fi "WINDOWTITLE eq Trade Confidence Monitor :58710*" >nul 2>&1

for %%P in (58710 58711 3000) do (
  for /f "tokens=5" %%K in ('netstat -ano ^| findstr ":%%P .*LISTENING" 2^>nul') do (
    taskkill /f /pid %%K >nul 2>&1
  )
)

echo [stop] done
exit /b 0
