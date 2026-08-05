[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [ValidateSet("Status", "Register", "Start", "Stop", "Disable", "Unregister")]
    [string]$Action = "Status"
)

# AGENT: ROLE: reversible current-user Scheduled Task owner for the runtime-native scalp runtime.
# AGENT HANDSHAKE: current-user logon/on-demand -> exact 21_start_scalp_runtime.bat --run -> broker/account/runtime preflight before mutation.
# AGENT: SIDE EFFECTS: only the named owned task and its runtime process are mutable; MT4 is out of scope.
# AGENT ISOLATION: no credential, broker-data, database, model, registry, or research path is read or stored.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$TaskName = "TradingAgentScalpRuntime"
$TaskPath = "\"
$OwnerDescription = (
    "FXSTACK_OWNER=signed_validation_scalp_runtime_task_v4; " +
    "current-user interactive signed-validation runtime; repo-owned reversible task."
)
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$LauncherPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\21_start_scalp_runtime.bat")
)
$CommandProcessor = [IO.Path]::GetFullPath(
    (Join-Path $env:SystemRoot "System32\cmd.exe")
)
$ExpectedArguments = '/d /c ""' + $LauncherPath + '" --run"'
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentUserName = $CurrentIdentity.Name
$CurrentUserSid = $CurrentIdentity.User.Value

