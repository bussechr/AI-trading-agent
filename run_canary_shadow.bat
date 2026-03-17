@echo off
setlocal

title Shadow Canary Runner
cd /d "%~dp0"

set BASELINE_URL=http://127.0.0.1:58710
if not "%1"=="" set BASELINE_URL=%1

set CANDIDATE_URL=http://127.0.0.1:58711
if not "%2"=="" set CANDIDATE_URL=%2

set DURATION_SECS=900
if not "%3"=="" set DURATION_SECS=%3

set OUT_DIR=docs
if not "%4"=="" set OUT_DIR=%4

set "ROLLBACK_CMD=start /b run_bridge.bat"
if not "%5"=="" set "ROLLBACK_CMD=%5"

echo ============================================================
echo  SHADOW CANARY RUNNER
echo ============================================================
echo  Baseline URL : %BASELINE_URL%
echo  Candidate URL: %CANDIDATE_URL%
echo  Duration secs: %DURATION_SECS%
echo  Output dir   : %OUT_DIR%
echo  Rollback cmd : %ROLLBACK_CMD%
echo ============================================================
echo.

if exist "C:\Python311\python.exe" (
    "C:\Python311\python.exe" -m src.trader.cli scenario shadow-run -- --baseline-url %BASELINE_URL% --candidate-url %CANDIDATE_URL% --duration-secs %DURATION_SECS% --poll-secs 2 --min-throughput-delta 1 --max-timeout-rate 0.05 --require-nonzero-entries --rollback-on-fail --rollback-cmd "%ROLLBACK_CMD%" --rollback-timeout-secs 60 --out-dir %OUT_DIR% --prefix canary_shadow
) else if exist "C:\Python312\python.exe" (
    "C:\Python312\python.exe" -m src.trader.cli scenario shadow-run -- --baseline-url %BASELINE_URL% --candidate-url %CANDIDATE_URL% --duration-secs %DURATION_SECS% --poll-secs 2 --min-throughput-delta 1 --max-timeout-rate 0.05 --require-nonzero-entries --rollback-on-fail --rollback-cmd "%ROLLBACK_CMD%" --rollback-timeout-secs 60 --out-dir %OUT_DIR% --prefix canary_shadow
) else (
    python -m src.trader.cli scenario shadow-run -- --baseline-url %BASELINE_URL% --candidate-url %CANDIDATE_URL% --duration-secs %DURATION_SECS% --poll-secs 2 --min-throughput-delta 1 --max-timeout-rate 0.05 --require-nonzero-entries --rollback-on-fail --rollback-cmd "%ROLLBACK_CMD%" --rollback-timeout-secs 60 --out-dir %OUT_DIR% --prefix canary_shadow
)

set EXIT_CODE=%ERRORLEVEL%
echo.
echo Shadow run exit code: %EXIT_CODE%
echo 0=pass, 2=gate_fail, 3=gate_fail_and_rollback_failed
pause
exit /b %EXIT_CODE%
