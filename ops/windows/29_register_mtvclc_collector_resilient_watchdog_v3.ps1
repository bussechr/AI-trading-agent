[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Install", "Preview", "Start", "Remove")]
    [string]$Action = "Preview",

    [ValidatePattern("^[A-Za-z0-9_.-]{1,100}$")]
    [string]$TaskName = "TradingAgentMtvclcV5Collector",

    [ValidateSet("AtLogOn", "AtStartup")]
    [string]$TriggerMode = "AtLogOn",

    [string]$PythonExe,
    [string]$Preregistration,
    [string]$OutputDir,
    [string]$ApiKeyFile,
    [string]$BridgeEaRepositorySource,
    [string]$BridgeEaDeployedSource,
    [string]$BridgeEaDeployedEx4,

    [ValidatePattern("^$|^[0-9a-fA-F]{64}$")]
    [string]$ExpectedCollectorSha256 = "",

    [ValidatePattern("^$|^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationArtifactSha256 = "",

    [ValidatePattern("^$|^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationBodySha256 = "",

    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(1, 60)]
    [int]$WatchdogIntervalMinutes = 5,

    [ValidateRange(10.0, 3600.0)]
    [double]$MaximumManifestAgeSeconds = 120.0,

    [ValidateRange(10.0, 3600.0)]
    [double]$StartupGraceSeconds = 180.0,

    [ValidateRange(30, 300)]
    [int]$StartConfirmationSeconds = 60
)

# AGENT: ROLE: reversible Scheduled Task definition and full-lifetime start boundary for the v5 collector watchdog.
# AGENT: HANDSHAKE: exact task action pins supervisor, adapter/template, producer software, preregistration, and output identity; Start proves an absent writer, observes one new Scheduler instance, and health-confirms its locked descendant writer.
# AGENT: ISOLATION: stores only the API-key file path and has no evaluation, runtime, or trade path.
# AGENT: SIDE EFFECTS: Install/Remove mutates only the exact distinct task; Start may resume collection only through that already-installed full-lifetime task and never starts runtime or trading.

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
            [IO.Path]::GetFullPath($Left),
            [IO.Path]::GetFullPath($Right),
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }
}

if (-not ("FxStackV5TaskNativeCommandLine" -as [type])) {
    Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;

public static class FxStackV5TaskNativeCommandLine
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

function Get-ScheduledTaskRunningInstances {
    $service = $null
    $folder = $null
    $registeredTask = $null
    $collection = $null
    try {
        $service = New-Object -ComObject "Schedule.Service"
        $service.Connect()
        $folder = $service.GetFolder($TaskPath)
        $registeredTask = $folder.GetTask($TaskName)
        $collection = $registeredTask.GetInstances(0)
        $result = @()
        for ($index = 1; $index -le [int]$collection.Count; $index++) {
            $instance = $collection.Item($index)
            try {
                $result += [pscustomobject]@{
                    InstanceGuid = ([string]$instance.InstanceGuid).ToLowerInvariant()
                    EnginePid = [int]$instance.EnginePID
                    State = [int]$instance.State
                    CurrentAction = [string]$instance.CurrentAction
                }
            }
            finally {
                if ($null -ne $instance -and [Runtime.InteropServices.Marshal]::IsComObject($instance)) {
                    $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($instance)
                }
            }
        }
        return @($result)
    }
    finally {
        if ($null -ne $collection -and [Runtime.InteropServices.Marshal]::IsComObject($collection)) {
            $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($collection)
        }
        if ($null -ne $registeredTask -and [Runtime.InteropServices.Marshal]::IsComObject($registeredTask)) {
            $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($registeredTask)
        }
        if ($null -ne $folder -and [Runtime.InteropServices.Marshal]::IsComObject($folder)) {
            $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($folder)
        }
        if ($null -ne $service -and [Runtime.InteropServices.Marshal]::IsComObject($service)) {
            $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($service)
        }
    }
}

function Get-ProcessSnapshotById {
    $map = @{}
    foreach ($process in @(Get-CimInstance Win32_Process -ErrorAction Stop)) {
        $map[[int]$process.ProcessId] = $process
    }
    return ,$map
}

function Get-ProcessCreationUtc {
    param([object]$Process)
    $value = $Process.CreationDate
    if ($value -is [DateTime]) {
        return ([DateTime]$value).ToUniversalTime()
    }
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime(
            [string]$value
        ).ToUniversalTime()
    }
    catch {
        throw "task_process_creation_time_invalid"
    }
}

function Test-ExactTaskActionProcess {
    param(
        [object]$Process,
        [string[]]$ExpectedArguments,
        [DateTime]$NotBeforeUtc
    )
    $commandLine = [string]$Process.CommandLine
    if ([string]::IsNullOrWhiteSpace($commandLine)) {
        return $false
    }
    try {
        [string[]]$arguments = [FxStackV5TaskNativeCommandLine]::Split(
            $commandLine
        )
    }
    catch {
        return $false
    }
    if (
        $arguments.Count -ne $ExpectedArguments.Count + 1 -or
        -not (Test-SamePath $arguments[0] $WindowsPowerShell)
    ) {
        return $false
    }
    for ($index = 0; $index -lt $ExpectedArguments.Count; $index++) {
        if (-not [string]::Equals(
            [string]$arguments[$index + 1],
            [string]$ExpectedArguments[$index],
            [StringComparison]::Ordinal
        )) {
            return $false
        }
    }
    try {
        return (Get-ProcessCreationUtc $Process) -ge $NotBeforeUtc
    }
    catch {
        return $false
    }
}

function Test-ProcessDescendsFrom {
    param(
        [int]$ProcessId,
        [int]$AncestorProcessId,
        [hashtable]$ProcessMap
    )
    $cursor = $ProcessId
    $seen = @{}
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($cursor -eq $AncestorProcessId) {
            return $true
        }
        if ($cursor -le 0 -or $seen.ContainsKey($cursor) -or -not $ProcessMap.ContainsKey($cursor)) {
            return $false
        }
        $seen[$cursor] = $true
        $cursor = [int]$ProcessMap[$cursor].ParentProcessId
    }
    return $false
}

