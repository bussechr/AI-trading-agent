@echo off
REM QUARANTINED: A candidate runtime must never share the production host trust domain.
echo [candidate-quarantined] ERROR: same-host candidate startup is disabled.
echo [candidate-quarantined] Run validation on a separate host or VM with no production database, bridge, broker, credential, registry-write, or writable production mounts.
echo [candidate-quarantined] Import only the externally signed, content-addressed evidence bundle through the documented release workflow.
exit /b 2
