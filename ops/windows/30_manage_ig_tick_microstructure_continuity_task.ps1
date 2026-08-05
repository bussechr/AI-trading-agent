[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Install", "Preview", "Remove")][string]$Action = "Install",
    [ValidatePattern("^[A-Za-z0-9_.-]{1,100}$")][string]$TaskName = "TradingAgentIgTickCandidateContinuity",
    [string]$PythonExe,
    [string]$ApiKeyFile,
    [string]$Preregistration,
    [string]$StartReceipt,
    [string]$ReadinessRoot,
    [string]$CheckpointRoot,
    [ValidateRange(15, 1440)][int]$IntervalMinutes = 60,
    [ValidateRange(1, 15)][int]$HttpTimeoutSeconds = 3
)

# AGENT: ROLE: reversible current-user registrar for the candidate continuity checkpoint.
# AGENT: ISOLATION: pins collection-only source hashes and stores paths only; never starts trading.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$Wrapper = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "ops\windows\30_checkpoint_ig_tick_microstructure_candidate.ps1"))
$ReadinessTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\check_ig_tick_history_readiness.py"))
$ContinuityTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\check_ig_tick_microstructure_candidate_continuity.py"))
$SealerTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\seal_ig_tick_microstructure_candidate_preregistration.py"))
$CaptureTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\capture_ig_scalp_cost_model.py"))
$EnvPath = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "ops\windows\_env.bat"))
$PowerShell = [IO.Path]::GetFullPath((Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"))
$TaskPath = "\"
$Description = "FXSTACK_OWNER=ig_tick_candidate_continuity_v1; metadata-only; no runtime or trading authority."
$CurrentUser = [Security.Principal.WindowsIdentity]::GetCurrent().Name

function ExistingFile([string]$Path, [string]$Reason) {
    if ([string]::IsNullOrWhiteSpace($Path)) { throw $Reason }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw $Reason }
    return [IO.Path]::GetFullPath($item.FullName)
}
function Hash([string]$Path) { return (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() }
function Quote([string]$Value) {
    if ($Value.Contains('"') -or $Value.Contains("`r") -or $Value.Contains("`n") -or $Value.EndsWith('\')) { throw "task_argument_not_safely_quotable" }
    return '"' + $Value + '"'
}
function Owned([object]$Task) {
    $actions = @($Task.Actions)
    return (
        $actions.Count -eq 1 -and
        [string]$Task.TaskPath -eq $TaskPath -and
        [string]$Task.Description -eq $Description -and
        [IO.Path]::GetFullPath([string]$actions[0].Execute) -eq $PowerShell -and
        [string]$actions[0].Arguments -like ("*-File " + (Quote $Wrapper) + "*") -and
        [string]$Task.Principal.LogonType -in @("Interactive", "InteractiveToken") -and
        [string]$Task.Principal.RunLevel -eq "Limited"
    )
}

if ($Action -eq "Remove") {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) { Write-Output "[tick-continuity] task already absent: $TaskName"; exit 0 }
    if (-not (Owned $existing)) { throw "scheduled_task_identity_mismatch_refusing_remove" }
    if ($PSCmdlet.ShouldProcess($TaskName, "Remove candidate continuity task")) {
        Unregister-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -Confirm:$false
        Write-Output "[tick-continuity] task removed; collection and trading processes were untouched"
    }
    exit 0
}

$Python = ExistingFile $PythonExe "python_executable_invalid"
$Key = ExistingFile $ApiKeyFile "api_key_file_invalid"
$Prereg = ExistingFile $Preregistration "preregistration_invalid"
$Receipt = ExistingFile $StartReceipt "start_receipt_invalid"
foreach ($path in @($Wrapper, $ReadinessTool, $ContinuityTool, $SealerTool, $CaptureTool, $EnvPath, $PowerShell)) { [void](ExistingFile $path "task_source_invalid") }
$argumentParts = @(
    "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", (Quote $Wrapper),
    "-PythonExe", (Quote $Python), "-ApiKeyFile", (Quote $Key),
    "-Preregistration", (Quote $Prereg), "-StartReceipt", (Quote $Receipt),
    "-ReadinessRoot", (Quote ([IO.Path]::GetFullPath($ReadinessRoot))),
    "-CheckpointRoot", (Quote ([IO.Path]::GetFullPath($CheckpointRoot))),
    "-HttpTimeoutSeconds", ([string]$HttpTimeoutSeconds),
    "-ExpectedWrapperSha256", (Hash $Wrapper),
    "-ExpectedReadinessToolSha256", (Hash $ReadinessTool),
    "-ExpectedContinuityToolSha256", (Hash $ContinuityTool),
    "-ExpectedSealerSha256", (Hash $SealerTool),
    "-ExpectedCaptureToolSha256", (Hash $CaptureTool),
    "-ExpectedEnvSha256", (Hash $EnvPath)
)
$arguments = $argumentParts -join " "
$taskAction = New-ScheduledTaskAction -Execute $PowerShell -Argument $arguments
$preregistrationPayload = Get-Content -LiteralPath $Prereg -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop
$endUtc = ([DateTimeOffset]$preregistrationPayload.prospective_window.end_utc_exclusive).ToUniversalTime()
$triggerStart = (Get-Date).AddMinutes(1)
$repetitionDuration = $endUtc.LocalDateTime - $triggerStart
if ($repetitionDuration.TotalMinutes -le $IntervalMinutes) { throw "prospective_window_too_close_or_ended" }
$repeat = New-ScheduledTaskTrigger -Once -At $triggerStart -RepetitionInterval (New-TimeSpan -Minutes $IntervalMinutes) -RepetitionDuration $repetitionDuration
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::FromMinutes(20))
$principal = New-ScheduledTaskPrincipal -UserId $CurrentUser -LogonType Interactive -RunLevel Limited
$definition = New-ScheduledTask -Action $taskAction -Trigger $repeat -Settings $settings -Principal $principal -Description $Description

if ($Action -eq "Preview") {
    Write-Output (@{
        schema_version = "fxstack.ig_tick_candidate_continuity_task_preview.v1"; task_name = $TaskName;
        arguments = $arguments; interval_minutes = $IntervalMinutes; trigger_end_utc_exclusive = $endUtc.ToString("o");
        multiple_instances = "IgnoreNew"; start_when_available = $true; collection_metadata_only = $true;
        api_key_value_stored = $false; runtime_authorized = $false; trading_authorized = $false; mutation_performed = $false
    } | ConvertTo-Json -Compress -Depth 4)
    exit 0
}
$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not (Owned $existing)) { throw "scheduled_task_identity_mismatch_refusing_overwrite" }
if ($PSCmdlet.ShouldProcess($TaskName, "Install candidate continuity task")) {
    Register-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -InputObject $definition -Force | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    if (-not (Owned $registered) -or [string]$registered.Actions[0].Arguments -ne $arguments) { throw "registered_task_identity_verification_failed" }
    Write-Output "[tick-continuity] task installed or updated: $TaskName; hourly metadata only; no process was started"
}
