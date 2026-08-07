REM AGENT: ROLE: Revoke execution egress, then stop repo-owned bridge/runtime/dashboard/feature-push/monitor Windows processes and clear the runtime snapshot.
REM AGENT: ENTRYPOINT: `ops/windows/90_stop_all.bat`.
REM AGENT: PRIMARY INPUTS: PID files, repo-scoped process inspection, env from `_env.bat`.
REM AGENT: PRIMARY OUTPUTS: release authority revoked, execution queue quarantined, stopped repo-owned processes, and cleared runtime snapshot state.
REM AGENT: DEPENDS ON: `ops/windows/_env.bat`, installed `fxstack.runtime.execution_egress_control`, repo log PID files, runtime service import for snapshot clear.
REM AGENT: CALLED BY: operators and recovery workflows.
REM AGENT: STATE / SIDE EFFECTS: first disables execution egress and revokes release authority, then kills repo-owned Windows processes and patches runtime state to `stopped`; MT4 is never stopped.
REM AGENT: HANDSHAKES: durable egress revocation/quarantine precedes repo-scoped Windows stop semantics and runtime state patch reset.
REM AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `fx-quant-stack/src/fxstack/runtime/service.py` -> `docs/agents/runtime-loop.md`
@echo off
setlocal
set "FXSTACK_INSTANCE_ID=baseline"
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

echo [stop] disabling execution egress, revoking release authority, and quarantining queued commands...
"%TRADER_PYTHON_EXE%" -I -m fxstack.runtime.execution_egress_control --reason operator_stop_all
if errorlevel 1 (
  echo [stop] ERROR: durable egress revocation/quarantine was not confirmed; no process was stopped.
  echo [stop] Resolve the database/control-path failure, then rerun this command. MT4 terminal and EA remain running.
  exit /b 2
)

echo [stop] egress revoked; stopping repo-owned stack processes. MT4 terminal and EA remain running.

set "STOP_WAIT_SECS=%FXSTACK_PROCESS_EXIT_WAIT_SECS%"
if not defined STOP_WAIT_SECS set "STOP_WAIT_SECS=10"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop_owned_stack_processes.ps1" -Root "%ROOT%" -PidDirectory "%ROOT%\logs" -PortsCsv "%TRADER_BRIDGE_PORT%,%TRADER_DASHBOARD_PORT%" -WaitSeconds %STOP_WAIT_SECS%
if errorlevel 1 (
  echo [stop] ERROR: repo-owned process-tree/listener shutdown was not confirmed.
  echo [stop] PID markers were retained for diagnosis; MT4 terminal and EA remain running.
  exit /b 2
)

for /f "delims=" %%F in ('dir /b /a:-d "%ROOT%\logs\*.pid" 2^>nul') do del /q "%ROOT%\logs\%%~F" >nul 2>&1
call :clear_runtime_snapshot >nul 2>&1
if exist "%ROOT%\logs\active_stack_env.bat" del /q "%ROOT%\logs\active_stack_env.bat" >nul 2>&1
if exist "%ROOT%\logs\active_candidate_env.bat" del /q "%ROOT%\logs\active_candidate_env.bat" >nul 2>&1

echo [stop] done; repo-owned stack processes stopped. MT4 terminal and EA were not stopped.
exit /b 0

:clear_runtime_snapshot
setlocal
if not defined TRADER_PYTHON_EXE exit /b 0
"%TRADER_PYTHON_EXE%" -c "from fxstack.runtime.service import RuntimeService; from fxstack.settings import get_settings; s=get_settings(); svc=RuntimeService(database_url=s.database_url, default_session_id=s.default_session_id, command_ttl_secs=s.command_ttl_secs, requeue_age_secs=s.startup_requeue_age_secs, db_connect_retries=1); svc.patch_state({'runtime_status':'stopped','runtime_last_cycle_ts':0.0,'runtime_diag':{},'monitor':{},'agent_decisions':[],'agent_diagnostics':{},'system_status':'disconnected','last_heartbeat':None,'positions':[],'symbol_readiness':{},'symbol_ready_count':0,'unsupported_pairs':[],'equity':0.0,'margin':0.0,'freemargin':0.0,'__prune_stale__':True})" >nul 2>&1
endlocal
exit /b 0
