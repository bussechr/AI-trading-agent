[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$Preregistration,

    [Parameter(Mandatory = $true)]
    [string]$OutputDir,

    [Parameter(Mandatory = $true)]
    [string]$ApiKeyFile,

    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(10.0, 3600.0)]
    [double]$MaximumManifestAgeSeconds = 120.0,

    [ValidateRange(10.0, 3600.0)]
    [double]$StartupGraceSeconds = 180.0,

    [ValidateRange(10, 300)]
    [int]$StartConfirmationSeconds = 60,

    [string]$ExpectedWatchdogSha256 = "",
    [string]$ExpectedGuardSha256 = ""
)

# AGENT: ROLE: fail-closed restart/watchdog adapter for one resilient MTVCLC guard tuple.
# AGENT: HANDSHAKE: exact Health result with no writer -> same-argument StartOrResume.
# AGENT: ISOLATION: passes only an API-key file path and never reads credentials or capture rows.
# AGENT: SIDE EFFECTS: only the pinned guard may initialize/resume collection after the absent-writer proof.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StatusSchema = "fxstack.mtvclc_collector_restart_watchdog.resilient.v1"
$GuardStatusSchema = "fxstack.mtvclc_collector_guard_status.resilient.v1"
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$GuardPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\27_guard_mtvclc_collector_resilient.ps1")
)
$PowerShellHost = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"

function Write-StatusAndExit {
    param([hashtable]$Payload, [int]$Code)
    $Payload["schema_version"] = $StatusSchema
    $Payload["collection_only"] = $true
    $Payload["evaluation_performed"] = $false
    $Payload["signal_computation_authorized"] = $false
    $Payload["outcome_access_authorized"] = $false
    $Payload["performance_computation_authorized"] = $false
    $Payload["success_claim_authorized"] = $false
    $Payload["issuer_authorized"] = $false
    $Payload["signature_authorized"] = $false
    $Payload["authority_granted"] = $false
    $Payload["runtime_authorized"] = $false
    $Payload["activation_authorized"] = $false
    $Payload["broker_access_authorized"] = $false
    $Payload["order_authorized"] = $false
    Write-Output ($Payload | ConvertTo-Json -Compress -Depth 8)
    exit $Code
}

