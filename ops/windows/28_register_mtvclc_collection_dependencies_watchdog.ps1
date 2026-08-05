[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Install", "Preview", "Remove")]
    [string]$Action = "Install",

    [ValidatePattern("^[A-Za-z0-9_.-]{1,100}$")]
    [string]$TaskName = "TradingAgentMtvclcCollectionDependencies",

    [ValidateSet("AtLogOn", "AtStartup")]
    [string]$TriggerMode = "AtLogOn",

    [string]$PythonExe,
    [string]$TerminalExe,
    [string]$ApiKeyFile,
    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(1, 60)]
    [int]$WatchdogIntervalMinutes = 5,

    [ValidateRange(10, 300)]
    [int]$ReadinessTimeoutSeconds = 90,

    [ValidateRange(1, 15)]
    [int]$HttpTimeoutSeconds = 3
)

# AGENT: ROLE: reversible current-user Scheduled Task registrar for collection dependencies only.
# AGENT: HANDSHAKE: AtLogOn/repeat (or admin AtStartup) -> hash-pinned dependency ensure wrapper.
# AGENT: ISOLATION: task arguments contain the API-key file path only; this registrar never reads the key.
# AGENT: SIDE EFFECTS: Install updates only a proven owned task; Remove deletes only that same identity and neither action starts processes.

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

function Test-IsAdministrator {
    $principal = [Security.Principal.WindowsPrincipal]::new(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$EnsurePath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\28_ensure_mtvclc_collection_dependencies.ps1"
) "dependency_watchdog_wrapper_missing"
$BridgeLauncherPath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\20_start_bridge.bat"
) "bridge_launcher_missing"
$Mt4LauncherPath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\19_start_mt4.ps1"
) "mt4_launcher_missing"
$EnvPath = Resolve-ExistingFile (
    Join-Path $RepositoryRoot "ops\windows\_env.bat"
) "windows_env_launcher_missing"
$WindowsPowerShell = Resolve-ExistingFile (
    Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe"
) "windows_powershell_missing"
$TaskPath = "\"
$TaskDescriptionPrefix = (
    "FXSTACK_OWNER=mtvclc_collection_dependencies_watchdog_v1; " +
    "collection-only bridge+visible-IG-MT4; no runtime or trading authority; trigger="
)
$LogOnTaskDescription = $TaskDescriptionPrefix + "AtLogOn."
$StartupTaskDescription = $TaskDescriptionPrefix + "AtStartupInteractive."
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
        @("Interactive", "InteractiveToken") -contains [string]$Task.Principal.LogonType -and
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
        task_path = [string]$Task.TaskPath
        task_path_matches = $pathMatches
        execute_matches = $executeMatches
        file_argument_matches = $fileArgumentMatches
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

if ($Action -eq "Remove") {
    $existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        Write-Output ("[mtvclc-dependencies] task already absent: {0}" -f $TaskName)
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
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister MTVCLC collection dependency watchdog")) {
        Unregister-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction Stop
        Write-Output (
            "[mtvclc-dependencies] task removed: {0}; bridge and MT4 were not stopped" -f
            $TaskName
        )
    }
    exit 0
}

if (-not [string]::Equals($BaseUrl.TrimEnd('/'), "http://127.0.0.1:58710", [StringComparison]::Ordinal)) {
    throw "bridge_base_url_must_be_exact_127_0_0_1_58710"
}
$ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
$ResolvedTerminal = Resolve-ExistingFile $TerminalExe "mt4_terminal_executable_invalid"
$ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
if (
    [IO.Path]::GetFileName($ResolvedTerminal) -ne "terminal.exe" -or
    $ResolvedTerminal -notmatch '(?i)\\IG MetaTrader 4 Terminal\\terminal\.exe$'
) {
    throw "configured_terminal_is_not_exact_ig_mt4"
}
$ensureSha256 = Get-FileSha256 $EnsurePath
$bridgeLauncherSha256 = Get-FileSha256 $BridgeLauncherPath
$mt4LauncherSha256 = Get-FileSha256 $Mt4LauncherPath
$envSha256 = Get-FileSha256 $EnvPath

