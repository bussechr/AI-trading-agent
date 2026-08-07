[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Install", "Preview", "Remove")]
    [string]$Action = "Preview",

    [ValidatePattern("^[A-Za-z0-9_.-]{1,100}$")]
    [string]$TaskName = "TradingAgentMtvclcCapturePreservation",

    [ValidateSet("AtLogOn", "AtStartup")]
    [string]$TriggerMode = "AtLogOn",

    [Parameter(Mandatory = $true)]
    [string]$Preregistration,

    [Parameter(Mandatory = $true)]
    [string]$CaptureRoot,

    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedGuardIdentitySha256,

    [ValidatePattern("^$|^[0-9a-fA-F]{64}$")]
    [string]$PinnedPreservationScriptSha256 = "",

    [ValidatePattern("^[A-Za-z]$")]
    [string]$ExpectedCaptureDriveLetter = "D",

    [long]$WarningFreeBytes = 500GB,
    [long]$HardMinimumFreeBytes = 311GB,

    [ValidateRange(10.0, 86400.0)]
    [double]$MaximumJournalAgeSeconds = 120.0,

    [ValidateRange(60.0, 86400.0)]
    [double]$MaximumManifestAgeSeconds = 4200.0,

    [ValidateRange(0.0, 300.0)]
    [double]$MinimumClosedAgeSeconds = 120.0,

    [ValidateRange(0, 60)]
    [int]$StabilityProbeSeconds = 2,

    [ValidateRange(0.0, 300.0)]
    [double]$MaximumFutureClockSkewSeconds = 5.0,

    [ValidateRange(15, 1440)]
    [int]$PreservationIntervalMinutes = 60
)

# AGENT: ROLE: reversible identity-safe Scheduled Task registrar for exact MTVCLC capture preservation only.
# AGENT: HANDSHAKE: AtLogOn/hourly (or administrator AtStartup) -> hash-pinned append-only preservation script and exact source/destination tuple.
# AGENT: ISOLATION: stores no credential or key path/value, never parses evidence, and cannot evaluate, sign, activate, run, or trade.
# AGENT: SIDE EFFECTS: mutates only the exact owned task definition; it never starts the task or touches capture/backup data.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$TaskPath = "\"

function Assert-NoReparsePath {
    param([string]$Path, [string]$Reason)

    $currentPath = [IO.Path]::GetFullPath($Path)
    while ($true) {
        $currentItem = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw $Reason
        }
        $parent = [IO.Directory]::GetParent($currentPath)
        if ($null -eq $parent) {
            break
        }
        $currentPath = $parent.FullName
    }
}

function Resolve-ExistingFile {
    param([string]$Path, [string]$Reason)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $Reason
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        throw $Reason
    }
    if ($item.PSIsContainer) {
        throw $Reason
    }
    Assert-NoReparsePath $item.FullName $Reason
    return [IO.Path]::GetFullPath($item.FullName)
}

function Resolve-ExistingDirectory {
    param([string]$Path, [string]$Reason)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $Reason
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw $Reason
    }
    Assert-NoReparsePath $item.FullName $Reason
    return [IO.Path]::GetFullPath($item.FullName).TrimEnd('\', '/')
}

