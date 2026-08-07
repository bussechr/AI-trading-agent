@echo off
setlocal enabledelayedexpansion
call "%~dp0_env.bat" || exit /b 1
cd /d "%ROOT%"

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp024_deploy_bridge_ea.ps1" -BridgeApiKeyFile "%ROOT%\logs\bridge_api_key.txt" -BridgeCommandTokenFile "%ROOT%\logs\bridge_command_token.txt" -ConsumerIdentity "%FXSTACK_BRIDGE_CONSUMER_IDENTITY%" -TerminalLeaseScope "%FXSTACK_BRIDGE_TERMINAL_LEASE_SCOPE%" -CredentialGenerationId "%FXSTACK_BRIDGE_CREDENTIAL_GENERATION_ID%" %*
exit /b %errorlevel%