$argumentParts = @(
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy", "Bypass",
    "-File", (Quote-TaskArgument $EnsurePath),
    "-PythonExe", (Quote-TaskArgument $ResolvedPython),
    "-TerminalExe", (Quote-TaskArgument $ResolvedTerminal),
    "-ApiKeyFile", (Quote-TaskArgument $ResolvedApiKeyFile),
    "-BaseUrl", (Quote-TaskArgument "http://127.0.0.1:58710"),
    "-ReadinessTimeoutSeconds", ([string]$ReadinessTimeoutSeconds),
    "-HttpTimeoutSeconds", ([string]$HttpTimeoutSeconds),
    "-ExpectedEnsureSha256", $ensureSha256,
    "-ExpectedBridgeLauncherSha256", $bridgeLauncherSha256,
    "-ExpectedMt4LauncherSha256", $mt4LauncherSha256,
    "-ExpectedEnvSha256", $envSha256
)
$expectedArguments = $argumentParts -join " "
$taskAction = New-ScheduledTaskAction `
    -Execute $WindowsPowerShell `
    -Argument $expectedArguments
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
    # Keep an interactive token even for the startup trigger: visible MT4 is
    # never launched into session 0. StartWhenAvailable defers until logon.
    New-ScheduledTaskPrincipal `
        -UserId $currentUserName `
        -LogonType Interactive `
        -RunLevel Highest
}
else {
    New-ScheduledTaskPrincipal `
        -UserId $currentUserName `
        -LogonType Interactive `
        -RunLevel Limited
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
        schema_version = "fxstack.mtvclc_collection_dependencies_task_preview.v1"
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
        start_when_available = $true
        principal_user = $currentUserName
        principal_logon_type = "Interactive"
        principal_run_level = if ($TriggerMode -eq "AtStartup") { "Highest" } else { "Limited" }
        ensure_source_sha256 = $ensureSha256
        bridge_launcher_source_sha256 = $bridgeLauncherSha256
        mt4_launcher_source_sha256 = $mt4LauncherSha256
        env_source_sha256 = $envSha256
        api_key_value_stored = $false
        collection_only = $true
        runtime_authorized = $false
        trading_authorized = $false
        mutation_performed = $false
    } | ConvertTo-Json -Compress -Depth 6)
    exit 0
}

if ($TriggerMode -eq "AtStartup" -and -not (Test-IsAdministrator)) {
    throw "scheduled_task_mutation_requires_elevated_administrator"
}
$existing = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction SilentlyContinue
if ($null -ne $existing -and -not (Test-OwnedScheduledTask $existing)) {
    $diagnostics = Get-OwnedScheduledTaskDiagnostics $existing
    throw (
        "scheduled_task_identity_mismatch_refusing_overwrite:" +
        ($diagnostics | ConvertTo-Json -Compress -Depth 4)
    )
}

$verb = if ($null -eq $existing) { "Register" } else { "Update owned" }
if ($PSCmdlet.ShouldProcess($TaskName, "$verb MTVCLC collection dependency watchdog")) {
    Register-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -InputObject $definition `
        -Force `
        -ErrorAction Stop | Out-Null
    $registered = Get-ScheduledTask -TaskPath $TaskPath -TaskName $TaskName -ErrorAction Stop
    if (-not (Test-OwnedScheduledTask $registered)) {
        $diagnostics = Get-OwnedScheduledTaskDiagnostics $registered
        throw (
            "registered_task_identity_verification_failed:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 4)
        )
    }
    $registeredActions = @($registered.Actions)
    if (
        $registeredActions.Count -ne 1 -or
        -not [string]::Equals(
            [string]$registeredActions[0].Arguments,
            $expectedArguments,
            [StringComparison]::Ordinal
        )
    ) {
        throw "registered_task_exact_arguments_verification_failed"
    }
    Write-Output (
        "[mtvclc-dependencies] task installed or updated: {0}; {1} plus every {2} minute(s); no process was started" -f
        $TaskName,
        $TriggerMode,
        $WatchdogIntervalMinutes
    )
}
