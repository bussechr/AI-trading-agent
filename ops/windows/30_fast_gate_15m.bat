@echo off
REM QUARANTINED: Runtime release evidence must be produced outside the production host.
echo [fast-gate-quarantined] ERROR: same-host 15-minute candidate validation is disabled.
echo [fast-gate-quarantined] Run the exact candidate for at least 900 seconds on the external isolated validation host or VM.
echo [fast-gate-quarantined] Do not use 90_stop_all.bat as an external candidate rollback command; rollback must be scoped inside that isolated trust domain.
exit /b 2
