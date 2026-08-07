param(
    [string]$TargetRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = Join-Path $env:LOCALAPPDATA "Programs\TradingAgent"
}
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)

$installMarkerValue = "fxstack.windows_install_root.v1"
$installMarker = Join-Path $TargetRoot ".fxstack-install-root"
$markerMatches = (
    (Test-Path -LiteralPath $installMarker -PathType Leaf) -and
    [string]::Equals(
        (Get-Content -LiteralPath $installMarker -Raw),
        $installMarkerValue,
        [StringComparison]::Ordinal
    )
)
$legacyInstallIdentityMatches = (
    (Test-Path -LiteralPath (Join-Path $TargetRoot "launch_all.bat") -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $TargetRoot "monitor_trading_agent.bat") -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $TargetRoot "runtime\python\python.exe") -PathType Leaf) -and
    (Test-Path -LiteralPath (Join-Path $TargetRoot "installer\windows\uninstall.ps1") -PathType Leaf)
)

if (
    [string]::Equals(
        $TargetRoot.TrimEnd('\'),
        ([IO.Path]::GetPathRoot($TargetRoot)).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "uninstall_target_must_not_be_a_filesystem_root"
}
if (
    (Test-Path -LiteralPath $TargetRoot -PathType Container) -and
    -not $markerMatches -and
    -not $legacyInstallIdentityMatches
) {
    throw "uninstall_target_identity_missing_refusing_recursive_delete:$TargetRoot"
}

$desktopDir = [Environment]::GetFolderPath("Desktop")
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Trading Agent"
$scalpTaskName = "TradingAgentScalpRuntime"
$scalpTaskManager = Join-Path $TargetRoot "ops\windows\22_manage_scalp_runtime_task.ps1"
$scalpLauncher = [IO.Path]::GetFullPath(
    (Join-Path $TargetRoot "ops\windows\21_start_scalp_runtime.bat")
)
$removeOwnedScalpTask = $false

# Disarm the persistent launcher before shutdown so it cannot race the
# egress-safe stack stop. The task owner performs the complete identity check.
$scalpTask = Get-ScheduledTask `
    -TaskPath "\" `
    -TaskName $scalpTaskName `
    -ErrorAction SilentlyContinue
if ($null -ne $scalpTask) {
    $scalpActions = @($scalpTask.Actions)
    $referencesTargetRoot = @(
        $scalpActions | Where-Object {
            $actionArguments = [string]$_.Arguments
            [string]::Equals(
                [IO.Path]::GetFullPath([string]$_.WorkingDirectory),
                [IO.Path]::GetFullPath($TargetRoot),
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $actionArguments.IndexOf(
                $scalpLauncher,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        }
    ).Count -gt 0

    if (-not (Test-Path -LiteralPath $scalpTaskManager -PathType Leaf)) {
        if ($referencesTargetRoot) {
            throw "uninstall_refusing_to_orphan_scalp_runtime_task:task_manager_missing"
        }
        Write-Warning "A foreign TradingAgentScalpRuntime task was preserved."
    }
    else {
        try {
            & $scalpTaskManager -Action Disable -Confirm:$false | Out-Null
            $removeOwnedScalpTask = $true
        }
        catch {
            if ($referencesTargetRoot) {
                throw "uninstall_refusing_to_orphan_scalp_runtime_task:$($_.Exception.Message)"
            }
            Write-Warning "A foreign TradingAgentScalpRuntime task was preserved."
        }
    }
}

if (Test-Path (Join-Path $TargetRoot "launch_all.bat")) {
    Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop" -Wait -WindowStyle Hidden
}

if ($removeOwnedScalpTask) {
    & $scalpTaskManager -Action Stop -Confirm:$false | Out-Null
    & $scalpTaskManager -Action Unregister -Confirm:$false | Out-Null
    if ($null -ne (Get-ScheduledTask -TaskPath "\" -TaskName $scalpTaskName -ErrorAction SilentlyContinue)) {
        throw "owned_scalp_runtime_task_unregister_verification_failed"
    }
}

# Upgrade cleanup for the retired auto-retrain supervisor. Never delete a
# same-named foreign task: the historical action, description, and weekly
# trigger must all match the repository-owned definition.
$legacyTaskName = "TradingAgentWeeklyFullRetrain"
$legacyDescription = "Trading Agent Saturday full retrain, validation, gated activation, and stack restart."
$legacyTask = Get-ScheduledTask -TaskPath "\" -TaskName $legacyTaskName -ErrorAction SilentlyContinue
if ($null -ne $legacyTask) {
    $legacyActions = @($legacyTask.Actions)
    $legacyTriggers = @($legacyTask.Triggers)
    $legacyActionMatches = (
        $legacyActions.Count -eq 1 -and
        [IO.Path]::GetFileName([string]$legacyActions[0].Execute) -eq "cmd.exe" -and
        [string]$legacyActions[0].Arguments -match '(?i)^/c\s+"[^"\r\n]*\\ops\\+windows\\+26_weekly_full_retrain_and_activate\.bat"$'
    )
    $legacyTriggerMatches = (
        $legacyTriggers.Count -eq 1 -and
        [string]$legacyTriggers[0].CimClass.CimClassName -eq "MSFT_TaskWeeklyTrigger" -and
        $legacyTriggers[0].Enabled -eq $true
    )
    if (
        [string]$legacyTask.Description -eq $legacyDescription -and
        $legacyActionMatches -and
        $legacyTriggerMatches
    ) {
        Unregister-ScheduledTask `
            -TaskPath "\" `
            -TaskName $legacyTaskName `
            -Confirm:$false `
            -ErrorAction Stop | Out-Null
    }
}

foreach ($path in @(
    (Join-Path $desktopDir "Trading Agent.lnk"),
    (Join-Path $desktopDir "Trading Agent Monitor.lnk"),
    (Join-Path $desktopDir "Trading Agent Stop.lnk"),
    (Join-Path $desktopDir "Trading Agent Status.lnk"),
    (Join-Path $desktopDir "Trading Agent Uninstall.lnk")
)) {
    if (Test-Path $path) {
        Remove-Item -Force $path -ErrorAction SilentlyContinue
    }
}

if (Test-Path $startMenuDir) {
    Remove-Item -Recurse -Force $startMenuDir -ErrorAction SilentlyContinue
}

if (Test-Path $TargetRoot) {
    Remove-Item -Recurse -Force $TargetRoot -ErrorAction SilentlyContinue
}

Write-Host "[uninstall] Trading Agent removed from $TargetRoot"