function Get-ExactGuardAncestorPid {
    param(
        [int]$WriterProcessId,
        [int]$TaskActionProcessId,
        [hashtable]$ProcessMap,
        [string[]]$ExpectedGuardArguments,
        [DateTime]$NotBeforeUtc
    )
    $cursor = $WriterProcessId
    $seen = @{}
    for ($depth = 0; $depth -lt 32; $depth++) {
        if ($cursor -le 0 -or $seen.ContainsKey($cursor) -or -not $ProcessMap.ContainsKey($cursor)) {
            return 0
        }
        if ($cursor -eq $TaskActionProcessId) {
            return 0
        }
        $seen[$cursor] = $true
        $process = $ProcessMap[$cursor]
        $commandLine = [string]$process.CommandLine
        if (-not [string]::IsNullOrWhiteSpace($commandLine)) {
            try {
                [string[]]$arguments = [FxStackV5TaskNativeCommandLine]::Split(
                    $commandLine
                )
                $argumentsMatch = (
                    $arguments.Count -eq $ExpectedGuardArguments.Count + 1 -and
                    (Test-SamePath $arguments[0] $WindowsPowerShell)
                )
                for (
                    $index = 0;
                    $argumentsMatch -and $index -lt $ExpectedGuardArguments.Count;
                    $index++
                ) {
                    $argumentsMatch = [string]::Equals(
                        [string]$arguments[$index + 1],
                        [string]$ExpectedGuardArguments[$index],
                        [StringComparison]::Ordinal
                    )
                }
                if (
                    $argumentsMatch -and
                    (Get-ProcessCreationUtc $process) -ge $NotBeforeUtc
                ) {
                    return [int]$process.ProcessId
                }
            }
            catch {
                return 0
            }
        }
        $cursor = [int]$process.ParentProcessId
    }
    return 0
}

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$EnsurePath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\29_ensure_mtvclc_collector_resilient_v3.ps1")
)
$GuardPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\27_guard_mtvclc_collector_resilient_v3.ps1")
)
$CollectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\capture_ig_mt4_m1_activity_resilient_v4.py")
)
$CollectorTemplatePath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\capture_ig_mt4_m1_activity_resilient_v3.py")
)
$BridgeEaRepositoryPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "MQL4\Experts\BridgeEA.mq4")
)
$InspectorPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\check_mt4_tick_volume_collector_continuity_resilient_v3.py")
)
$ContinuityCorePath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "tools\check_mt4_tick_volume_collector_continuity_resilient.py")
)
$WindowsPowerShell = [IO.Path]::GetFullPath(
    (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe")
)
$TaskDescriptionPrefix = (
    "FXSTACK_OWNER=mtvclc_gap_v5_collector_watchdog_v1; " +
    "collection-only; no evaluation, runtime, or trading authority; trigger="
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
    $actionArguments = [string]$actions[0].Arguments
    $fileArgumentMatches = $actionArguments.Contains($expectedFileArgument)
    $pinnedTupleArgumentsPresent = $true
    foreach ($requiredOption in @(
        "-ExpectedCollectorSha256",
        "-ExpectedCollectorTemplateSha256",
        "-ExpectedInspectorSha256",
        "-ExpectedContinuityCoreSha256",
        "-ExpectedPreregistrationArtifactSha256",
        "-ExpectedPreregistrationBodySha256",
        "-ExpectedWatchdogSha256",
        "-ExpectedGuardSha256",
        "-BridgeEaRepositorySource",
        "-BridgeEaDeployedSource",
        "-BridgeEaDeployedEx4"
    )) {
        if (-not $actionArguments.Contains($requiredOption)) {
            $pinnedTupleArgumentsPresent = $false
        }
    }
    return [ordered]@{
        owned = (
            $pathMatches -and
            $executeMatches -and
            $fileArgumentMatches -and
            $pinnedTupleArgumentsPresent -and
            $userMatches -and
            ($atLogOnIdentity -or $atStartupIdentity)
        )
        action_count = 1
        task_path = [string]$Task.TaskPath
        task_path_matches = $pathMatches
        execute = [string]$actions[0].Execute
        execute_matches = $executeMatches
        file_argument_matches = $fileArgumentMatches
        pinned_tuple_arguments_present = $pinnedTupleArgumentsPresent
        description = $description
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

function Get-ExactScheduledTaskDiagnostics {
    param(
        [object]$Task,
        [string]$ExpectedArguments,
        [string]$ExpectedDescription
    )

    $ownership = Get-OwnedScheduledTaskDiagnostics $Task
    $actions = @($Task.Actions)
    $triggers = @($Task.Triggers)
    $settings = $Task.Settings
    $argumentsMatch = (
        $actions.Count -eq 1 -and
        [string]::Equals(
            [string]$actions[0].Arguments,
            $ExpectedArguments,
            [StringComparison]::Ordinal
        )
    )
    $descriptionMatches = (
        [string]$Task.Description -eq $ExpectedDescription
    )
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
        $triggers.Count -eq 2 -and
        $lifecycleTriggers.Count -eq 1 -and
        $repetitionTriggers.Count -eq 1 -and
        [bool]$lifecycleTriggers[0].Enabled -and
        [bool]$repetitionTriggers[0].Enabled
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
                $taskInterval -eq (New-TimeSpan -Minutes $WatchdogIntervalMinutes)
            )
            $repetitionDurationMatches = (
                $taskDuration -eq (New-TimeSpan -Days 3650)
            )
        }
        catch {
            $repetitionIntervalMatches = $false
            $repetitionDurationMatches = $false
        }
    }
    $settingsExact = (
        [bool]$settings.Enabled -and
        [bool]$settings.AllowDemandStart -and
        [bool]$settings.StartWhenAvailable -and
        [string]$settings.ExecutionTimeLimit -eq "PT0S" -and
        -not [bool]$settings.DisallowStartIfOnBatteries -and
        -not [bool]$settings.StopIfGoingOnBatteries -and
        @("IgnoreNew", "2") -contains [string]$settings.MultipleInstances
    )
    $exact = (
        [bool]$ownership.owned -and
        $argumentsMatch -and
        $descriptionMatches -and
        $triggerTopologyMatches -and
        $logOnTriggerUserMatches -and
        $repetitionIntervalMatches -and
        $repetitionDurationMatches -and
        $settingsExact
    )
    return [ordered]@{
        exact = $exact
        owned = [bool]$ownership.owned
        ownership = $ownership
        arguments_match = $argumentsMatch
        description_matches = $descriptionMatches
        trigger_count = $triggers.Count
        trigger_topology_matches = $triggerTopologyMatches
        logon_trigger_user_matches = $logOnTriggerUserMatches
        repetition_interval_matches = $repetitionIntervalMatches
        repetition_duration_matches = $repetitionDurationMatches
        settings_exact = $settingsExact
        task_enabled = [bool]$settings.Enabled
        allow_demand_start = [bool]$settings.AllowDemandStart
        start_when_available = [bool]$settings.StartWhenAvailable
        execution_time_limit = [string]$settings.ExecutionTimeLimit
        multiple_instances = [string]$settings.MultipleInstances
    }
}

