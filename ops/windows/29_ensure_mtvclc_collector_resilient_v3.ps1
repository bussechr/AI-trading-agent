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

    [Parameter(Mandatory = $true)]
    [string]$BridgeEaRepositorySource,

    [Parameter(Mandatory = $true)]
    [string]$BridgeEaDeployedSource,

    [Parameter(Mandatory = $true)]
    [string]$BridgeEaDeployedEx4,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedCollectorSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedCollectorTemplateSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedInspectorSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedContinuityCoreSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationArtifactSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationBodySha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedWatchdogSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedGuardSha256,

    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(10.0, 3600.0)]
    [double]$MaximumManifestAgeSeconds = 120.0,

    [ValidateRange(10.0, 3600.0)]
    [double]$StartupGraceSeconds = 180.0
)

# AGENT: ROLE: fail-closed full-lifetime task adapter for one runtime-pinned v5 collector tuple.
# AGENT: HANDSHAKE: exact collection-only Health result with no writer -> synchronous identical producer-pinned StartOrResume tuple.
# AGENT: ISOLATION: passes only an API-key file path and has no evaluation, runtime, or trade path.
# AGENT: SIDE EFFECTS: only the hash-pinned gap-v5 guard may resume collection; this process remains its parent for the guard's full lifetime.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StatusSchema = "fxstack.mtvclc_collector_restart_watchdog.gap_v5.v1"
$GuardStatusSchema = "fxstack.mtvclc_collector_guard_status.gap_v5.v1"
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$GuardPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\27_guard_mtvclc_collector_resilient_v3.ps1")
)
$CollectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\capture_ig_mt4_m1_activity_resilient_v4.py")
)
$CollectorTemplatePath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\capture_ig_mt4_m1_activity_resilient_v3.py")
)
$InspectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\check_mt4_tick_volume_collector_continuity_resilient_v3.py")
)
$ContinuityCorePath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\check_mt4_tick_volume_collector_continuity_resilient.py")
)
$PowerShellHost = Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
$SupervisionFolder = "supervision-gap-v5"

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
    $Payload["immediate_market_trade_authorized"] = $false
    Write-Output ($Payload | ConvertTo-Json -Compress -Depth 8)
    exit $Code
}

function Resolve-ExistingFile {
    param([string]$Path, [string]$Reason)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw $Reason
    }
    return [IO.Path]::GetFullPath($item.FullName)
}