function Get-FileSha256 {
    param([string]$Path)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

try {
    $guardItem = Get-Item -LiteralPath $GuardPath -Force -ErrorAction Stop
    $hostItem = Get-Item -LiteralPath $PowerShellHost -Force -ErrorAction Stop
    $outputItem = Get-Item -LiteralPath $OutputDir -Force -ErrorAction Stop
    if (
        $guardItem.PSIsContainer -or
        $hostItem.PSIsContainer -or
        -not $outputItem.PSIsContainer -or
        ($guardItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        ($hostItem.Attributes -band [IO.FileAttributes]::ReparsePoint) -or
        ($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
    ) {
        throw "watchdog_executable_path_invalid"
    }
    $ResolvedOutput = [IO.Path]::GetFullPath($outputItem.FullName).TrimEnd('\', '/')
    foreach ($expectedHash in @($ExpectedWatchdogSha256, $ExpectedGuardSha256)) {
        if (
            -not [string]::IsNullOrWhiteSpace($expectedHash) -and
            $expectedHash -notmatch "^[0-9a-fA-F]{64}$"
        ) {
            throw "watchdog_expected_source_hash_invalid"
        }
    }
    $actualWatchdogHash = Get-FileSha256 $PSCommandPath
    $actualGuardHash = Get-FileSha256 $GuardPath
    if (
        -not [string]::IsNullOrWhiteSpace($ExpectedWatchdogSha256) -and
        $actualWatchdogHash -ne $ExpectedWatchdogSha256.ToLowerInvariant()
    ) {
        throw "watchdog_source_identity_mismatch"
    }
    if (
        -not [string]::IsNullOrWhiteSpace($ExpectedGuardSha256) -and
        $actualGuardHash -ne $ExpectedGuardSha256.ToLowerInvariant()
    ) {
        throw "guard_source_identity_mismatch"
    }
}
catch {
    Write-StatusAndExit @{
        status = "watchdog_configuration_refused"
        reason = $_.Exception.Message
    } 2
}

function New-GuardArguments {
    param([string]$GuardAction)
    return @(
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy", "Bypass",
        "-File", $GuardPath,
        "-Action", $GuardAction,
        "-PythonExe", $PythonExe,
        "-Preregistration", $Preregistration,
        "-OutputDir", $ResolvedOutput,
        "-ApiKeyFile", $ApiKeyFile,
        "-BaseUrl", $BaseUrl,
        "-MaximumManifestAgeSeconds", ([string]::Format(
            [Globalization.CultureInfo]::InvariantCulture,
            "{0:R}",
            $MaximumManifestAgeSeconds
        )),
        "-StartupGraceSeconds", ([string]::Format(
            [Globalization.CultureInfo]::InvariantCulture,
            "{0:R}",
            $StartupGraceSeconds
        ))
    )
}

function Quote-ProcessArgument {
    param([string]$Value)
    if (
        $Value.Contains('"') -or
        $Value.Contains("`r") -or
        $Value.Contains("`n") -or
        $Value.EndsWith('\')
    ) {
        throw "guard_process_argument_not_safely_quotable"
    }
    return '"' + $Value + '"'
}

function Invoke-GuardHealth {
    $healthArguments = @(New-GuardArguments "Health")
    $raw = @(& $PowerShellHost @healthArguments 2>&1)
    $exitCode = [int]$LASTEXITCODE
    $lines = @(
        $raw |
            ForEach-Object { ([string]$_).Trim() } |
            Where-Object { $_.Length -gt 0 }
    )
    if ($lines.Count -ne 1) {
        throw "guard_health_output_not_single_json_record"
    }
    try {
        $report = $lines[0] | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw "guard_health_output_invalid"
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Report = $report
    }
}

function Test-CollectionOnlyGuardStatus {
    param([object]$Report)
    return (
        [string]$Report.schema_version -eq $GuardStatusSchema -and
        $Report.collection_only -eq $true -and
        $Report.evaluation_performed -eq $false -and
        $Report.signal_computation_authorized -eq $false -and
        $Report.outcome_access_authorized -eq $false -and
        $Report.performance_computation_authorized -eq $false -and
        $Report.success_claim_authorized -eq $false -and
        $Report.issuer_authorized -eq $false -and
        $Report.signature_authorized -eq $false -and
        $Report.authority_granted -eq $false -and
        $Report.runtime_authorized -eq $false -and
        $Report.activation_authorized -eq $false -and
        $Report.broker_access_authorized -eq $false -and
        $Report.order_authorized -eq $false
    )
}

try {
    $health = Invoke-GuardHealth
}
catch {
    Write-StatusAndExit @{
        status = "health_refused"
        reason = $_.Exception.Message
    } 2
}

$report = $health.Report
if (-not (Test-CollectionOnlyGuardStatus $report)) {
    Write-StatusAndExit @{
        status = "health_refused"
        reason = "guard_health_authority_or_schema_mismatch"
        guard_exit_code = $health.ExitCode
    } 2
}

$healthyStatuses = @(
    "healthy",
    "starting",
    "waiting_for_sealed_t0",
    "prospective_window_complete"
)
if ($health.ExitCode -eq 0) {
    if ($healthyStatuses -notcontains [string]$report.status) {
        Write-StatusAndExit @{
            status = "health_refused"
            reason = "unexpected_success_health_status"
            guard_status = [string]$report.status
        } 2
    }
    Write-StatusAndExit @{
        status = "no_action_required"
        guard_status = [string]$report.status
        writer_group_count = [int]$report.writer_group_count
    } 0
}

$restartableStatuses = @("stopped_before_t0", "stopped_during_window")
$lockState = [string]$report.supervisor_lock_state
$restartAllowed = (
    $health.ExitCode -eq 3 -and
    $restartableStatuses -contains [string]$report.status -and
    [string]$report.reason -eq "collector_writer_absent" -and
    [int]$report.writer_group_count -eq 0 -and
    $report.supervisor_lock_held -eq $false -and
    @("absent", "available") -contains $lockState
)
if (-not $restartAllowed) {
    Write-StatusAndExit @{
        status = "restart_refused"
        reason = "health_did_not_prove_restartable_absent_writer"
        guard_exit_code = $health.ExitCode
        guard_status = [string]$report.status
        writer_group_count = [int]$report.writer_group_count
        supervisor_lock_state = $lockState
    } 4
}

$endEpoch = 0.0
try {
    $endEpoch = [double]$report.prospective_end_epoch_exclusive
}
catch {
    Write-StatusAndExit @{
        status = "restart_refused"
        reason = "prospective_end_invalid"
    } 2
}
if (
    [double]::IsNaN($endEpoch) -or
    [double]::IsInfinity($endEpoch) -or
    $endEpoch -le 0.0 -or
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() -ge $endEpoch
) {
    Write-StatusAndExit @{
        status = "restart_refused"
        reason = "prospective_window_not_open"
    } 4
}

Write-Output (@{
    schema_version = $StatusSchema
    status = "restart_allowed_after_absent_writer_health"
    guard_status = [string]$report.status
    collection_only = $true
    evaluation_performed = $false
    signal_computation_authorized = $false
    outcome_access_authorized = $false
    performance_computation_authorized = $false
    success_claim_authorized = $false
    issuer_authorized = $false
    signature_authorized = $false
    authority_granted = $false
    runtime_authorized = $false
    activation_authorized = $false
    broker_access_authorized = $false
    order_authorized = $false
} | ConvertTo-Json -Compress -Depth 6)

$startArguments = @(New-GuardArguments "StartOrResume")
$startArgumentLine = ($startArguments | ForEach-Object { Quote-ProcessArgument ([string]$_) }) -join " "
$supervisionDirectory = Join-Path $ResolvedOutput "supervision-resilient"
try {
    if (-not (Test-Path -LiteralPath $supervisionDirectory -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $supervisionDirectory -ErrorAction Stop
    }
    $supervisionItem = Get-Item -LiteralPath $supervisionDirectory -Force -ErrorAction Stop
    if (
        -not $supervisionItem.PSIsContainer -or
        ($supervisionItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
    ) {
        throw "watchdog_supervision_directory_invalid"
    }
    $launchId = "{0}_{1}" -f (
        [DateTimeOffset]::UtcNow.ToString("yyyyMMddTHHmmssfffZ")
    ), ([Guid]::NewGuid().ToString("N"))
    $guardStdout = Join-Path $supervisionDirectory ("guard.{0}.stdout.log" -f $launchId)
    $guardStderr = Join-Path $supervisionDirectory ("guard.{0}.stderr.log" -f $launchId)
    $detachedGuard = Start-Process `
        -FilePath $PowerShellHost `
        -ArgumentList $startArgumentLine `
        -WindowStyle Hidden `
        -RedirectStandardOutput $guardStdout `
        -RedirectStandardError $guardStderr `
        -PassThru `
        -ErrorAction Stop
}
catch {
    Write-StatusAndExit @{
        status = "restart_launch_refused"
        reason = $_.Exception.Message
    } 2
}

$confirmationDeadline = [DateTimeOffset]::UtcNow.AddSeconds($StartConfirmationSeconds)
$lastGuardStatus = "not_observed"
while ([DateTimeOffset]::UtcNow -lt $confirmationDeadline) {
    Start-Sleep -Seconds 1
    try {
        $confirmation = Invoke-GuardHealth
        if (-not (Test-CollectionOnlyGuardStatus $confirmation.Report)) {
            throw "guard_confirmation_authority_or_schema_mismatch"
        }
    }
    catch {
        Write-StatusAndExit @{
            status = "restart_confirmation_refused"
            reason = $_.Exception.Message
            launched_guard_pid = [int]$detachedGuard.Id
            guard_stdout_log = $guardStdout
            guard_stderr_log = $guardStderr
        } 4
    }
    $confirmationReport = $confirmation.Report
    $lastGuardStatus = [string]$confirmationReport.status
    if (
        $confirmation.ExitCode -eq 0 -and
        @("starting", "waiting_for_sealed_t0", "healthy") -contains $lastGuardStatus -and
        [int]$confirmationReport.writer_group_count -eq 1 -and
        $confirmationReport.supervisor_lock_held -eq $true
    ) {
        Write-StatusAndExit @{
            status = "restart_confirmed"
            guard_status = $lastGuardStatus
            launched_guard_pid = [int]$detachedGuard.Id
            writer_group_count = 1
            supervisor_lock_held = $true
            guard_stdout_log = $guardStdout
            guard_stderr_log = $guardStderr
        } 0
    }
    if ($detachedGuard.HasExited) {
        Write-StatusAndExit @{
            status = "restart_confirmation_refused"
            reason = "detached_guard_exited_before_healthy_confirmation"
            launched_guard_pid = [int]$detachedGuard.Id
            detached_guard_exit_code = [int]$detachedGuard.ExitCode
            guard_status = $lastGuardStatus
            guard_stdout_log = $guardStdout
            guard_stderr_log = $guardStderr
        } 4
    }
}

Write-StatusAndExit @{
    status = "restart_confirmation_refused"
    reason = "detached_guard_health_confirmation_timed_out"
    launched_guard_pid = [int]$detachedGuard.Id
    guard_status = $lastGuardStatus
    guard_stdout_log = $guardStdout
    guard_stderr_log = $guardStderr
} 4