if (
    [string]::IsNullOrWhiteSpace($ExpectedCollectorSha256) -or
    [string]::IsNullOrWhiteSpace($ExpectedPreregistrationArtifactSha256) -or
    [string]::IsNullOrWhiteSpace($ExpectedPreregistrationBodySha256) -or
    [string]::IsNullOrWhiteSpace($BridgeEaRepositorySource) -or
    [string]::IsNullOrWhiteSpace($BridgeEaDeployedSource) -or
    [string]::IsNullOrWhiteSpace($BridgeEaDeployedEx4)
) {
    throw "exact_collector_preregistration_and_producer_identity_required"
}
$EnsurePath = Resolve-ExistingFile $EnsurePath "watchdog_wrapper_missing"
$GuardPath = Resolve-ExistingFile $GuardPath "collector_guard_missing"
$CollectorPath = Resolve-ExistingFile $CollectorPath "collector_source_missing"
$CollectorTemplatePath = Resolve-ExistingFile $CollectorTemplatePath "collector_template_source_missing"
$BridgeEaRepositoryPath = Resolve-ExistingFile $BridgeEaRepositoryPath "bridge_ea_repository_source_missing"
$InspectorPath = Resolve-ExistingFile $InspectorPath "continuity_inspector_missing"
$ContinuityCorePath = Resolve-ExistingFile $ContinuityCorePath "continuity_core_missing"
$WindowsPowerShell = Resolve-ExistingFile $WindowsPowerShell "windows_powershell_missing"
$ExpectedCollectorSha256 = $ExpectedCollectorSha256.ToLowerInvariant()
$ExpectedPreregistrationArtifactSha256 = (
    $ExpectedPreregistrationArtifactSha256.ToLowerInvariant()
)
$ExpectedPreregistrationBodySha256 = (
    $ExpectedPreregistrationBodySha256.ToLowerInvariant()
)

$ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
$ResolvedPreregistration = Resolve-ExistingFile $Preregistration "preregistration_file_invalid"
$ResolvedOutput = Resolve-ExistingDirectory $OutputDir "output_root_invalid"
$ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
$ResolvedBridgeEaRepositorySource = Resolve-ExistingFile $BridgeEaRepositorySource "bridge_ea_repository_source_invalid"
$ResolvedBridgeEaDeployedSource = Resolve-ExistingFile $BridgeEaDeployedSource "bridge_ea_deployed_source_invalid"
$ResolvedBridgeEaDeployedEx4 = Resolve-ExistingFile $BridgeEaDeployedEx4 "bridge_ea_deployed_ex4_invalid"
if (-not (Test-SamePath $ResolvedBridgeEaRepositorySource $BridgeEaRepositoryPath)) {
    throw "bridge_ea_repository_source_path_mismatch"
}

