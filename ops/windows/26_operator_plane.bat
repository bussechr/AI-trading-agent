REM AGENT: ROLE: Describe or run the optional read-only stdio MCP operator-plane services.
REM AGENT: ENTRYPOINT: `26_operator_plane.bat describe|runtime-mcp|twin-mcp|release-mcp`.
REM AGENT: STATE / SIDE EFFECTS: describe is read-only; MCP modes stay attached to stdio and expose GET/file reads only.
@echo off
setlocal
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

set "ACTION=%~1"
if not defined ACTION set "ACTION=describe"

if /I "%ACTION%"=="describe" goto describe
if /I "%ACTION%"=="runtime-mcp" goto runtime_mcp
if /I "%ACTION%"=="twin-mcp" goto twin_mcp
if /I "%ACTION%"=="release-mcp" goto release_mcp
goto usage

:describe
echo [operator-plane] runtime-state MCP
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_runtime_state.server --describe
if errorlevel 1 exit /b %errorlevel%
echo [operator-plane] twin-artefacts MCP
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_twin_artefacts.server --describe
if errorlevel 1 exit /b %errorlevel%
echo [operator-plane] release-registry MCP
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_release_registry.server --describe
if errorlevel 1 exit /b %errorlevel%
echo [operator-plane] OpenClaw supervisor ^(disabled mode is inert^)
"%TRADER_PYTHON_EXE%" -m services.operator_plane.openclaw.service --describe
exit /b %errorlevel%

:runtime_mcp
call :require_read_only_mcp
if errorlevel 1 exit /b %errorlevel%
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_runtime_state.server
exit /b %errorlevel%

:twin_mcp
call :require_read_only_mcp
if errorlevel 1 exit /b %errorlevel%
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_twin_artefacts.server
exit /b %errorlevel%

:release_mcp
call :require_read_only_mcp
if errorlevel 1 exit /b %errorlevel%
"%TRADER_PYTHON_EXE%" -m services.operator_plane.mcp_release_registry.server
exit /b %errorlevel%

:require_read_only_mcp
if /I not "%FXSTACK_MCP_ENABLED%"=="1" if /I not "%FXSTACK_MCP_ENABLED%"=="true" (
  echo [operator-plane] ERROR: set FXSTACK_MCP_ENABLED=1 explicitly before starting an MCP server.
  exit /b 2
)
if /I not "%FXSTACK_MCP_TRANSPORT%"=="stdio" (
  echo [operator-plane] ERROR: only FXSTACK_MCP_TRANSPORT=stdio is supported.
  exit /b 2
)
exit /b 0

:usage
echo Usage:
echo   26_operator_plane.bat describe
echo   26_operator_plane.bat runtime-mcp
echo   26_operator_plane.bat twin-mcp
echo   26_operator_plane.bat release-mcp
exit /b 2