if (-not (Test-Path -LiteralPath $LauncherPath -PathType Leaf)) {
    throw "scalp_runtime_launcher_missing"
}
if (-not (Test-Path -LiteralPath $CommandProcessor -PathType Leaf)) {
    throw "windows_command_processor_missing"
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

function Resolve-UserSid {
    param([string]$UserId)
    if ([string]::IsNullOrWhiteSpace($UserId)) {
        return ""
    }
    try {
        if ($UserId -match '^S-\d-') {
            return ([Security.Principal.SecurityIdentifier]::new($UserId)).Value
        }
        return (
            [Security.Principal.NTAccount]::new($UserId).Translate(
                [Security.Principal.SecurityIdentifier]
            )
        ).Value
    }
    catch {
        return ""
    }
}

function Test-CurrentUser {
    param([string]$UserId)
    $resolvedSid = Resolve-UserSid $UserId
    return (
        [string]::Equals($UserId, $CurrentUserName, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($UserId, $CurrentUserSid, [StringComparison]::OrdinalIgnoreCase) -or
        [string]::Equals($resolvedSid, $CurrentUserSid, [StringComparison]::OrdinalIgnoreCase)
    )
}

function Test-InteractiveLogonType {
    param([object]$Value)
    return @("Interactive", "InteractiveToken", "3") -contains [string]$Value
}

function Test-LimitedRunLevel {
    param([object]$Value)
    return @("Limited", "0") -contains [string]$Value
}

function Test-IgnoreNew {
    param([object]$Value)
    return @("IgnoreNew", "2") -contains [string]$Value
}

function Get-TaskDiagnostics {
    param([object]$Task)

    if ($null -eq $Task) {
        return [ordered]@{
            exists = $false
            identity_owned = $false
            contract_exact = $false
            reason = "task_absent"
        }
    }

    $actions = @($Task.Actions)
    $triggers = @($Task.Triggers)
    $principal = $Task.Principal
    $settings = $Task.Settings
    $action = if ($actions.Count -eq 1) { $actions[0] } else { $null }
    $trigger = if ($triggers.Count -eq 1) { $triggers[0] } else { $null }
    $triggerClass = if ($null -ne $trigger) {
        [string]$trigger.CimClass.CimClassName
    }
    else {
        ""
    }

    $pathMatches = [string]$Task.TaskPath -eq $TaskPath
    $nameMatches = [string]$Task.TaskName -eq $TaskName
    $descriptionMatches = [string]$Task.Description -eq $OwnerDescription
    $actionCountMatches = $actions.Count -eq 1
    $executeMatches = (
        $null -ne $action -and
        (Test-SamePath ([string]$action.Execute) $CommandProcessor)
    )
    $argumentsMatch = (
        $null -ne $action -and
        [string]::Equals(
            [string]$action.Arguments,
            $ExpectedArguments,
            [StringComparison]::Ordinal
        )
    )
    $workingDirectoryMatches = (
        $null -ne $action -and
        (Test-SamePath ([string]$action.WorkingDirectory) $RepositoryRoot)
    )
    $principalUserMatches = Test-CurrentUser ([string]$principal.UserId)
    $logonTypeMatches = Test-InteractiveLogonType $principal.LogonType
    $runLevelMatches = Test-LimitedRunLevel $principal.RunLevel

    # The owner identity deliberately excludes trigger/settings drift so Register
    # can repair an otherwise proven repository task. Action or principal drift is
    # never repaired automatically because it no longer proves this task's owner.
    $identityOwned = (
        $pathMatches -and
        $nameMatches -and
        $descriptionMatches -and
        $actionCountMatches -and
        $executeMatches -and
        $argumentsMatch -and
        $workingDirectoryMatches -and
        $principalUserMatches -and
        $logonTypeMatches -and
        $runLevelMatches
    )

    $triggerExact = (
        $triggers.Count -eq 1 -and
        $triggerClass -eq "MSFT_TaskLogonTrigger" -and
        [bool]$trigger.Enabled -and
        (Test-CurrentUser ([string]$trigger.UserId))
    )
    $settingsExact = (
        [bool]$settings.Enabled -and
        [bool]$settings.AllowDemandStart -and
        [bool]$settings.StartWhenAvailable -and
        [int]$settings.RestartCount -eq 10 -and
        [string]$settings.RestartInterval -eq "PT1M" -and
        [string]$settings.ExecutionTimeLimit -eq "PT0S" -and
        -not [bool]$settings.DisallowStartIfOnBatteries -and
        -not [bool]$settings.StopIfGoingOnBatteries -and
        (Test-IgnoreNew $settings.MultipleInstances)
    )

    return [ordered]@{
        exists = $true
        identity_owned = $identityOwned
        contract_exact = ($identityOwned -and $triggerExact -and $settingsExact)
        state = [string]$Task.State
        enabled = [bool]$settings.Enabled
        task_path_matches = $pathMatches
        task_name_matches = $nameMatches
        description_matches = $descriptionMatches
        action_count = $actions.Count
        execute_matches = $executeMatches
        arguments_match = $argumentsMatch
        working_directory_matches = $workingDirectoryMatches
        principal_user_matches = $principalUserMatches
        principal_logon_type = [string]$principal.LogonType
        principal_run_level = [string]$principal.RunLevel
        trigger_count = $triggers.Count
        trigger_class = $triggerClass
        trigger_exact = $triggerExact
        settings_exact = $settingsExact
        start_when_available = [bool]$settings.StartWhenAvailable
        allow_demand_start = [bool]$settings.AllowDemandStart
        restart_count = [int]$settings.RestartCount
        restart_interval = [string]$settings.RestartInterval
        execution_time_limit = [string]$settings.ExecutionTimeLimit
        disallow_start_on_batteries = [bool]$settings.DisallowStartIfOnBatteries
        stop_if_going_on_batteries = [bool]$settings.StopIfGoingOnBatteries
        multiple_instances = [string]$settings.MultipleInstances
    }
}

function Get-TaskStatusPayload {
    $task = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    $diagnostics = Get-TaskDiagnostics $task
    $info = if ($null -ne $task) {
        Get-ScheduledTaskInfo `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction SilentlyContinue
    }
    else {
        $null
    }
    return [ordered]@{
        schema_version = "fxstack.scalp_runtime_scheduled_task_status.v1"
        task_name = $TaskName
        task_path = $TaskPath
        repository_root = $RepositoryRoot
        expected_action = [ordered]@{
            execute = $CommandProcessor
            arguments = $ExpectedArguments
            working_directory = $RepositoryRoot
        }
        expected_principal = [ordered]@{
            user = $CurrentUserName
            sid = $CurrentUserSid
            logon_type = "InteractiveToken"
            run_level = "Limited"
        }
        expected_trigger = "AtLogOnCurrentUser"
        expected_settings = [ordered]@{
            enabled = $true
            allow_demand_start = $true
            start_when_available = $true
            restart_count = 10
            restart_interval = "PT1M"
            execution_time_limit = "PT0S"
            disallow_start_on_batteries = $false
            stop_if_going_on_batteries = $false
            multiple_instances = "IgnoreNew"
        }
        diagnostics = $diagnostics
        last_run_time = if ($null -ne $info) { $info.LastRunTime } else { $null }
        last_task_result = if ($null -ne $info) { $info.LastTaskResult } else { $null }
        next_run_time = if ($null -ne $info) { $info.NextRunTime } else { $null }
        number_of_missed_runs = if ($null -ne $info) { $info.NumberOfMissedRuns } else { $null }
    }
}

function Write-TaskStatus {
    Write-Output ((Get-TaskStatusPayload) | ConvertTo-Json -Compress -Depth 8)
}

function Get-ExistingOwnedTask {
    param([switch]$RequireExact)
    $task = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        throw "scalp_runtime_scheduled_task_absent"
    }
    $diagnostics = Get-TaskDiagnostics $task
    if (-not [bool]$diagnostics.identity_owned) {
        throw (
            "scheduled_task_identity_mismatch_refusing_mutation:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 5)
        )
    }
    if ($RequireExact -and -not [bool]$diagnostics.contract_exact) {
        throw (
            "scheduled_task_contract_mismatch_run_register_first:" +
            ($diagnostics | ConvertTo-Json -Compress -Depth 5)
        )
    }
    return $task
}

if ($Action -eq "Status") {
    Write-TaskStatus
    exit 0
}

$TaskAction = New-ScheduledTaskAction `
    -Execute $CommandProcessor `
    -Argument $ExpectedArguments `
    -WorkingDirectory $RepositoryRoot
$LogOnTrigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUserName
$TaskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit ([TimeSpan]::Zero)
$TaskPrincipal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUserSid `
    -LogonType Interactive `
    -RunLevel Limited

if ($Action -eq "Register") {
    $existing = Get-ScheduledTask `
        -TaskPath $TaskPath `
        -TaskName $TaskName `
        -ErrorAction SilentlyContinue
    if ($null -ne $existing) {
        $existingDiagnostics = Get-TaskDiagnostics $existing
        if (-not [bool]$existingDiagnostics.identity_owned) {
            throw (
                "scheduled_task_identity_mismatch_refusing_overwrite:" +
                ($existingDiagnostics | ConvertTo-Json -Compress -Depth 5)
            )
        }
        if (
            [string]$existing.State -eq "Running" -and
            -not [bool]$existingDiagnostics.contract_exact
        ) {
            throw "scheduled_task_running_with_contract_drift_stop_before_register"
        }
    }

    if ($PSCmdlet.ShouldProcess($TaskName, "Register or repair account-attested scalp runtime task")) {
        if ($null -eq $existing) {
            Register-ScheduledTask `
                -TaskPath $TaskPath `
                -TaskName $TaskName `
                -Action $TaskAction `
                -Trigger $LogOnTrigger `
                -Settings $TaskSettings `
                -Principal $TaskPrincipal `
                -Description $OwnerDescription `
                -ErrorAction Stop | Out-Null
        }
        elseif (-not [bool]$existingDiagnostics.contract_exact) {
            Set-ScheduledTask `
                -TaskPath $TaskPath `
                -TaskName $TaskName `
                -Action $TaskAction `
                -Trigger $LogOnTrigger `
                -Settings $TaskSettings `
                -Principal $TaskPrincipal `
                -ErrorAction Stop | Out-Null
        }
        Enable-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction Stop | Out-Null
        $registered = Get-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction Stop
        $registeredDiagnostics = Get-TaskDiagnostics $registered
        if (-not [bool]$registeredDiagnostics.contract_exact) {
            throw (
                "registered_task_contract_verification_failed:" +
                ($registeredDiagnostics | ConvertTo-Json -Compress -Depth 5)
            )
        }
    }
    Write-TaskStatus
    exit 0
}

if ($Action -eq "Start") {
    $task = Get-ExistingOwnedTask
    if (-not [bool]$task.Settings.Enabled) {
        Enable-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction Stop | Out-Null
    }
    $task = Get-ExistingOwnedTask -RequireExact
    if ([string]$task.State -ne "Running") {
        if ($PSCmdlet.ShouldProcess($TaskName, "Start scalp runtime task instance")) {
            Start-ScheduledTask `
                -TaskPath $TaskPath `
                -TaskName $TaskName `
                -ErrorAction Stop
        }
    }
    Write-TaskStatus
    exit 0
}

if ($Action -eq "Stop") {
    $task = Get-ExistingOwnedTask
    if ([string]$task.State -eq "Running") {
        if ($PSCmdlet.ShouldProcess($TaskName, "Stop scalp runtime task instance")) {
            Stop-ScheduledTask `
                -TaskPath $TaskPath `
                -TaskName $TaskName `
                -ErrorAction Stop
        }
    }
    Write-TaskStatus
    exit 0
}

if ($Action -eq "Disable") {
    $null = Get-ExistingOwnedTask
    if ($PSCmdlet.ShouldProcess($TaskName, "Disable future scalp runtime task starts")) {
        Disable-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -ErrorAction Stop | Out-Null
    }
    Write-TaskStatus
    exit 0
}

if ($Action -eq "Unregister") {
    $task = Get-ExistingOwnedTask
    if ([string]$task.State -eq "Running") {
        throw "scalp_runtime_scheduled_task_running_stop_before_unregister"
    }
    if ($PSCmdlet.ShouldProcess($TaskName, "Unregister scalp runtime task")) {
        Unregister-ScheduledTask `
            -TaskPath $TaskPath `
            -TaskName $TaskName `
            -Confirm:$false `
            -ErrorAction Stop
    }
    Write-TaskStatus
    exit 0
}

throw "unsupported_scalp_runtime_scheduled_task_action"