$actualCollectorSha256 = Get-FileSha256 $CollectorPath
$collectorTemplateSha256 = Get-FileSha256 $CollectorTemplatePath
$actualPreregistrationSha256 = Get-FileSha256 $ResolvedPreregistration
if ($actualCollectorSha256 -ne $ExpectedCollectorSha256) {
    throw "collector_source_identity_mismatch"
}
if ($actualPreregistrationSha256 -ne $ExpectedPreregistrationArtifactSha256) {
    throw "preregistration_artifact_identity_mismatch"
}
try {
    $preregistrationBytes = [IO.File]::ReadAllBytes($ResolvedPreregistration)
    $preregistrationJson = [Text.Encoding]::UTF8.GetString($preregistrationBytes) |
        ConvertFrom-Json -ErrorAction Stop
    $declaredBodySha256 = (
        [string]$preregistrationJson.preregistration_body_sha256
    ).ToLowerInvariant()
}
catch {
    throw "preregistration_body_identity_unreadable"
}
if ($declaredBodySha256 -ne $ExpectedPreregistrationBodySha256) {
    throw "preregistration_body_identity_mismatch"
}
$repositoryIdentity = $preregistrationJson.source_identities.'production_engine_component:MQL4/Experts/BridgeEA.mq4'
$deployedSourceIdentity = $preregistrationJson.source_identities.bridge_ea_deployed_source
$deployedEx4Identity = $preregistrationJson.source_identities.bridge_ea_deployed_ex4
$actualRepositorySha256 = Get-FileSha256 $ResolvedBridgeEaRepositorySource
$actualDeployedSourceSha256 = Get-FileSha256 $ResolvedBridgeEaDeployedSource
$actualDeployedEx4Sha256 = Get-FileSha256 $ResolvedBridgeEaDeployedEx4
$repositoryLength = (Get-Item -LiteralPath $ResolvedBridgeEaRepositorySource -Force).Length
$deployedSourceLength = (Get-Item -LiteralPath $ResolvedBridgeEaDeployedSource -Force).Length
$deployedEx4Length = (Get-Item -LiteralPath $ResolvedBridgeEaDeployedEx4 -Force).Length
if (
    [string]$repositoryIdentity.filename -ne "BridgeEA.mq4" -or
    [string]$repositoryIdentity.sha256 -ne $actualRepositorySha256 -or
    [int64]$repositoryIdentity.size_bytes -ne [int64]$repositoryLength -or
    [string]$deployedSourceIdentity.filename -ne "BridgeEA.mq4" -or
    [string]$deployedSourceIdentity.sha256 -ne $actualDeployedSourceSha256 -or
    [int64]$deployedSourceIdentity.size_bytes -ne [int64]$deployedSourceLength -or
    [string]$deployedEx4Identity.filename -ne "BridgeEA.ex4" -or
    [string]$deployedEx4Identity.sha256 -ne $actualDeployedEx4Sha256 -or
    [int64]$deployedEx4Identity.size_bytes -ne [int64]$deployedEx4Length -or
    $actualRepositorySha256 -ne $actualDeployedSourceSha256 -or
    [int64]$repositoryLength -ne [int64]$deployedSourceLength
) {
    throw "bridge_ea_producer_identity_mismatch"
}

$watchdogSha256 = Get-FileSha256 $EnsurePath
$guardSha256 = Get-FileSha256 $GuardPath
$inspectorSha256 = Get-FileSha256 $InspectorPath
$continuityCoreSha256 = Get-FileSha256 $ContinuityCorePath

$argumentParts = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-TaskArgument $EnsurePath),
    "-PythonExe", (Quote-TaskArgument $ResolvedPython),
    "-Preregistration", (Quote-TaskArgument $ResolvedPreregistration),
    "-OutputDir", (Quote-TaskArgument $ResolvedOutput),
    "-ApiKeyFile", (Quote-TaskArgument $ResolvedApiKeyFile),
    "-BridgeEaRepositorySource", (Quote-TaskArgument $ResolvedBridgeEaRepositorySource),
    "-BridgeEaDeployedSource", (Quote-TaskArgument $ResolvedBridgeEaDeployedSource),
    "-BridgeEaDeployedEx4", (Quote-TaskArgument $ResolvedBridgeEaDeployedEx4),
    "-ExpectedCollectorSha256", $ExpectedCollectorSha256,
    "-ExpectedCollectorTemplateSha256", $collectorTemplateSha256,
    "-ExpectedInspectorSha256", $inspectorSha256,
    "-ExpectedContinuityCoreSha256", $continuityCoreSha256,
    "-ExpectedPreregistrationArtifactSha256", $ExpectedPreregistrationArtifactSha256,
    "-ExpectedPreregistrationBodySha256", $ExpectedPreregistrationBodySha256,
    "-ExpectedWatchdogSha256", $watchdogSha256,
    "-ExpectedGuardSha256", $guardSha256,
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
    ))
)
$expectedTaskArguments = $argumentParts -join " "
$parsedTaskActionCommandLine = [FxStackV5TaskNativeCommandLine]::Split(
    (Quote-TaskArgument $WindowsPowerShell) + " " + $expectedTaskArguments
)
$expectedTaskProcessArguments = @(
    $parsedTaskActionCommandLine | Select-Object -Skip 1
)
$taskAction = New-ScheduledTaskAction -Execute $WindowsPowerShell -Argument $expectedTaskArguments
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

