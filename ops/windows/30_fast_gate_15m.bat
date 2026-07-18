@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

if not defined BASELINE_URL set "BASELINE_URL=%MT4_BRIDGE_URL%"
if not defined FXSTACK_CANDIDATE_BRIDGE_PORT set "FXSTACK_CANDIDATE_BRIDGE_PORT=58711"
if not defined CANDIDATE_URL set "CANDIDATE_URL=http://%TRADER_BRIDGE_HOST%:%FXSTACK_CANDIDATE_BRIDGE_PORT%"
if not defined OUT_DIR set "OUT_DIR=docs"
if not defined DURATION_SECS set "DURATION_SECS=900"
if not defined ROLLBACK_CMD set "ROLLBACK_CMD=ops\windows\90_stop_all.bat"
if not exist "%OUT_DIR%" mkdir "%OUT_DIR%" >nul 2>&1

echo [fast-gate] baseline=%BASELINE_URL% candidate=%CANDIDATE_URL%
"%TRADER_PYTHON_EXE%" -m src.trader.cli scenario shadow-run -- --baseline-url %BASELINE_URL% --candidate-url %CANDIDATE_URL% --duration-secs %DURATION_SECS% --poll-secs 2 --min-throughput-delta 1 --max-timeout-rate 0.05 --require-nonzero-entries --rollback-on-fail --rollback-cmd "%ROLLBACK_CMD%" --out-dir %OUT_DIR% --prefix canary_shadow_fast15m
set "RC=%ERRORLEVEL%"
echo [fast-gate] exit_code=%RC%
exit /b %RC%
