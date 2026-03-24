param(
    [string]$TaskName = "TradingAgentWeeklyFullRetrain",
    [string]$TaskTime = "03:00"
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
$batchPath = Join-Path $root "ops\\windows\\26_weekly_full_retrain_and_activate.bat"

if (-not (Test-Path $batchPath)) {
    throw "weekly retrain batch not found: $batchPath"
}

$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$batchPath`""
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Saturday -At $TaskTime
$userId = if ($env:USERDOMAIN) { "{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME } else { $env:USERNAME }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable

try {
    $principal = New-ScheduledTaskPrincipal -UserId $userId -LogonType S4U -RunLevel Highest
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description "Trading Agent Saturday full retrain, validation, gated activation, and stack restart." `
        -Force | Out-Null
    Write-Host ("[task] registered {0} for Saturdays at {1} with highest privileges" -f $TaskName, $TaskTime)
    exit 0
} catch {
    Register-ScheduledTask `
        -TaskName $TaskName `
        -Action $action `
        -Trigger $trigger `
        -Settings $settings `
        -Description "Trading Agent Saturday full retrain, validation, gated activation, and stack restart." `
        -Force | Out-Null
    Write-Warning "registered fallback current-user task because elevated registration was denied"
    Write-Host ("[task] registered {0} for Saturdays at {1} without highest privileges" -f $TaskName, $TaskTime)
}
