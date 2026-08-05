[CmdletBinding()]
param(
    [ValidateSet("Health", "AdoptRunning", "StartOrResume")]
    [string]$Action = "Health",

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
    [double]$StartupGraceSeconds = 180.0
)

# AGENT: ROLE: exclusive Windows supervisor for the resilient MTVCLC collector.
# AGENT: HANDSHAKE: exact prereg/output/source/config/key-file path -> one collection-only writer group.
# AGENT: ISOLATION: no secret value, signal/outcome/performance, issuer, runtime, activation, or trade path.
# AGENT: SIDE EFFECTS: guard identity/supervision metadata and the exact collector child for StartOrResume only.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$CollectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\capture_ig_mt4_m1_activity_resilient.py")
)
$InspectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\check_mt4_tick_volume_collector_continuity_resilient.py")
)
$TickIntervalSeconds = 2.0
$BarIntervalSeconds = 60.0
$BarLimit = 400
$HttpTimeoutSeconds = 5.0
$RolloverMode = "refuse"
$StatusSchema = "fxstack.mtvclc_collector_guard_status.resilient.v1"

function Resolve-ExistingFile {
    param([string]$Path, [string]$Reason)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw $Reason
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw $Reason
    }
    return [IO.Path]::GetFullPath($item.FullName)
}

function Resolve-ExistingDirectory {
    param([string]$Path, [string]$Reason)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw $Reason
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw $Reason
    }
    return [IO.Path]::GetFullPath($item.FullName).TrimEnd('\', '/')
}

function Write-JsonAndExit {
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
    Write-Output ($Payload | ConvertTo-Json -Compress -Depth 10)
    exit $Code
}

try {
    $ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
    $ResolvedPreregistration = Resolve-ExistingFile $Preregistration "preregistration_file_invalid"
    $ResolvedOutput = Resolve-ExistingDirectory $OutputDir "output_root_invalid"
    $ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
    $null = Resolve-ExistingFile $CollectorPath "collector_source_missing"
    $null = Resolve-ExistingFile $InspectorPath "continuity_inspector_missing"
}
catch {
    Write-JsonAndExit @{ status = "configuration_refused"; reason = $_.Exception.Message } 2
}

function Invoke-ContinuityInspection {
    param([switch]$InitializeGuard)
    $arguments = @(
        "-I",
        "-B",
        $InspectorPath,
        "--preregistration", $ResolvedPreregistration,
        "--output-dir", $ResolvedOutput,
        "--api-key-file", $ResolvedApiKeyFile,
        "--base-url", $BaseUrl,
        "--tick-interval-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $TickIntervalSeconds)),
        "--bar-interval-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $BarIntervalSeconds)),
        "--bar-limit", ([string]$BarLimit),
        "--http-timeout-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $HttpTimeoutSeconds))
    )
    if ($InitializeGuard) {
        $arguments += "--initialize-guard"
    }
    $raw = @(& $ResolvedPython @arguments 2>&1)
    $code = $LASTEXITCODE
    $text = (($raw | ForEach-Object { [string]$_ }) -join "`n").Trim()
    if ($code -ne 0) {
        throw "continuity_preflight_failed: $text"
    }
    try {
        return ($text | ConvertFrom-Json -ErrorAction Stop)
    }
    catch {
        throw "continuity_preflight_output_invalid"
    }
}

