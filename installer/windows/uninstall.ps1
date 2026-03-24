param(
    [string]$TargetRoot = ""
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = Join-Path $env:LOCALAPPDATA "Programs\TradingAgent"
}

$desktopDir = [Environment]::GetFolderPath("Desktop")
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Trading Agent"

if (Test-Path (Join-Path $TargetRoot "launch_all.bat")) {
    Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop" -Wait -WindowStyle Hidden
}

$taskName = "TradingAgentWeeklyFullRetrain"
try {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop | Out-Null
} catch {
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
