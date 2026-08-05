[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Install", "Preview", "Remove")]
    [string]$Action = "Install",

    [ValidatePattern("^[A-Za-z0-9_.-]{1,100}$")]
    [string]$TaskName = "TradingAgentMtvclcResilientCollector",

    [ValidateSet("AtLogOn", "AtStartup")]
    [string]$TriggerMode = "AtLogOn",

    [string]$PythonExe,
    [string]$Preregistration,
    [string]$OutputDir,
    [string]$ApiKeyFile,
    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(1, 60)]
    [int]$WatchdogIntervalMinutes = 5,

    [ValidateRange(10.0, 3600.0)]
    [double]$MaximumManifestAgeSeconds = 120.0,

    [ValidateRange(10.0, 3600.0)]
    [double]$StartupGraceSeconds = 180.0,

    [ValidateRange(10, 300)]
    [int]$StartConfirmationSeconds = 60
)

# AGENT: ROLE: explicit reversible Scheduled Task installer for the resilient MTVCLC watchdog.
# AGENT: HANDSHAKE: startup/repeating trigger -> exact-path ensure wrapper -> guarded Health/StartOrResume.
# AGENT: ISOLATION: stores only the API-key file path in task arguments; it never reads credential contents.
# AGENT: SIDE EFFECTS: Install/Remove mutates only the exact named Scheduled Task and never starts or stops collection.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

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

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$EnsurePath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\29_ensure_mtvclc_collector_resilient.ps1"
) "watchdog_wrapper_missing"
$WindowsPowerShell = Resolve-ExistingFile (
    Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
) "windows_powershell_missing"
$TaskDescriptionPrefix = (
    "FXSTACK_OWNER=mtvclc_resilient_collector_watchdog_v1; " +
    "collection-only; no evaluation or trading authority; trigger="
)
$TaskPath = "\"
$LogOnTaskDescription = $TaskDescriptionPrefix + "AtLogOn."
$StartupTaskDescription = $TaskDescriptionPrefix + "AtStartup."
$currentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$currentUserName = $currentIdentity.Name
$currentUserSid = $currentIdentity.User.Value

function Test-IsAdministrator {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-OwnedScheduledTaskDiagnostics {
    param([object]$Task)
    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        return [ordered]@{
            owned = $false
            action_count = $actions.Count
            reason = "action_count_mismatch"
        }
    }
    $expectedFileArgument = "-File " + (Quote-TaskArgument $EnsurePath)
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
    $description = [string]$Task.Description
    $atLogOnIdentity = (
        $description -eq $LogOnTaskDescription -and
        @("Interactive", "InteractiveToken") -contains [string]$Task.Principal.LogonType -and
        [string]$Task.Principal.RunLevel -eq "Limited"
    )
    $atStartupIdentity = (
        $description -eq $StartupTaskDescription -and
        [string]$Task.Principal.LogonType -eq "S4U" -and
        [string]$Task.Principal.RunLevel -eq "Highest"
    )
    $pathMatches = [string]$Task.TaskPath -eq $TaskPath
    $executeMatches = Test-SamePath ([string]$actions[0].Execute) $WindowsPowerShell
    $fileArgumentMatches = ([string]$actions[0].Arguments).Contains($expectedFileArgument)
    return [ordered]@{
        owned = (
            $pathMatches -and
            $executeMatches -and
            $fileArgumentMatches -and
            $userMatches -and
            ($atLogOnIdentity -or $atStartupIdentity)
        )
        action_count = 1
        task_path = [string]$Task.TaskPath
        task_path_matches = $pathMatches
        execute = [string]$actions[0].Execute
        execute_matches = $executeMatches
        file_argument_matches = $fileArgumentMatches
        description = $description
        at_logon_description_matches = ($description -eq $LogOnTaskDescription)
        at_startup_description_matches = ($description -eq $StartupTaskDescription)
        principal_user = $taskUser
        principal_user_sid = $taskUserSid
        principal_user_matches = $userMatches
        principal_logon_type = [string]$Task.Principal.LogonType
        principal_run_level = [string]$Task.Principal.RunLevel
        at_logon_identity_matches = $atLogOnIdentity
        at_startup_identity_matches = $atStartupIdentity
    }
}

function Test-OwnedScheduledTask {
    param([object]$Task)
    return [bool](Get-OwnedScheduledTaskDiagnostics $Task).owned
}

if ($Action -eq "Remove") {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output ("[mtvclc-watchdog] task already absent: {0}" -f $TaskName)
        exit 0
    }
    if (-not (Test-OwnedScheduledTask $existing)) {
        $diagnostics = Get-OwnedScheduledTaskDiagnostics $existing
        throw (
            "scheduled_task_identity_mismatch_refusing_remove:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 4)
        )
    }
    if (
        [string]$existing.Description -eq $StartupTaskDescription -and
        -not (Test-IsAdministrator)
    ) {
        throw "scheduled_task_mutation_requires_elevated_administrator"
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister resilient MTVCLC watchdog task")) {
        Unregister-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction Stop
        Write-Output ("[mtvclc-watchdog] task removed: {0}; running collection was not stopped" -f $TaskName)
    }
    exit 0
}

$ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
$ResolvedPreregistration = Resolve-ExistingFile $Preregistration "preregistration_file_invalid"
$ResolvedOutput = Resolve-ExistingDirectory $OutputDir "output_root_invalid"
$ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
$watchdogSha256 = Get-FileSha256 $EnsurePath
$guardPath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\27_guard_mtvclc_collector_resilient.ps1"
) "collector_guard_missing"
$guardSha256 = Get-FileSha256 $guardPath

$argumentParts = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-TaskArgument $EnsurePath),
    "-PythonExe", (Quote-TaskArgument $ResolvedPython),
    "-Preregistration", (Quote-TaskArgument $ResolvedPreregistration),
    "-OutputDir", (Quote-TaskArgument $ResolvedOutput),
    "-ApiKeyFile", (Quote-TaskArgument $ResolvedApiKeyFile),
    "-BaseUrl", (Quote-TaskArgument $BaseUrl),
    "-MaximumManifestAgeSeconds", ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $MaximumManifestAgeSeconds
    )),
    "-StartupGraceSeconds", ([string]::Format(
        [Globalization.CultureInfo]::InvariantCulture,
        "{0:R}",
        $StartupGraceSeconds
    )),
    "-StartConfirmationSeconds", ([string]$StartConfirmationSeconds),
    "-ExpectedWatchdogSha256", $watchdogSha256,
    "-ExpectedGuardSha256", $guardSha256
)
$taskAction = New-ScheduledTaskAction -Execute $WindowsPowerShell -Argument ($argumentParts -join " ")
$lifecycleTrigger = if ($TriggerMode -eq "AtStartup") {
    New-ScheduledTaskTrigger -AtStartup
}
else {
    New-ScheduledTaskTrigger -AtLogOn -User $currentUserName
}
$watchdogTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $WatchdogIntervalMinutes) `
    -RepetitionDuration (New-TimeSpan -Days 3650)
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$taskPrincipal = if ($TriggerMode -eq "AtStartup") {
    New-ScheduledTaskPrincipal -UserId $currentUserName -LogonType S4U -RunLevel Highest
}
else {
    New-ScheduledTaskPrincipal -UserId $currentUserName -LogonType Interactive -RunLevel Limited
}
$taskDescription = if ($TriggerMode -eq "AtStartup") {
    $StartupTaskDescription
}
else {
    $LogOnTaskDescription
}
$definition = New-ScheduledTask `
    -Action $taskAction `
    -Trigger @($lifecycleTrigger, $watchdogTrigger) `
    -Settings $settings `
    -Principal $taskPrincipal `
    -Description $taskDescription

if ($Action -eq "Preview") {
    Write-Output (@{
        schema_version = "fxstack.mtvclc_collector_watchdog_task_preview.resilient.v1"
        task_name = $TaskName
        task_path = $TaskPath
        execute = [string]$taskAction.Execute
        arguments = [string]$taskAction.Arguments
        trigger_mode = $TriggerMode
        startup_trigger = ($TriggerMode -eq "AtStartup")
        logon_trigger = ($TriggerMode -eq "AtLogOn")
        repetition_interval_minutes = $WatchdogIntervalMinutes
        repetition_duration_days = 3650
        multiple_instances = "IgnoreNew"
        execution_time_limit_seconds = 0
        start_confirmation_seconds = $StartConfirmationSeconds
        principal_user = $currentUserName
        principal_logon_type = if ($TriggerMode -eq "AtStartup") { "S4U" } else { "Interactive" }
        principal_run_level = if ($TriggerMode -eq "AtStartup") { "Highest" } else { "Limited" }
        watchdog_source_sha256 = $watchdogSha256
        guard_source_sha256 = $guardSha256
        collection_only = $true
        evaluation_performed = $false
        authority_granted = $false
        runtime_authorized = $false
        broker_access_authorized = $false
        order_authorized = $false
        mutation_performed = $false
    } | ConvertTo-Json -Compress -Depth 6)
    exit 0
}

if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
    throw "scheduled_task_mutation_requires_elevated_administrator"
}
$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "scheduled_task_already_exists_remove_it_explicitly_before_install"
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register resilient MTVCLC watchdog task")) {
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -InputObject $definition `
        -ErrorAction Stop | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    if (-not (Test-OwnedScheduledTask $registered)) {
        $diagnostics = Get-OwnedScheduledTaskDiagnostics $registered
        Unregister-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction Stop
        throw (
            "registered_task_identity_verification_failed_and_removed:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 4)
        )
    }
    Write-Output (
        "[mtvclc-watchdog] task installed: {0}; {1} plus every {2} minute(s); collection was not restarted" -f
        $TaskName,
        $TriggerMode,
        $WatchdogIntervalMinutes
    )
}