if (-not ("FxStackResilientNativeCommandLine" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class FxStackResilientNativeCommandLine
{
    [DllImport("shell32.dll", SetLastError = true)]
    private static extern IntPtr CommandLineToArgvW(
        [MarshalAs(UnmanagedType.LPWStr)] string commandLine,
        out int argumentCount
    );

    [DllImport("kernel32.dll")]
    private static extern IntPtr LocalFree(IntPtr memory);

    public static string[] Split(string commandLine)
    {
        int count;
        IntPtr pointer = CommandLineToArgvW(commandLine, out count);
        if (pointer == IntPtr.Zero)
        {
            throw new Win32Exception();
        }
        try
        {
            string[] result = new string[count];
            for (int index = 0; index < count; index++)
            {
                IntPtr item = Marshal.ReadIntPtr(pointer, index * IntPtr.Size);
                result[index] = Marshal.PtrToStringUni(item);
            }
            return result;
        }
        finally
        {
            LocalFree(pointer);
        }
    }
}
'@
}

function Get-OptionValue {
    param([string[]]$Arguments, [string]$Name)
    $values = @()
    for ($index = 0; $index -lt $Arguments.Count; $index++) {
        if ($Arguments[$index] -eq $Name) {
            if ($index + 1 -ge $Arguments.Count) {
                return [pscustomobject]@{ Present = $true; Valid = $false; Value = $null }
            }
            $values += $Arguments[$index + 1]
        }
        elseif ($Arguments[$index].StartsWith($Name + "=", [StringComparison]::OrdinalIgnoreCase)) {
            $values += $Arguments[$index].Substring($Name.Length + 1)
        }
    }
    if ($values.Count -eq 0) {
        return [pscustomobject]@{ Present = $false; Valid = $true; Value = $null }
    }
    if ($values.Count -ne 1) {
        return [pscustomobject]@{ Present = $true; Valid = $false; Value = $null }
    }
    return [pscustomobject]@{ Present = $true; Valid = $true; Value = [string]$values[0] }
}

function Test-SamePath {
    param([string]$Left, [string]$Right)
    try {
        $leftFull = [IO.Path]::GetFullPath($Left).TrimEnd('\', '/')
        $rightFull = [IO.Path]::GetFullPath($Right).TrimEnd('\', '/')
        return [string]::Equals($leftFull, $rightFull, [StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

function Test-DoubleValue {
    param([string]$Value, [double]$Expected)
    $parsed = 0.0
    if (-not [double]::TryParse(
        $Value,
        [Globalization.NumberStyles]::Float,
        [Globalization.CultureInfo]::InvariantCulture,
        [ref]$parsed
    )) {
        return $false
    }
    return [Math]::Abs($parsed - $Expected) -le 0.000000001
}

function Test-OptionalDoubleValue {
    param([object]$Option, [double]$Expected)
    if (-not [bool]$Option.Valid) {
        return $false
    }
    if (-not [bool]$Option.Present) {
        return $true
    }
    return Test-DoubleValue ([string]$Option.Value) $Expected
}

function Test-OptionalStringValue {
    param([object]$Option, [string]$Expected)
    if (-not [bool]$Option.Valid) {
        return $false
    }
    if (-not [bool]$Option.Present) {
        return $true
    }
    return [string]::Equals(
        [string]$Option.Value,
        $Expected,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Get-CollectorWriterGroups {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $candidates = @()
    foreach ($process in $processes) {
        $commandLine = [string]$process.CommandLine
        if ([string]::IsNullOrWhiteSpace($commandLine)) {
            continue
        }
        try {
            [string[]]$arguments = [FxStackResilientNativeCommandLine]::Split($commandLine)
        }
        catch {
            continue
        }
        $hasCollector = $false
        foreach ($argument in $arguments) {
            if (Test-SamePath $argument $CollectorPath) {
                $hasCollector = $true
                break
            }
        }
        if (-not $hasCollector) {
            continue
        }
        $output = Get-OptionValue $arguments "--output-dir"
        if (-not $output.Valid -or -not (Test-SamePath $output.Value $ResolvedOutput)) {
            continue
        }
        $prereg = Get-OptionValue $arguments "--preregistration"
        $apiKey = Get-OptionValue $arguments "--api-key-file"
        $base = Get-OptionValue $arguments "--base-url"
        $tick = Get-OptionValue $arguments "--tick-interval-secs"
        $bar = Get-OptionValue $arguments "--bar-interval-secs"
        $limit = Get-OptionValue $arguments "--bar-limit"
        $timeout = Get-OptionValue $arguments "--http-timeout-secs"
        $rollover = Get-OptionValue $arguments "--rollover-mode"
        $configurationMatches = (
            $prereg.Valid -and $prereg.Present -and (Test-SamePath $prereg.Value $ResolvedPreregistration) -and
            $apiKey.Valid -and $apiKey.Present -and (Test-SamePath $apiKey.Value $ResolvedApiKeyFile) -and
            $base.Valid -and $base.Present -and [string]::Equals($base.Value, $BaseUrl, [StringComparison]::OrdinalIgnoreCase) -and
            (Test-OptionalDoubleValue $tick $TickIntervalSeconds) -and
            (Test-OptionalDoubleValue $bar $BarIntervalSeconds) -and
            $limit.Valid -and ((-not $limit.Present) -or $limit.Value -eq ([string]$BarLimit)) -and
            (Test-OptionalDoubleValue $timeout $HttpTimeoutSeconds) -and
            (Test-OptionalStringValue $rollover $RolloverMode)
        )
        $candidates += [pscustomobject]@{
            ProcessId = [int]$process.ProcessId
            ParentProcessId = [int]$process.ParentProcessId
            CreationDate = $process.CreationDate
            ConfigurationMatches = [bool]$configurationMatches
        }
    }
    $byId = @{}
    foreach ($candidate in $candidates) {
        $byId[[int]$candidate.ProcessId] = $candidate
    }
    $groups = @{}
    foreach ($candidate in $candidates) {
        $root = $candidate
        $seen = @{}
        while ($byId.ContainsKey([int]$root.ParentProcessId) -and -not $seen.ContainsKey([int]$root.ParentProcessId)) {
            $seen[[int]$root.ProcessId] = $true
            $root = $byId[[int]$root.ParentProcessId]
        }
        $rootId = [int]$root.ProcessId
        if (-not $groups.ContainsKey($rootId)) {
            $groups[$rootId] = @()
        }
        $groups[$rootId] += $candidate
    }
    $result = @()
    foreach ($rootId in @($groups.Keys | Sort-Object)) {
        $members = @($groups[$rootId])
        $created = @($members | Sort-Object CreationDate | Select-Object -First 1)[0].CreationDate
        $result += [pscustomobject]@{
            RootProcessId = [int]$rootId
            ProcessIds = @($members | ForEach-Object { [int]$_.ProcessId } | Sort-Object)
            ConfigurationMatches = (@($members | Where-Object { -not $_.ConfigurationMatches }).Count -eq 0)
            CreationDate = $created
        }
    }
    return @($result)
}

function New-BaseStatus {
    param([object]$Inspection, [object[]]$WriterGroups)
    return @{
        action = $Action
        preregistration_body_sha256 = [string]$Inspection.preregistration_body_sha256
        preregistration_artifact_sha256 = [string]$Inspection.preregistration_artifact_sha256
        collector_source_sha256 = [string]$Inspection.collector_source_sha256
        collector_support_source_sha256 = [string]$Inspection.collector_support_source_sha256
        capture_integrity_contract_sha256 = [string]$Inspection.capture_integrity_contract_sha256
        policy_sha256 = [string]$Inspection.policy_sha256
        output_root = [string]$Inspection.output_root
        prospective_t0_epoch = [double]$Inspection.prospective_t0_epoch
        prospective_end_epoch_exclusive = [double]$Inspection.prospective_end_epoch_exclusive
        manifest_sequence = [int64]$Inspection.manifest_sequence
        manifest_last_write_epoch = $Inspection.manifest_last_write_epoch
        active_journal_present = [bool]$Inspection.active_journal_present
        active_journal_sequence = [int64]$Inspection.active_journal_sequence
        active_journal_last_write_epoch = $Inspection.active_journal_last_write_epoch
        collector_activity_last_write_epoch = $Inspection.collector_activity_last_write_epoch
        manifest_tail_check_only = $true
        guard_identity_present = [bool]$Inspection.guard_identity_present
        writer_group_count = @($WriterGroups).Count
        writer_root_pids = @($WriterGroups | ForEach-Object { [int]$_.RootProcessId })
        stdout_log = (Join-Path $ResolvedOutput "supervision-resilient\collector.stdout.log")
        stderr_log = (Join-Path $ResolvedOutput "supervision-resilient\collector.stderr.log")
    }
}

function Get-WriterLockState {
    $lockPath = Join-Path $ResolvedOutput "supervision-resilient\collector-writer.lock"
    if (-not (Test-Path -LiteralPath $lockPath -PathType Leaf)) {
        return "absent"
    }
    try {
        $probe = [IO.File]::Open(
            $lockPath,
            [IO.FileMode]::Open,
            [IO.FileAccess]::ReadWrite,
            [IO.FileShare]::None
        )
        $probe.Dispose()
        return "available"
    }
    catch [IO.IOException] {
        return "held"
    }
    catch {
        return "unknown"
    }
}

if ($Action -eq "Health") {
    try {
        $inspection = Invoke-ContinuityInspection
        $writers = @(Get-CollectorWriterGroups)
    }
    catch {
        Write-JsonAndExit @{ status = "health_refused"; reason = $_.Exception.Message } 2
    }
    $status = New-BaseStatus $inspection $writers
    $lockState = Get-WriterLockState
    $status["supervisor_lock_state"] = $lockState
    $status["supervisor_lock_held"] = ($lockState -eq "held")
    if ($writers.Count -gt 1) {
        $status["status"] = "duplicate_writer_groups"
        $status["reason"] = "more_than_one_independent_collector_writer"
        Write-JsonAndExit $status 4
    }
    if ($writers.Count -eq 1 -and -not [bool]$writers[0].ConfigurationMatches) {
        $status["status"] = "writer_configuration_mismatch"
        $status["reason"] = "running_writer_not_pinned_to_guard_configuration"
        Write-JsonAndExit $status 4
    }
    $now = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $t0 = [double]$inspection.prospective_t0_epoch
    $end = [double]$inspection.prospective_end_epoch_exclusive
    if ($writers.Count -eq 0) {
        if ($now -ge $end -and [int64]$inspection.manifest_sequence -gt 0) {
            $status["status"] = "prospective_window_complete"
            Write-JsonAndExit $status 0
        }
        $status["status"] = if ($now -lt $t0) { "stopped_before_t0" } else { "stopped_during_window" }
        $status["reason"] = "collector_writer_absent"
        Write-JsonAndExit $status 3
    }
    if (-not [bool]$inspection.guard_identity_present) {
        $status["status"] = "running_unmanaged"
        $status["reason"] = "guard_identity_missing"
        Write-JsonAndExit $status 3
    }
    if ($lockState -ne "held") {
        $status["status"] = "running_without_supervisor_lock"
        $status["reason"] = "writer_not_owned_by_guard_supervisor"
        Write-JsonAndExit $status 3
    }
    if ($now -ge $end) {
        $status["status"] = "window_closed_writer_still_running"
        Write-JsonAndExit $status 3
    }
    if ($now -lt $t0) {
        $status["status"] = "waiting_for_sealed_t0"
        Write-JsonAndExit $status 0
    }
    if ($null -eq $inspection.collector_activity_last_write_epoch) {
        $createdAt = [DateTimeOffset]$writers[0].CreationDate
        $age = [DateTimeOffset]::UtcNow.Subtract($createdAt.ToUniversalTime()).TotalSeconds
        $status["writer_age_seconds"] = [Math]::Max(0.0, $age)
        if ($age -le $StartupGraceSeconds) {
            $status["status"] = "starting"
            Write-JsonAndExit $status 0
        }
        $status["status"] = "manifest_missing_after_startup_grace"
        Write-JsonAndExit $status 3
    }
    $activityAge = [Math]::Max(0.0, ([double]$now - [double]$inspection.collector_activity_last_write_epoch))
    $status["collector_activity_age_seconds"] = $activityAge
    if ($activityAge -gt $MaximumManifestAgeSeconds) {
        $status["status"] = "collector_activity_stale"
        Write-JsonAndExit $status 3
    }
    $status["status"] = "healthy"
    Write-JsonAndExit $status 0
}

$SupervisionDirectory = Join-Path $ResolvedOutput "supervision-resilient"
$lockStream = $null
try {
    if (-not (Test-Path -LiteralPath $SupervisionDirectory -PathType Container)) {
        $null = New-Item -ItemType Directory -Path $SupervisionDirectory -ErrorAction Stop
    }
    $lockPath = Join-Path $SupervisionDirectory "collector-writer.lock"
    $lockStream = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
}
catch {
    Write-JsonAndExit @{
        status = "duplicate_guard_supervisor"
        reason = "exclusive_writer_lock_unavailable"
        output_root = $ResolvedOutput
    } 4
}

try {
    $writers = @(Get-CollectorWriterGroups)
    if ($Action -eq "AdoptRunning") {
        if ($writers.Count -ne 1) {
            Write-JsonAndExit @{
                status = "adoption_refused"
                reason = "exactly_one_existing_writer_required"
                output_root = $ResolvedOutput
                writer_group_count = $writers.Count
            } 4
        }
        if (-not [bool]$writers[0].ConfigurationMatches) {
            Write-JsonAndExit @{
                status = "adoption_refused"
                reason = "running_writer_not_pinned_to_guard_configuration"
                output_root = $ResolvedOutput
                writer_root_pids = @([int]$writers[0].RootProcessId)
            } 4
        }
        try {
            $inspection = Invoke-ContinuityInspection -InitializeGuard
        }
        catch {
            Write-JsonAndExit @{
                status = "adoption_refused"
                reason = $_.Exception.Message
                output_root = $ResolvedOutput
            } 2
        }
        Write-Output (@{
            schema_version = $StatusSchema
            status = "running_writer_adopted"
            collection_only = $true
            preregistration_body_sha256 = [string]$inspection.preregistration_body_sha256
            collector_source_sha256 = [string]$inspection.collector_source_sha256
            capture_integrity_contract_sha256 = [string]$inspection.capture_integrity_contract_sha256
            manifest_sequence = [int64]$inspection.manifest_sequence
            output_root = $ResolvedOutput
            writer_root_pids = @([int]$writers[0].RootProcessId)
            supervisor_lock_held = $true
        } | ConvertTo-Json -Compress -Depth 6)
        while ($true) {
            Start-Sleep -Seconds 5
            $observed = @(Get-CollectorWriterGroups)
            if ($observed.Count -eq 0) {
                Write-JsonAndExit @{
                    status = "adopted_writer_exited"
                    output_root = $ResolvedOutput
                } 0
            }
            if ($observed.Count -ne 1 -or -not [bool]$observed[0].ConfigurationMatches) {
                Write-JsonAndExit @{
                    status = "adopted_writer_drift_detected"
                    reason = "writer_count_or_configuration_changed"
                    output_root = $ResolvedOutput
                    writer_group_count = $observed.Count
                } 4
            }
        }
    }
    if ($writers.Count -gt 0) {
        Write-JsonAndExit @{
            status = "collector_already_running"
            reason = "existing_writer_for_output_root"
            output_root = $ResolvedOutput
            writer_group_count = $writers.Count
            writer_root_pids = @($writers | ForEach-Object { [int]$_.RootProcessId })
        } 4
    }
    try {
        $inspection = Invoke-ContinuityInspection -InitializeGuard
    }
    catch {
        Write-JsonAndExit @{
            status = "start_refused"
            reason = $_.Exception.Message
            output_root = $ResolvedOutput
        } 2
    }
    $collectorArguments = @(
        "-I",
        "-u",
        $CollectorPath,
        "--base-url", $BaseUrl,
        "--api-key-file", $ResolvedApiKeyFile,
        "--preregistration", $ResolvedPreregistration,
        "--output-dir", $ResolvedOutput,
        "--tick-interval-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $TickIntervalSeconds)),
        "--bar-interval-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $BarIntervalSeconds)),
        "--bar-limit", ([string]$BarLimit),
        "--http-timeout-secs", ([string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $HttpTimeoutSeconds)),
        "--rollover-mode", $RolloverMode
    )
    $stdoutLog = Join-Path $SupervisionDirectory "collector.stdout.log"
    $stderrLog = Join-Path $SupervisionDirectory "collector.stderr.log"
    Write-Output (@{
        schema_version = $StatusSchema
        status = "starting_or_resuming"
        collection_only = $true
        preregistration_body_sha256 = [string]$inspection.preregistration_body_sha256
        collector_source_sha256 = [string]$inspection.collector_source_sha256
        capture_integrity_contract_sha256 = [string]$inspection.capture_integrity_contract_sha256
        manifest_sequence = [int64]$inspection.manifest_sequence
        output_root = $ResolvedOutput
    } | ConvertTo-Json -Compress -Depth 6)
    & $ResolvedPython @collectorArguments 1>> $stdoutLog 2>> $stderrLog
    $collectorExitCode = $LASTEXITCODE
    $finalStatus = if ($collectorExitCode -eq 0) { "collector_window_finished" } else { "collector_exited" }
    Write-JsonAndExit @{
        status = $finalStatus
        collector_exit_code = [int]$collectorExitCode
        output_root = $ResolvedOutput
        stdout_log = $stdoutLog
        stderr_log = $stderrLog
    } ([int]$collectorExitCode)
}
finally {
    if ($null -ne $lockStream) {
        $lockStream.Dispose()
    }
}
