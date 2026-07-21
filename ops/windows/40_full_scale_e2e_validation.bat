@echo off
REM QUARANTINED: Training and candidate validation cannot execute beside production.
echo [full-validation-quarantined] ERROR: same-host full-scale validation is disabled.
echo [full-validation-quarantined] Use a separate build/research host or VM with immutable copied inputs and no production database, API key, bridge, MT4, broker, registry-write, or writable production mounts.
echo [full-validation-quarantined] Transfer only the signed, content-addressed evidence bundle into the production quarantine root for operator review.
exit /b 2
