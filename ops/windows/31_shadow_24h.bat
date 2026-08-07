@echo off
REM QUARANTINED: Runtime release evidence must be produced outside the production host.
echo [shadow-24h-quarantined] ERROR: same-host 24-hour candidate validation is disabled.
echo [shadow-24h-quarantined] Run the exact candidate for at least 86400 seconds on the external isolated validation host or VM.
echo [shadow-24h-quarantined] Do not use 90_stop_all.bat as an external candidate rollback command; rollback must be scoped inside that isolated trust domain.
exit /b 2