function Write-StartStatusAndExit {
    param([hashtable]$Payload, [int]$Code)
    $Payload["schema_version"] = "fxstack.mtvclc_collector_task_start.gap_v5.v1"
    $Payload["task_name"] = $TaskName
    $Payload["task_path"] = $TaskPath
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

function Write-RemoveStatusAndExit {
    param([hashtable]$Payload, [int]$Code)
    $Payload["schema_version"] = "fxstack.mtvclc_collector_task_remove.gap_v5.v1"
    $Payload["task_name"] = $TaskName
    $Payload["task_path"] = $TaskPath
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

function Assert-DurableLaunchSourceIdentity {
    if ((Get-FileSha256 $EnsurePath) -ne $watchdogSha256) {
        throw "watchdog_source_identity_drift"
    }
    if ((Get-FileSha256 $GuardPath) -ne $guardSha256) {
        throw "guard_source_identity_drift"
    }
    if ((Get-FileSha256 $CollectorPath) -ne $ExpectedCollectorSha256) {
        throw "collector_source_identity_drift"
    }
    if ((Get-FileSha256 $CollectorTemplatePath) -ne $collectorTemplateSha256) {
        throw "collector_template_source_identity_drift"
    }
    if ((Get-FileSha256 $InspectorPath) -ne $inspectorSha256) {
        throw "continuity_inspector_source_identity_drift"
    }
    if ((Get-FileSha256 $ContinuityCorePath) -ne $continuityCoreSha256) {
        throw "continuity_core_source_identity_drift"
    }
    if (
        (Get-FileSha256 $ResolvedPreregistration) -ne
        $ExpectedPreregistrationArtifactSha256
    ) {
        throw "preregistration_artifact_identity_drift"
    }
    if ((Get-FileSha256 $ResolvedBridgeEaRepositorySource) -ne $actualRepositorySha256) {
        throw "bridge_ea_repository_source_identity_drift"
    }
    if ((Get-FileSha256 $ResolvedBridgeEaDeployedSource) -ne $actualDeployedSourceSha256) {
        throw "bridge_ea_deployed_source_identity_drift"
    }
    if ((Get-FileSha256 $ResolvedBridgeEaDeployedEx4) -ne $actualDeployedEx4Sha256) {
        throw "bridge_ea_deployed_ex4_identity_drift"
    }
}

function New-DirectGuardArguments {
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
        "-ExpectedCollectorTemplateSha256", $collectorTemplateSha256,
        "-ExpectedInspectorSha256", $inspectorSha256,
        "-ExpectedContinuityCoreSha256", $continuityCoreSha256,
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

function Invoke-DirectGuardHealth {
    Assert-DurableLaunchSourceIdentity
    $healthArguments = @(New-DirectGuardArguments "Health")
    $raw = @(& $WindowsPowerShell @healthArguments 2>&1)
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

function Test-GuardAuthorityEnvelope {
    param([object]$Report)
    return (
        [string]$Report.schema_version -eq "fxstack.mtvclc_collector_guard_status.gap_v5.v1" -and
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
        [string]$Report.collector_template_source_sha256 -eq $collectorTemplateSha256 -and
        [string]$Report.continuity_inspector_source_sha256 -eq $inspectorSha256 -and
        [string]$Report.continuity_core_source_sha256 -eq $continuityCoreSha256 -and
        [string]$Report.preregistration_artifact_sha256 -eq $ExpectedPreregistrationArtifactSha256 -and
        [string]$Report.preregistration_body_sha256 -eq $ExpectedPreregistrationBodySha256 -and
        (Test-SamePath ([string]$Report.bridge_ea_repository_source_path) $ResolvedBridgeEaRepositorySource) -and
        [string]$Report.bridge_ea_repository_source_identity.sha256 -eq $actualRepositorySha256 -and
        (Test-SamePath ([string]$Report.bridge_ea_deployed_source_path) $ResolvedBridgeEaDeployedSource) -and
        [string]$Report.bridge_ea_deployed_source_identity.sha256 -eq $actualDeployedSourceSha256 -and
        (Test-SamePath ([string]$Report.bridge_ea_deployed_ex4_path) $ResolvedBridgeEaDeployedEx4) -and
        [string]$Report.bridge_ea_deployed_ex4_identity.sha256 -eq $actualDeployedEx4Sha256 -and
        (Test-SamePath ([string]$Report.output_root) $ResolvedOutput)
    )
}

function Test-HealthyLockedWriter {
    param([object]$Health)
    return (
        [int]$Health.ExitCode -eq 0 -and
        @("starting", "waiting_for_sealed_t0", "healthy") -contains [string]$Health.Report.status -and
        [int]$Health.Report.writer_group_count -eq 1 -and
        [string]$Health.Report.supervisor_lock_state -eq "held" -and
        $Health.Report.supervisor_lock_held -eq $true
    )
}

if ($Action -eq "Preview") {
    Write-Output (@{
        schema_version = "fxstack.mtvclc_collector_watchdog_task_preview.gap_v5.v1"
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
        collector_source_sha256 = $ExpectedCollectorSha256
        collector_template_source_path = $CollectorTemplatePath
        collector_template_source_sha256 = $collectorTemplateSha256
        preregistration_artifact_sha256 = $ExpectedPreregistrationArtifactSha256
        preregistration_body_sha256 = $ExpectedPreregistrationBodySha256
        watchdog_source_sha256 = $watchdogSha256
        guard_source_sha256 = $guardSha256
        continuity_inspector_source_sha256 = $inspectorSha256
        continuity_core_source_sha256 = $continuityCoreSha256
        upstream_producer_software_body_sha256 = [string]$preregistrationJson.upstream_producer_software.producer_software_body_sha256
        bridge_ea_repository_source_path = $ResolvedBridgeEaRepositorySource
        bridge_ea_repository_source_sha256 = $actualRepositorySha256
        bridge_ea_deployed_source_path = $ResolvedBridgeEaDeployedSource
        bridge_ea_deployed_source_sha256 = $actualDeployedSourceSha256
        bridge_ea_deployed_ex4_path = $ResolvedBridgeEaDeployedEx4
        bridge_ea_deployed_ex4_sha256 = $actualDeployedEx4Sha256
        output_root = $ResolvedOutput
        collection_only = $true
        evaluation_performed = $false
        authority_granted = $false
        runtime_authorized = $false
        broker_access_authorized = $false
        order_authorized = $false
        immediate_market_trade_authorized = $false
        mutation_performed = $false
    } | ConvertTo-Json -Compress -Depth 6)
    exit 0
}

if ($Action -eq "Remove") {
    $existing = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-RemoveStatusAndExit @{
            status = "no_action_required"
            reason = "scheduled_task_absent"
            mutation_performed = $false
        } 0
    }
    $taskDiagnostics = Get-ExactScheduledTaskDiagnostics `
        $existing `
        $expectedTaskArguments `
        $taskDescription
    if (-not [bool]$taskDiagnostics.exact) {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "scheduled_task_exact_identity_mismatch_refusing_remove"
            task_diagnostics = $taskDiagnostics
            mutation_performed = $false
        } 4
    }
    try {
        $runningInstances = @(Get-ScheduledTaskRunningInstances)
    }
    catch {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "scheduled_task_instance_query_failed"
            mutation_performed = $false
        } 4
    }
    if ([string]$existing.State -ne "Ready" -or $runningInstances.Count -ne 0) {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "scheduled_task_running_or_not_ready"
            scheduled_task_state = [string]$existing.State
            scheduled_task_running_instance_count = $runningInstances.Count
            mutation_performed = $false
        } 4
    }
    try {
        $removeHealth = Invoke-DirectGuardHealth
        if (-not (Test-GuardAuthorityEnvelope $removeHealth.Report)) {
            throw "guard_health_authority_or_schema_mismatch"
        }
        if (-not (Test-ExactGuardIdentity $removeHealth.Report)) {
            throw "guard_health_identity_mismatch"
        }
    }
    catch {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = $_.Exception.Message
            mutation_performed = $false
        } 4
    }
    $removeWriterCount = [int]$removeHealth.Report.writer_group_count
    $removeLockState = [string]$removeHealth.Report.supervisor_lock_state
    if ($removeWriterCount -gt 0 -or $removeLockState -eq "held") {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "orphaned_writer_not_task_owned"
            writer_group_count = $removeWriterCount
            supervisor_lock_state = $removeLockState
            scheduled_task_state = [string]$existing.State
            mutation_performed = $false
        } 4
    }
    if (
        @((0), (3)) -notcontains [int]$removeHealth.ExitCode -or
        @("absent", "available") -notcontains $removeLockState
    ) {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "guard_health_did_not_prove_safe_stopped_state"
            guard_exit_code = [int]$removeHealth.ExitCode
            guard_status = [string]$removeHealth.Report.status
            writer_group_count = $removeWriterCount
            supervisor_lock_state = $removeLockState
            mutation_performed = $false
        } 4
    }
    if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
        Write-RemoveStatusAndExit @{
            status = "task_remove_refused"
            reason = "scheduled_task_mutation_requires_elevated_administrator"
            mutation_performed = $false
        } 4
    }
    if (-not $PSCmdlet.ShouldProcess($TaskName, "Unregister exact stopped gap-v5 collector task")) {
        Write-RemoveStatusAndExit @{
            status = "task_remove_previewed"
            reason = "should_process_declined"
            mutation_performed = $false
        } 0
    }
    Unregister-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -Confirm:$false `
        -ErrorAction Stop
    Write-RemoveStatusAndExit @{
        status = "task_removed"
        reason = "exact_stopped_task_unregistered"
        mutation_performed = $true
    } 0
}

if ($Action -eq "Start") {
    $existing = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "scheduled_task_absent_refusing_start"
            mutation_performed = $false
        } 4
    }
    $taskDiagnostics = Get-ExactScheduledTaskDiagnostics `
        $existing `
        $expectedTaskArguments `
        $taskDescription
    if (-not [bool]$taskDiagnostics.exact) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "scheduled_task_exact_identity_mismatch_refusing_start"
            task_diagnostics = $taskDiagnostics
            mutation_performed = $false
        } 4
    }
    if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "scheduled_task_mutation_requires_elevated_administrator"
            mutation_performed = $false
        } 4
    }
    try {
        $preStartInstances = @(Get-ScheduledTaskRunningInstances)
        $preflight = Invoke-DirectGuardHealth
        if (-not (Test-GuardAuthorityEnvelope $preflight.Report)) {
            throw "guard_health_authority_or_schema_mismatch"
        }
        if (-not (Test-ExactGuardIdentity $preflight.Report)) {
            throw "guard_health_identity_mismatch"
        }
    }
    catch {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = $_.Exception.Message
            mutation_performed = $false
        } 4
    }
    $preWriterCount = [int]$preflight.Report.writer_group_count
    $preLockState = [string]$preflight.Report.supervisor_lock_state
    if ($preWriterCount -gt 0 -or $preLockState -eq "held") {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "orphaned_writer_not_task_owned"
            scheduled_task_state = [string]$existing.State
            scheduled_task_running_instance_count = $preStartInstances.Count
            guard_status = [string]$preflight.Report.status
            writer_group_count = $preWriterCount
            supervisor_lock_state = $preLockState
            task_scheduler_start_requested = $false
            mutation_performed = $false
        } 4
    }
    if ([string]$existing.State -ne "Ready" -or $preStartInstances.Count -ne 0) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "scheduled_task_must_be_ready_with_zero_running_instances"
            scheduled_task_state = [string]$existing.State
            scheduled_task_running_instance_count = $preStartInstances.Count
            writer_group_count = $preWriterCount
            task_scheduler_start_requested = $false
            mutation_performed = $false
        } 4
    }
    if (
        [int]$preflight.ExitCode -eq 0 -and
        [string]$preflight.Report.status -eq "prospective_window_complete" -and
        [int]$preflight.Report.writer_group_count -eq 0
    ) {
        Write-StartStatusAndExit @{
            status = "no_action_required"
            reason = "prospective_window_complete"
            guard_status = [string]$preflight.Report.status
            writer_group_count = 0
            task_scheduler_start_requested = $false
            mutation_performed = $false
        } 0
    }
    $endEpoch = 0.0
    try {
        $endEpoch = [double]$preflight.Report.prospective_end_epoch_exclusive
    }
    catch {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "prospective_end_invalid"
            mutation_performed = $false
        } 4
    }
    $restartAllowed = (
        [int]$preflight.ExitCode -eq 3 -and
        @("stopped_before_t0", "stopped_during_window") -contains [string]$preflight.Report.status -and
        [string]$preflight.Report.reason -eq "collector_writer_absent" -and
        [int]$preflight.Report.writer_group_count -eq 0 -and
        $preflight.Report.supervisor_lock_held -eq $false -and
        @("absent", "available") -contains [string]$preflight.Report.supervisor_lock_state -and
        -not [double]::IsNaN($endEpoch) -and
        -not [double]::IsInfinity($endEpoch) -and
        $endEpoch -gt [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    )
    if (-not $restartAllowed) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "guard_health_did_not_prove_restartable_absent_writer"
            guard_exit_code = [int]$preflight.ExitCode
            guard_status = [string]$preflight.Report.status
            writer_group_count = [int]$preflight.Report.writer_group_count
            supervisor_lock_state = [string]$preflight.Report.supervisor_lock_state
            mutation_performed = $false
        } 4
    }
    $preStartTaskInfo = Get-ScheduledTaskInfo `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $preStartTaskInfo) {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "scheduled_task_prestart_run_info_unavailable"
            mutation_performed = $false
        } 4
    }
    $preStartLastRunTime = [DateTime]$preStartTaskInfo.LastRunTime
    if (-not $PSCmdlet.ShouldProcess(
        $TaskName,
        "Start exact full-lifetime collection-only gap-v5 task through Task Scheduler"
    )) {
        Write-StartStatusAndExit @{
            status = "task_start_previewed"
            reason = "should_process_declined"
            task_scheduler_start_requested = $false
            mutation_performed = $false
        } 0
    }
    [DateTime]$launchRequestUtc = [DateTime]::UtcNow
    try {
        Start-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction Stop
    }
    catch {
        Write-StartStatusAndExit @{
            status = "task_start_refused"
            reason = "task_scheduler_start_failed"
            task_scheduler_start_requested = $true
            mutation_performed = $false
        } 4
    }

    $confirmationDeadline = [DateTimeOffset]::UtcNow.AddSeconds(
        $StartConfirmationSeconds
    )
    $lastGuardStatus = "not_observed"
    $firstOwnedSample = $null
    $expectedGuardStartArguments = @(New-DirectGuardArguments "StartOrResume")
    while ([DateTimeOffset]::UtcNow -lt $confirmationDeadline) {
        Start-Sleep -Seconds 1
        try {
            $confirmation = Invoke-DirectGuardHealth
            if (-not (Test-GuardAuthorityEnvelope $confirmation.Report)) {
                throw "guard_confirmation_authority_or_schema_mismatch"
            }
            if (-not (Test-ExactGuardIdentity $confirmation.Report)) {
                throw "guard_confirmation_identity_mismatch"
            }
        }
        catch {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = $_.Exception.Message
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $lastGuardStatus = [string]$confirmation.Report.status
        $confirmedTask = Get-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $confirmedTask) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "scheduled_task_disappeared_during_confirmation"
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $confirmedDiagnostics = Get-ExactScheduledTaskDiagnostics `
            $confirmedTask `
            $expectedTaskArguments `
            $taskDescription
        if (-not [bool]$confirmedDiagnostics.exact) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "scheduled_task_identity_drift_during_confirmation"
                task_diagnostics = $confirmedDiagnostics
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $confirmedTaskInfo = Get-ScheduledTaskInfo `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
        if ($null -eq $confirmedTaskInfo) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "scheduled_task_run_info_unavailable"
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        try {
            $confirmedInstances = @(Get-ScheduledTaskRunningInstances)
        }
        catch {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "scheduled_task_instance_query_failed"
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $confirmedTaskState = [string]$confirmedTask.State
        $confirmedWriterCount = [int]$confirmation.Report.writer_group_count
        $confirmedLockState = [string]$confirmation.Report.supervisor_lock_state
        $lastRunTransitioned = (
            [DateTime]$confirmedTaskInfo.LastRunTime -gt $preStartLastRunTime
        )
        if ($confirmedTaskState -eq "Ready") {
            if ($confirmedWriterCount -gt 0 -or $confirmedLockState -eq "held") {
                Write-StartStatusAndExit @{
                    status = "task_start_confirmation_refused"
                    reason = "orphaned_writer_not_task_owned"
                    scheduled_task_state = $confirmedTaskState
                    scheduled_task_running_instance_count = $confirmedInstances.Count
                    scheduled_task_last_result_diagnostic = [int64]$confirmedTaskInfo.LastTaskResult
                    guard_status = $lastGuardStatus
                    writer_group_count = $confirmedWriterCount
                    supervisor_lock_state = $confirmedLockState
                    task_scheduler_start_requested = $true
                    mutation_performed = $true
                } 4
            }
            if ($lastRunTransitioned) {
                Write-StartStatusAndExit @{
                    status = "task_start_confirmation_refused"
                    reason = "scheduled_task_exited_before_full_lifetime_confirmation"
                    scheduled_task_state = $confirmedTaskState
                    scheduled_task_last_result_diagnostic = [int64]$confirmedTaskInfo.LastTaskResult
                    task_scheduler_start_requested = $true
                    mutation_performed = $true
                } 4
            }
            continue
        }
        if ($confirmedTaskState -eq "Queued") {
            if ($confirmedWriterCount -gt 0) {
                Write-StartStatusAndExit @{
                    status = "task_start_confirmation_refused"
                    reason = "orphaned_writer_not_task_owned"
                    scheduled_task_state = $confirmedTaskState
                    writer_group_count = $confirmedWriterCount
                    supervisor_lock_state = $confirmedLockState
                    task_scheduler_start_requested = $true
                    mutation_performed = $true
                } 4
            }
            continue
        }
        if ($confirmedTaskState -ne "Running") {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "scheduled_task_not_running_during_confirmation"
                scheduled_task_state = $confirmedTaskState
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        if (-not $lastRunTransitioned) {
            continue
        }
        if ($confirmedInstances.Count -gt 1) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "multiple_scheduled_task_running_instances"
                scheduled_task_running_instance_count = $confirmedInstances.Count
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        if ($confirmedInstances.Count -eq 0) {
            continue
        }
        $runningInstance = $confirmedInstances[0]
        if ([int]$runningInstance.State -ne 4 -or [int]$runningInstance.EnginePid -le 0) {
            continue
        }
        if (-not (Test-HealthyLockedWriter $confirmation)) {
            if ($confirmedWriterCount -eq 0 -and @("stopped_before_t0", "stopped_during_window") -contains $lastGuardStatus) {
                continue
            }
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "guard_health_not_healthy_locked_single_writer"
                guard_exit_code = [int]$confirmation.ExitCode
                guard_status = $lastGuardStatus
                writer_group_count = $confirmedWriterCount
                supervisor_lock_state = $confirmedLockState
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        try {
            $processMap = Get-ProcessSnapshotById
        }
        catch {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "process_snapshot_failed"
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $taskActionProcesses = @(
            foreach ($candidate in $processMap.Values) {
                if (Test-ExactTaskActionProcess `
                    $candidate `
                    $expectedTaskProcessArguments `
                    $launchRequestUtc) {
                    $candidate
                }
            }
        )
        if ($taskActionProcesses.Count -gt 1) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "multiple_exact_task_action_processes_created_after_request"
                exact_task_action_process_count = $taskActionProcesses.Count
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        if ($taskActionProcesses.Count -eq 0) {
            continue
        }
        $taskActionPid = [int]$taskActionProcesses[0].ProcessId
        $enginePid = [int]$runningInstance.EnginePid
        if (-not (Test-ProcessDescendsFrom $taskActionPid $enginePid $processMap)) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "task_action_not_bound_to_scheduler_engine_pid"
                task_scheduler_engine_pid = $enginePid
                task_action_pid = $taskActionPid
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $writerRootPids = @($confirmation.Report.writer_root_pids)
        if ($writerRootPids.Count -ne 1) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "exact_single_writer_pid_missing"
                writer_root_pid_count = $writerRootPids.Count
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $writerPid = [int]$writerRootPids[0]
        if (-not $processMap.ContainsKey($writerPid)) {
            continue
        }
        if (
            (Get-ProcessCreationUtc $processMap[$writerPid]) -lt $launchRequestUtc -or
            -not (Test-ProcessDescendsFrom $writerPid $taskActionPid $processMap)
        ) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "writer_not_created_under_confirmed_task_action"
                writer_pid = $writerPid
                task_action_pid = $taskActionPid
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $guardPid = Get-ExactGuardAncestorPid `
            -WriterProcessId $writerPid `
            -TaskActionProcessId $taskActionPid `
            -ProcessMap $processMap `
            -ExpectedGuardArguments $expectedGuardStartArguments `
            -NotBeforeUtc $launchRequestUtc
        if ($guardPid -le 0) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "exact_guard_process_not_found_in_task_writer_ancestry"
                writer_pid = $writerPid
                task_action_pid = $taskActionPid
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        $ownedSample = [ordered]@{
            instance_guid = [string]$runningInstance.InstanceGuid
            engine_pid = $enginePid
            task_action_pid = $taskActionPid
            guard_pid = [int]$guardPid
            writer_pid = $writerPid
        }
        if ($null -eq $firstOwnedSample) {
            $firstOwnedSample = [pscustomobject]@{
                CapturedAtUtc = [DateTime]::UtcNow
                Identity = $ownedSample
            }
            continue
        }
        $identityMatches = $true
        foreach ($key in @("instance_guid", "engine_pid", "task_action_pid", "guard_pid", "writer_pid")) {
            if ([string]$firstOwnedSample.Identity[$key] -ne [string]$ownedSample[$key]) {
                $identityMatches = $false
            }
        }
        if (-not $identityMatches) {
            Write-StartStatusAndExit @{
                status = "task_start_confirmation_refused"
                reason = "task_owned_process_identity_changed_between_health_samples"
                first_identity = $firstOwnedSample.Identity
                second_identity = $ownedSample
                task_scheduler_start_requested = $true
                mutation_performed = $true
            } 4
        }
        if ([DateTime]::UtcNow.Subtract([DateTime]$firstOwnedSample.CapturedAtUtc).TotalSeconds -lt 1.0) {
            continue
        }
        Write-StartStatusAndExit @{
            status = "task_scheduler_full_lifetime_ownership_confirmed"
            guard_status = $lastGuardStatus
            writer_group_count = 1
            supervisor_lock_held = $true
            scheduled_task_state = $confirmedTaskState
            scheduled_task_instance_guid = [string]$runningInstance.InstanceGuid
            scheduled_task_engine_pid = $enginePid
            task_action_pid = $taskActionPid
            guard_pid = [int]$guardPid
            writer_pid = $writerPid
            scheduled_task_run_bound_to_request = $true
            live_confirmation_used_last_task_result = $false
            separated_healthy_owned_samples = 2
            task_scheduler_start_requested = $true
            task_identity_reconfirmed = $true
            mutation_performed = $true
        } 0
    }
    $timeoutInfo = Get-ScheduledTaskInfo `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    Write-StartStatusAndExit @{
        status = "task_start_confirmation_refused"
        reason = "task_scheduler_full_lifetime_ownership_confirmation_timed_out"
        guard_status = $lastGuardStatus
        scheduled_task_last_result_diagnostic = if ($null -eq $timeoutInfo) { $null } else { [int64]$timeoutInfo.LastTaskResult }
        live_confirmation_used_last_task_result = $false
        timeout_did_not_stop_task_or_process = $true
        task_scheduler_start_requested = $true
        mutation_performed = $true
    } 4
}

if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
    throw "scheduled_task_mutation_requires_elevated_administrator"
}
$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing) {
    throw "scheduled_task_already_exists_remove_it_explicitly_before_install"
}

if ($PSCmdlet.ShouldProcess($TaskName, "Register full-lifetime gap-v5 MTVCLC collector task")) {
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -InputObject $definition `
        -ErrorAction Stop | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    $diagnostics = Get-ExactScheduledTaskDiagnostics `
        $registered `
        $expectedTaskArguments `
        $taskDescription
    if (-not [bool]$diagnostics.exact) {
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
        "[mtvclc-gap-v5-collector] task installed: {0}; {1} plus every {2} minute(s); collection was not restarted" -f
        $TaskName,
        $TriggerMode,
        $WatchdogIntervalMinutes
    )
}