function Get-FileSha256 {
    param([string]$Path)
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    try {
        return [string]::Equals(
            [IO.Path]::GetFullPath($Left).TrimEnd('\', '/'),
            [IO.Path]::GetFullPath($Right).TrimEnd('\', '/'),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

try {
    $ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
    $ResolvedPreregistration = Resolve-ExistingFile $Preregistration "preregistration_file_invalid"
    $ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
    $ResolvedBridgeEaRepositorySource = Resolve-ExistingFile $BridgeEaRepositorySource "bridge_ea_repository_source_invalid"
    $ResolvedBridgeEaDeployedSource = Resolve-ExistingFile $BridgeEaDeployedSource "bridge_ea_deployed_source_invalid"
    $ResolvedBridgeEaDeployedEx4 = Resolve-ExistingFile $BridgeEaDeployedEx4 "bridge_ea_deployed_ex4_invalid"
    $null = Resolve-ExistingFile $GuardPath "collector_guard_missing"
    $null = Resolve-ExistingFile $CollectorPath "collector_source_missing"
    $null = Resolve-ExistingFile $CollectorTemplatePath "collector_template_source_missing"
    $null = Resolve-ExistingFile $InspectorPath "continuity_inspector_missing"
    $null = Resolve-ExistingFile $ContinuityCorePath "continuity_core_missing"
    $null = Resolve-ExistingFile $PowerShellHost "windows_powershell_missing"
    $outputItem = Get-Item -LiteralPath $OutputDir -Force -ErrorAction Stop
    if (
        -not $outputItem.PSIsContainer -or
        ($outputItem.Attributes -band [IO.FileAttributes]::ReparsePoint)
    ) {
        throw "output_root_invalid"
    }
    $ResolvedOutput = [IO.Path]::GetFullPath($outputItem.FullName).TrimEnd('\', '/')

    $ExpectedCollectorSha256 = $ExpectedCollectorSha256.ToLowerInvariant()
    $ExpectedCollectorTemplateSha256 = $ExpectedCollectorTemplateSha256.ToLowerInvariant()
    $ExpectedInspectorSha256 = $ExpectedInspectorSha256.ToLowerInvariant()
    $ExpectedContinuityCoreSha256 = $ExpectedContinuityCoreSha256.ToLowerInvariant()
    $ExpectedPreregistrationArtifactSha256 = (
        $ExpectedPreregistrationArtifactSha256.ToLowerInvariant()
    )
    $ExpectedPreregistrationBodySha256 = (
        $ExpectedPreregistrationBodySha256.ToLowerInvariant()
    )
    $ExpectedWatchdogSha256 = $ExpectedWatchdogSha256.ToLowerInvariant()
    $ExpectedGuardSha256 = $ExpectedGuardSha256.ToLowerInvariant()

    if ((Get-FileSha256 $PSCommandPath) -ne $ExpectedWatchdogSha256) {
        throw "watchdog_source_identity_mismatch"
    }
    if ((Get-FileSha256 $GuardPath) -ne $ExpectedGuardSha256) {
        throw "guard_source_identity_mismatch"
    }
    if ((Get-FileSha256 $CollectorPath) -ne $ExpectedCollectorSha256) {
        throw "collector_source_identity_mismatch"
    }
    if (
        (Get-FileSha256 $CollectorTemplatePath) -ne
        $ExpectedCollectorTemplateSha256
    ) {
        throw "collector_template_source_identity_mismatch"
    }
    if ((Get-FileSha256 $InspectorPath) -ne $ExpectedInspectorSha256) {
        throw "continuity_inspector_source_identity_mismatch"
    }
    if ((Get-FileSha256 $ContinuityCorePath) -ne $ExpectedContinuityCoreSha256) {
        throw "continuity_core_source_identity_mismatch"
    }
    if (
        (Get-FileSha256 $ResolvedPreregistration) -ne
        $ExpectedPreregistrationArtifactSha256
    ) {
        throw "preregistration_artifact_identity_mismatch"
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
        "-PythonExe", $ResolvedPython,
        "-Preregistration", $ResolvedPreregistration,
        "-OutputDir", $ResolvedOutput,
        "-ApiKeyFile", $ResolvedApiKeyFile,
        "-BridgeEaRepositorySource", $ResolvedBridgeEaRepositorySource,
        "-BridgeEaDeployedSource", $ResolvedBridgeEaDeployedSource,
        "-BridgeEaDeployedEx4", $ResolvedBridgeEaDeployedEx4,
        "-ExpectedCollectorSha256", $ExpectedCollectorSha256,
        "-ExpectedCollectorTemplateSha256", $ExpectedCollectorTemplateSha256,
        "-ExpectedInspectorSha256", $ExpectedInspectorSha256,
        "-ExpectedContinuityCoreSha256", $ExpectedContinuityCoreSha256,
        "-ExpectedPreregistrationArtifactSha256", $ExpectedPreregistrationArtifactSha256,
        "-ExpectedPreregistrationBodySha256", $ExpectedPreregistrationBodySha256,
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
        $Report.order_authorized -eq $false -and
        $Report.immediate_market_trade_authorized -eq $false
    )
}

function Test-ExactGuardIdentity {
    param([object]$Report)
    return (
        [string]$Report.collector_source_sha256 -eq $ExpectedCollectorSha256 -and
        [string]$Report.collector_template_source_sha256 -eq $ExpectedCollectorTemplateSha256 -and
        [string]$Report.continuity_inspector_source_sha256 -eq $ExpectedInspectorSha256 -and
        [string]$Report.continuity_core_source_sha256 -eq $ExpectedContinuityCoreSha256 -and
        [string]$Report.preregistration_artifact_sha256 -eq $ExpectedPreregistrationArtifactSha256 -and
        [string]$Report.preregistration_body_sha256 -eq $ExpectedPreregistrationBodySha256 -and
        (Test-SamePath ([string]$Report.bridge_ea_repository_source_path) $ResolvedBridgeEaRepositorySource) -and
        (Test-SamePath ([string]$Report.bridge_ea_deployed_source_path) $ResolvedBridgeEaDeployedSource) -and
        (Test-SamePath ([string]$Report.bridge_ea_deployed_ex4_path) $ResolvedBridgeEaDeployedEx4) -and
        (Test-SamePath ([string]$Report.output_root) $ResolvedOutput)
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
if (-not (Test-ExactGuardIdentity $report)) {
    Write-StatusAndExit @{
        status = "health_refused"
        reason = "guard_health_identity_mismatch"
        guard_exit_code = $health.ExitCode
    } 2
}

$lockState = [string]$report.supervisor_lock_state
$writerGroupCount = [int]$report.writer_group_count
$guardAction = ""
$launchStatus = ""

if ($writerGroupCount -gt 0) {
    if ($writerGroupCount -ne 1) {
        Write-StatusAndExit @{
            status = "ownership_recovery_refused"
            reason = "duplicate_or_ambiguous_writer_groups"
            guard_exit_code = $health.ExitCode
            guard_status = [string]$report.status
            writer_group_count = $writerGroupCount
            supervisor_lock_state = $lockState
        } 4
    }
    if ($lockState -eq "held") {
        Write-StatusAndExit @{
            status = "detached_supervisor_detected"
            reason = "exact_writer_already_owned_by_non_task_supervisor"
            guard_exit_code = $health.ExitCode
            guard_status = [string]$report.status
            writer_group_count = 1
            supervisor_lock_state = $lockState
            task_ownership_proven = $false
        } 4
    }
    $adoptionAllowed = (
        $health.ExitCode -eq 3 -and
        $lockState -eq "available" -and
        @("running_unmanaged", "running_without_supervisor_lock") -contains [string]$report.status -and
        @("guard_identity_missing", "writer_not_owned_by_gap_v5_guard") -contains [string]$report.reason
    )
    if (-not $adoptionAllowed) {
        Write-StatusAndExit @{
            status = "ownership_recovery_refused"
            reason = "exact_single_writer_not_safely_adoptable"
            guard_exit_code = $health.ExitCode
            guard_status = [string]$report.status
            writer_group_count = 1
            supervisor_lock_state = $lockState
        } 4
    }
    $guardAction = "AdoptRunning"
    $launchStatus = "orphan_writer_adoption_allowed"
}
elseif (
    $health.ExitCode -eq 0 -and
    [string]$report.status -eq "prospective_window_complete"
) {
    Write-StatusAndExit @{
        status = "no_action_required"
        reason = "prospective_window_complete"
        guard_status = [string]$report.status
        writer_group_count = 0
    } 0
}
elseif ($health.ExitCode -eq 0) {
    Write-StatusAndExit @{
        status = "health_refused"
        reason = "success_health_without_exact_writer_or_completed_window"
        guard_status = [string]$report.status
        writer_group_count = 0
    } 2
}

$restartableStatuses = @("stopped_before_t0", "stopped_during_window")
$restartAllowed = (
    [string]::IsNullOrEmpty($guardAction) -and
    $health.ExitCode -eq 3 -and
    $restartableStatuses -contains [string]$report.status -and
    [string]$report.reason -eq "collector_writer_absent" -and
    $writerGroupCount -eq 0 -and
    $report.supervisor_lock_held -eq $false -and
    @("absent", "available") -contains $lockState
)
if ([string]::IsNullOrEmpty($guardAction) -and -not $restartAllowed) {
    Write-StatusAndExit @{
        status = "restart_refused"
        reason = "health_did_not_prove_restartable_absent_writer"
        guard_exit_code = $health.ExitCode
        guard_status = [string]$report.status
        writer_group_count = [int]$report.writer_group_count
        supervisor_lock_state = $lockState
    } 4
}

if ($restartAllowed) {
    $guardAction = "StartOrResume"
    $launchStatus = "restart_allowed_after_absent_writer_health"
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
    status = $launchStatus
    guard_action = $guardAction
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
    immediate_market_trade_authorized = $false
} | ConvertTo-Json -Compress -Depth 6)

$startArguments = @(New-GuardArguments $guardAction)
$supervisionDirectory = Join-Path $ResolvedOutput $SupervisionFolder
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
    $guardStdout = Join-Path $supervisionDirectory "guard.supervisor.stdout.log"
    $guardStderr = Join-Path $supervisionDirectory "guard.supervisor.stderr.log"
}
catch {
    Write-StatusAndExit @{
        status = "restart_launch_refused"
        reason = $_.Exception.Message
    } 2
}

# AGENT: HANDSHAKE: synchronous invocation keeps the Scheduled Task action,
# guard supervisor, and collector in one full-lifetime process ancestry.
try {
    & $PowerShellHost @startArguments 1>> $guardStdout 2>> $guardStderr
    $guardExitCode = [int]$LASTEXITCODE
}
catch {
    Write-StatusAndExit @{
        status = "synchronous_guard_launch_refused"
        reason = $_.Exception.Message
        guard_stdout_log = $guardStdout
        guard_stderr_log = $guardStderr
    } 2
}

$finalStatus = if ($guardExitCode -eq 0) {
    "synchronous_guard_completed"
}
else {
    "synchronous_guard_exited_nonzero"
}
Write-StatusAndExit @{
    status = $finalStatus
    guard_exit_code = $guardExitCode
    guard_stdout_log = $guardStdout
    guard_stderr_log = $guardStderr
    full_lifetime_task_ownership = $true
    guard_action = $guardAction
} $guardExitCode