function Resolve-CanonicalTaskPath {
    param([string]$Path, [bool]$Directory, [string]$Reason)

    if ([string]::IsNullOrWhiteSpace($Path) -or -not [IO.Path]::IsPathRooted($Path)) {
        throw $Reason
    }
    $resolved = [IO.Path]::GetFullPath($Path)
    if ($Directory) {
        $resolved = $resolved.TrimEnd('\', '/')
    }
    return $resolved
}

function Quote-TaskArgument {
    param([string]$Value)

    if (
        $Value.Contains('"') -or
        $Value.Contains("`r") -or
        $Value.Contains("`n") -or
        $Value.EndsWith('\')
    ) {
        throw "scheduled_task_argument_not_safely_quotable"
    }
    return '"' + $Value + '"'
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

function Get-StringSha256 {
    param([string]$Value)

    $bytes = [Text.Encoding]::UTF8.GetBytes($Value)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

function Test-SamePath {
    param([string]$Left, [string]$Right)

    try {
        return [string]::Equals(
            [IO.Path]::GetFullPath($Left),
            [IO.Path]::GetFullPath($Right),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

function Test-IsAdministrator {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Assert-ExactTupleNames {
    param([string]$PreregistrationPath, [string]$CapturePath)

    $preregName = [IO.Path]::GetFileName($PreregistrationPath)
    $legacyPreregMatch = [regex]::Match(
        $preregName,
        "^mtvclc_v1_preregistration_([0-9a-f]{64})\.json$",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    $gapV3PreregMatch = [regex]::Match(
        $preregName,
        "^mtvclc_gap_v3_preregistration_([0-9a-f]{64})\.json$",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($legacyPreregMatch.Success) {
        $preregMatch = $legacyPreregMatch
        $allowedPreregistrationParents = @(
            "mtvclc_prereg_sealed_resilient_v1",
            "mtvclc_prereg_sealed_watermark_v2"
        )
        $captureNamePrefix = "mtvclc_prospective_capture_"
        $guardIdentityName = "collector-guard.identity.resilient.v1.json"
        $preservationFilenameSchemaVersion = "legacy_resilient_filename_contract"
    }
    elseif ($gapV3PreregMatch.Success) {
        $preregMatch = $gapV3PreregMatch
        $captureNamePrefix = "mtvclc_prospective_capture_gap_v3_"
        $preregParent = [IO.Directory]::GetParent($PreregistrationPath)
        if (
            $null -ne $preregParent -and
            $preregParent.Name.Equals(
                "mtvclc_prereg_sealed_runtime_bound_v5",
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            $allowedPreregistrationParents = @(
                "mtvclc_prereg_sealed_runtime_bound_v5"
            )
            $guardIdentityName = "collector-guard.identity.gap-v5.v1.json"
            $preservationFilenameSchemaVersion = (
                "fxstack.scalp.mtvclc_preservation_filenames.v5"
            )
        }
        else {
            $allowedPreregistrationParents = @(
                "mtvclc_prereg_sealed_runtime_bound_v3",
                "mtvclc_prereg_sealed_runtime_bound_v4"
            )
            $guardIdentityName = "collector-guard.identity.gap-v3.v1.json"
            $preservationFilenameSchemaVersion = (
                "fxstack.scalp.mtvclc_preservation_filenames.v3"
            )
        }
    }
    else {
        throw "preregistration_filename_not_exact_resilient_contract"
    }
    $preregParent = [IO.Directory]::GetParent($PreregistrationPath)
    if (
        $null -eq $preregParent -or
        -not ($allowedPreregistrationParents | Where-Object {
            $preregParent.Name.Equals($_, [StringComparison]::OrdinalIgnoreCase)
        })
    ) {
        throw "preregistration_parent_not_exact_resilient_contract"
    }
    $expectedCaptureName = (
        $captureNamePrefix + $preregMatch.Groups[1].Value.Substring(0, 16)
    )
    $captureName = [IO.Path]::GetFileName($CapturePath.TrimEnd('\', '/'))
    if (-not $captureName.Equals($expectedCaptureName, [StringComparison]::OrdinalIgnoreCase)) {
        throw "preregistration_capture_tuple_mismatch"
    }
    return [pscustomobject]@{
        GuardIdentityName = $guardIdentityName
        PreservationFilenameSchemaVersion = $preservationFilenameSchemaVersion
    }
}

$PreservationPath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\29_preserve_mtvclc_capture.ps1"
) "preservation_script_missing"
$WindowsPowerShell = Resolve-ExistingFile (
    Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
) "windows_powershell_missing"
$actualPreservationSha256 = Get-FileSha256 $PreservationPath
$pinnedPreservationSha256 = if ([string]::IsNullOrWhiteSpace($PinnedPreservationScriptSha256)) {
    $actualPreservationSha256
}
else {
    $PinnedPreservationScriptSha256.ToLowerInvariant()
}
if (
    $Action -ne "Remove" -and
    $pinnedPreservationSha256 -ne $actualPreservationSha256
) {
    throw "pinned_preservation_script_sha256_mismatch"
}
if ($WarningFreeBytes -le 0 -or $HardMinimumFreeBytes -le 0) {
    throw "free_space_threshold_invalid"
}
if ($WarningFreeBytes -lt $HardMinimumFreeBytes) {
    throw "warning_threshold_below_hard_minimum"
}

$expectedPreregHash = $ExpectedPreregistrationSha256.ToLowerInvariant()
$expectedGuardHash = $ExpectedGuardIdentitySha256.ToLowerInvariant()
if ($Action -eq "Remove") {
    $ResolvedPreregistration = Resolve-CanonicalTaskPath `
        $Preregistration $false "preregistration_path_invalid"
    $ResolvedCapture = Resolve-CanonicalTaskPath $CaptureRoot $true "capture_root_invalid"
    $ResolvedBackup = Resolve-CanonicalTaskPath $BackupRoot $true "backup_root_invalid"
}
else {
    $ResolvedPreregistration = Resolve-ExistingFile `
        $Preregistration "preregistration_path_invalid"
    $ResolvedCapture = Resolve-ExistingDirectory $CaptureRoot "capture_root_invalid"
    $ResolvedBackup = Resolve-ExistingDirectory $BackupRoot "backup_root_invalid"
}
$TupleNames = Assert-ExactTupleNames $ResolvedPreregistration $ResolvedCapture
$GuardIdentityName = [string]$TupleNames.GuardIdentityName
$PreservationFilenameSchemaVersion = [string]$TupleNames.PreservationFilenameSchemaVersion

if ($Action -ne "Remove") {
    $guardPath = Resolve-ExistingFile (
        Join-Path $ResolvedCapture $GuardIdentityName
    ) "guard_identity_missing_or_invalid"
    if ((Get-FileSha256 $ResolvedPreregistration) -ne $expectedPreregHash) {
        throw "preregistration_sha256_mismatch"
    }
    if ((Get-FileSha256 $guardPath) -ne $expectedGuardHash) {
        throw "guard_identity_sha256_mismatch"
    }
    $captureVolume = Get-Volume -FilePath $ResolvedCapture -ErrorAction Stop
    if ([string]$captureVolume.DriveLetter -ne $ExpectedCaptureDriveLetter.ToUpperInvariant()) {
        throw "capture_volume_drive_letter_mismatch"
    }
    $backupVolume = Get-Volume -FilePath $ResolvedBackup -ErrorAction Stop
    if ([string]$captureVolume.UniqueId -eq [string]$backupVolume.UniqueId) {
        throw "backup_volume_must_differ_from_capture_volume"
    }
}

function Format-InvariantDouble {
    param([double]$Value)
    return [string]::Format([Globalization.CultureInfo]::InvariantCulture, "{0:R}", $Value)
}

$argumentParts = @(
    "-NoProfile",
    "-NonInteractive",
    "-WindowStyle", "Hidden",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-TaskArgument $PreservationPath),
    "-Preregistration", (Quote-TaskArgument $ResolvedPreregistration),
    "-CaptureRoot", (Quote-TaskArgument $ResolvedCapture),
    "-BackupRoot", (Quote-TaskArgument $ResolvedBackup),
    "-ExpectedPreregistrationSha256", $expectedPreregHash,
    "-ExpectedGuardIdentitySha256", $expectedGuardHash,
    "-ExpectedPreservationScriptSha256", $pinnedPreservationSha256,
    "-ExpectedCaptureDriveLetter", $ExpectedCaptureDriveLetter.ToUpperInvariant(),
    "-WarningFreeBytes", ([string]$WarningFreeBytes),
    "-HardMinimumFreeBytes", ([string]$HardMinimumFreeBytes),
    "-MaximumJournalAgeSeconds", (Format-InvariantDouble $MaximumJournalAgeSeconds),
    "-MaximumManifestAgeSeconds", (Format-InvariantDouble $MaximumManifestAgeSeconds),
    "-MinimumClosedAgeSeconds", (Format-InvariantDouble $MinimumClosedAgeSeconds),
    "-StabilityProbeSeconds", ([string]$StabilityProbeSeconds),
    "-MaximumFutureClockSkewSeconds", (Format-InvariantDouble $MaximumFutureClockSkewSeconds)
)
$expectedArguments = $argumentParts -join " "
$tupleIdentitySha256 = Get-StringSha256 $expectedArguments
$TaskDescriptionPrefix = (
    "FXSTACK_OWNER=mtvclc_capture_preservation_task_v1; " +
    "collection-only append-only replica; no evidence evaluation or trading authority; " +
    "tuple=" + $tupleIdentitySha256 + "; trigger="
)
$TaskDescriptionSuffix = "; repeat=" + $PreservationIntervalMinutes + "m."
$LogOnTaskDescription = $TaskDescriptionPrefix + "AtLogOn" + $TaskDescriptionSuffix
$StartupTaskDescription = $TaskDescriptionPrefix + "AtStartup" + $TaskDescriptionSuffix
$taskDescription = if ($TriggerMode -eq "AtStartup") {
    $StartupTaskDescription
}
else {
    $LogOnTaskDescription
}
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUserName = $currentIdentity.Name
$currentUserSid = $currentIdentity.User.Value

function Get-OwnedScheduledTaskDiagnostics {
    param([object]$Task)

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return [ordered]@{
            owned = $false
            reason = "action_count_mismatch"
            action_count = $actions.Count
        }
    }
    $triggers = @($Task.Triggers)
    if ($triggers.Count -ne 2) {
        return [ordered]@{
            owned = $false
            reason = "trigger_count_mismatch"
            action_count = 1
            trigger_count = $triggers.Count
        }
    }
    $taskUser = [string]$Task.Principal.UserId
    $taskUserSid = ""
    try {
        $taskUserSid = (
            [Security.Principal.NTAccount]::new($taskUser).Translate(
                [Security.Principal.SecurityIdentifier]
            )
        ).Value
    }
    catch {
        $taskUserSid = ""
    }
    $userMatches = (
        [string]::Equals($taskUser, $currentUserName, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($taskUser, $currentUserSid, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($taskUserSid, $currentUserSid, [StringComparison]::OrdinalIgnoreCase)
    )
    $descriptionMatches = [string]$Task.Description -eq $taskDescription
    $atLogOnIdentity = (
        $TriggerMode -eq "AtLogOn" -and
        @("Interactive", "InteractiveToken") -contains [string]$Task.Principal.LogonType -and
        [string]$Task.Principal.RunLevel -eq "Limited"
    )
    $atStartupIdentity = (
        $TriggerMode -eq "AtStartup" -and
        [string]$Task.Principal.LogonType -eq "S4U" -and
        [string]$Task.Principal.RunLevel -eq "Highest"
    )
    $settings = $Task.Settings
    $pathMatches = [string]$Task.TaskPath -eq $TaskPath
    $executeMatches = Test-SamePath ([string]$actions[0].Execute) $WindowsPowerShell
    $argumentsMatch = [string]::Equals(
        [string]$actions[0].Arguments,
        $expectedArguments,
        [StringComparison]::Ordinal
    )
    $ignoreNewMatches = [string]$settings.MultipleInstances -eq "IgnoreNew"
    $startWhenAvailableMatches = [bool]$settings.StartWhenAvailable
    $expectedLifecycleClass = if ($TriggerMode -eq "AtStartup") {
        "MSFT_TaskBootTrigger"
    }
    else {
        "MSFT_TaskLogonTrigger"
    }
    $lifecycleTriggers = @(
        $triggers | Where-Object {
            [string]$_.CimClass.CimClassName -eq $expectedLifecycleClass
        }
    )
    $repetitionTriggers = @(
        $triggers | Where-Object {
            [string]$_.CimClass.CimClassName -eq "MSFT_TaskTimeTrigger"
        }
    )
    $triggerTopologyMatches = (
        $lifecycleTriggers.Count -eq 1 -and
        $repetitionTriggers.Count -eq 1
    )
    $logOnTriggerUserMatches = $true
    if ($TriggerMode -eq "AtLogOn" -and $lifecycleTriggers.Count -eq 1) {
        $triggerUser = [string]$lifecycleTriggers[0].UserId
        $triggerUserSid = ""
        try {
            $triggerUserSid = (
                [Security.Principal.NTAccount]::new($triggerUser).Translate(
                    [Security.Principal.SecurityIdentifier]
                )
            ).Value
        }
        catch {
            $triggerUserSid = ""
        }
        $logOnTriggerUserMatches = (
            [string]::Equals($triggerUser, $currentUserName, [StringComparison]::OrdinalIgnoreCase) -or
            [string]::Equals($triggerUser, $currentUserSid, [StringComparison]::OrdinalIgnoreCase) -or
            [string]::Equals($triggerUserSid, $currentUserSid, [StringComparison]::OrdinalIgnoreCase)
        )
    }
    $repetitionIntervalMatches = $false
    $repetitionDurationMatches = $false
    if ($repetitionTriggers.Count -eq 1) {
        try {
            $taskInterval = [Xml.XmlConvert]::ToTimeSpan(
                [string]$repetitionTriggers[0].Repetition.Interval
            )
            $taskDuration = [Xml.XmlConvert]::ToTimeSpan(
                [string]$repetitionTriggers[0].Repetition.Duration
            )
            $repetitionIntervalMatches = (
                $taskInterval -eq (New-TimeSpan -Minutes $PreservationIntervalMinutes)
            )
            $repetitionDurationMatches = ($taskDuration -eq (New-TimeSpan -Days 3650))
        }
        catch {
            $repetitionIntervalMatches = $false
            $repetitionDurationMatches = $false
        }
    }
    return [ordered]@{
        owned = (
            $pathMatches -and
            $executeMatches -and
            $argumentsMatch -and
            $descriptionMatches -and
            $userMatches -and
            ($atLogOnIdentity -or $atStartupIdentity) -and
            $ignoreNewMatches -and
            $startWhenAvailableMatches -and
            $triggerTopologyMatches -and
            $logOnTriggerUserMatches -and
            $repetitionIntervalMatches -and
            $repetitionDurationMatches
        )
        action_count = 1
        trigger_count = $triggers.Count
        task_path = [string]$Task.TaskPath
        task_path_matches = $pathMatches
        execute_matches = $executeMatches
        arguments_match = $argumentsMatch
        description_matches = $descriptionMatches
        principal_user = $taskUser
        principal_user_sid = $taskUserSid
        principal_user_matches = $userMatches
        principal_logon_type = [string]$Task.Principal.LogonType
        principal_run_level = [string]$Task.Principal.RunLevel
        at_logon_identity_matches = $atLogOnIdentity
        at_startup_identity_matches = $atStartupIdentity
        multiple_instances = [string]$settings.MultipleInstances
        ignore_new_matches = $ignoreNewMatches
        start_when_available_matches = $startWhenAvailableMatches
        trigger_topology_matches = $triggerTopologyMatches
        logon_trigger_user_matches = $logOnTriggerUserMatches
        repetition_interval_matches = $repetitionIntervalMatches
        repetition_duration_matches = $repetitionDurationMatches
    }
}

function Test-OwnedScheduledTask {
    param([object]$Task)
    return [bool](Get-OwnedScheduledTaskDiagnostics $Task).owned
}

if ($Action -eq "Remove") {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output ("[mtvclc-preservation] task already absent: {0}" -f $TaskName)
        exit 0
    }
    if (-not (Test-OwnedScheduledTask $existing)) {
        $diagnostics = Get-OwnedScheduledTaskDiagnostics $existing
        throw (
            "scheduled_task_identity_mismatch_refusing_remove:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 5)
        )
    }
    if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
        throw "scheduled_task_mutation_requires_elevated_administrator"
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister exact MTVCLC preservation task")) {
        Unregister-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction Stop
        Write-Output (
            "[mtvclc-preservation] task removed: {0}; capture and backup data were untouched" -f
            $TaskName
        )
    }
    exit 0
}

$taskAction = New-ScheduledTaskAction `
    -Execute $WindowsPowerShell `
    -Argument $expectedArguments
$lifecycleTrigger = if ($TriggerMode -eq "AtStartup") {
    New-ScheduledTaskTrigger -AtStartup
}
else {
    New-ScheduledTaskTrigger -AtLogOn -User $currentUserName
}
$repeatTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $PreservationIntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$taskPrincipal = if ($TriggerMode -eq "AtStartup") {
    New-ScheduledTaskPrincipal `
        -UserId $currentUserName `
        -LogonType S4U `
        -RunLevel Highest
}
else {
    New-ScheduledTaskPrincipal `
        -UserId $currentUserName `
        -LogonType Interactive `
        -RunLevel Limited
}
$definition = New-ScheduledTask `
    -Action $taskAction `
    -Trigger @($lifecycleTrigger, $repeatTrigger) `
    -Settings $settings `
    -Principal $taskPrincipal `
    -Description $taskDescription

if ($Action -eq "Preview") {
    Write-Output (@{
        schema_version = "fxstack.mtvclc_capture_preservation_task_preview.v1"
        task_name = $TaskName
        task_path = $TaskPath
        description = $taskDescription
        execute = [string]$taskAction.Execute
        arguments = [string]$taskAction.Arguments
        trigger_mode = $TriggerMode
        startup_trigger = ($TriggerMode -eq "AtStartup")
        logon_trigger = ($TriggerMode -eq "AtLogOn")
        repetition_interval_minutes = $PreservationIntervalMinutes
        repetition_duration_days = 3650
        multiple_instances = "IgnoreNew"
        start_when_available = $true
        execution_time_limit_seconds = 0
        principal_user = $currentUserName
        principal_logon_type = if ($TriggerMode -eq "AtStartup") { "S4U" } else { "Interactive" }
        principal_run_level = if ($TriggerMode -eq "AtStartup") { "Highest" } else { "Limited" }
        tuple_identity_sha256 = $tupleIdentitySha256
        preservation_script_sha256 = $pinnedPreservationSha256
        preregistration_sha256 = $expectedPreregHash
        guard_identity_sha256 = $expectedGuardHash
        preservation_filename_schema_version = $PreservationFilenameSchemaVersion
        guard_identity_filename = $GuardIdentityName
        preregistration_path = $ResolvedPreregistration
        capture_root = $ResolvedCapture
        backup_root = $ResolvedBackup
        collection_only = $true
        preservation_only = $true
        evidence_rows_interpreted = $false
        active_journal_copied = $false
        evaluation_performed = $false
        signal_computation_authorized = $false
        outcome_access_authorized = $false
        performance_computation_authorized = $false
        success_claim_authorized = $false
        issuer_authorized = $false
        signature_authorized = $false
        authority = $false
        authority_granted = $false
        runtime_authorized = $false
        activation_authorized = $false
        broker_access_authorized = $false
        order_authorized = $false
        key_value_read = $false
        key_value_stored = $false
        mutation_performed = $false
    } | ConvertTo-Json -Compress -Depth 8)
    exit 0
}

if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
    throw "scheduled_task_mutation_requires_elevated_administrator"
}
$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    if (Test-OwnedScheduledTask $existing) {
        Write-Output (
            "[mtvclc-preservation] task already current: {0}; no mutation performed" -f
            $TaskName
        )
        exit 0
    }
    $diagnostics = Get-OwnedScheduledTaskDiagnostics $existing
    throw (
        "scheduled_task_identity_mismatch_refusing_overwrite:" +
        ($diagnostics | ConvertTo-Json -Compress -Depth 5)
    )
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register exact MTVCLC preservation task")) {
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -InputObject $definition `
        -ErrorAction Stop | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    if (-not (Test-OwnedScheduledTask $registered)) {
        $diagnostics = Get-OwnedScheduledTaskDiagnostics $registered
        throw (
            "registered_task_identity_verification_failed:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 5)
        )
    }
    Write-Output (
        "[mtvclc-preservation] task installed: {0}; {1} plus every {2} minute(s); preservation was not run" -f
        $TaskName,
        $TriggerMode,
        $PreservationIntervalMinutes
    )
}
