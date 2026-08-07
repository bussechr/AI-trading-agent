[CmdletBinding()]
param()

# AGENT: ROLE: hidden Scheduled Task trampoline for the canonical scalp batch launcher.
# AGENT HANDSHAKE: TradingAgentScalpRuntime -> exact 21_start_scalp_runtime.bat --run with exit-code propagation.
# AGENT: SIDE EFFECTS: delegates only to the canonical launcher; it owns no task, credential, MT4, or process cleanup.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$launcherPath = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "21_start_scalp_runtime.bat")
)
$commandProcessor = [IO.Path]::GetFullPath(
    (Join-Path $env:SystemRoot "System32\cmd.exe")
)
if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "scalp_runtime_launcher_missing"
}
if (-not (Test-Path -LiteralPath $commandProcessor -PathType Leaf)) {
    throw "windows_command_processor_missing"
}

$commandLine = '""{0}" --run"' -f $launcherPath
& $commandProcessor /d /c $commandLine
exit [int]$LASTEXITCODE
