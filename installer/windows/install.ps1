param(
    [string]$SourceRoot = "",
    [string]$TargetRoot = "",
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = (Get-Location).Path
}

if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = Join-Path $env:LOCALAPPDATA "Programs\TradingAgent"
}

$payloadPath = Join-Path $SourceRoot "payload.tar"
if (-not (Test-Path $payloadPath)) {
    throw "payload.tar not found next to the installer: $payloadPath"
}

$tempRoot = Join-Path $env:TEMP ("TradingAgentInstall_" + [guid]::NewGuid().ToString("N"))
$extractRoot = Join-Path $tempRoot "extract"
$startMenuDir = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\Trading Agent"
$desktopDir = [Environment]::GetFolderPath("Desktop")

function New-Shortcut {
    param(
        [string]$Path,
        [string]$TargetPath,
        [string]$Arguments = "",
        [string]$WorkingDirectory = ""
    )
    $shell = New-Object -ComObject WScript.Shell
    $shortcut = $shell.CreateShortcut($Path)
    $shortcut.TargetPath = $TargetPath
    if ($Arguments) { $shortcut.Arguments = $Arguments }
    if ($WorkingDirectory) { $shortcut.WorkingDirectory = $WorkingDirectory }
    $shortcut.Save()
}

try {
    if (Test-Path (Join-Path $TargetRoot "launch_all.bat")) {
        Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop" -Wait -WindowStyle Hidden
    }

    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    & tar.exe -xf $payloadPath -C $extractRoot
    if ($LASTEXITCODE -ne 0) {
        throw "tar extraction failed with code $LASTEXITCODE"
    }

    $appSource = Join-Path $extractRoot "app"
    if (-not (Test-Path $appSource)) {
        throw "installer payload did not contain an app directory"
    }

    New-Item -ItemType Directory -Path (Split-Path $TargetRoot -Parent) -Force | Out-Null
    if (-not (Test-Path $TargetRoot)) {
        New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
    }

    & robocopy.exe $appSource $TargetRoot /MIR /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with code $LASTEXITCODE"
    }

    foreach ($rel in @(
        "logs",
        "fx-quant-stack\data\state",
        "fx-quant-stack\data\dukascopy",
        "fx-quant-stack\data\raw",
        "fx-quant-stack\data\labels",
        "fx-quant-stack\data\silver",
        "fx-quant-stack\data\bronze"
    )) {
        New-Item -ItemType Directory -Path (Join-Path $TargetRoot $rel) -Force | Out-Null
    }

    $uninstallScript = Join-Path $TargetRoot "installer\windows\uninstall.ps1"
    if (-not (Test-Path $uninstallScript)) {
        throw "uninstall script missing from installed payload"
    }

    New-Item -ItemType Directory -Path $startMenuDir -Force | Out-Null

    $launchArgs = "/c `"set LAUNCH_NO_PAUSE=1&& call `"$TargetRoot\launch_all.bat`" live 10000`""
    $stopArgs = "/c `"set LAUNCH_NO_PAUSE=1&& call `"$TargetRoot\launch_all.bat`" stop`""
    $statusArgs = "/c `"set LAUNCH_NO_PAUSE=1&& call `"$TargetRoot\launch_all.bat`" status`""
    $monitorArgs = "/c `"call `"$TargetRoot\ops\windows\25_monitor_everything.bat`"`""
    $uninstallArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$uninstallScript`" -TargetRoot `"$TargetRoot`""

    New-Shortcut -Path (Join-Path $desktopDir "Trading Agent.lnk") -TargetPath "cmd.exe" -Arguments $launchArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $desktopDir "Trading Agent Monitor.lnk") -TargetPath "cmd.exe" -Arguments $monitorArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $desktopDir "Trading Agent Stop.lnk") -TargetPath "cmd.exe" -Arguments $stopArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $desktopDir "Trading Agent Status.lnk") -TargetPath "cmd.exe" -Arguments $statusArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $desktopDir "Trading Agent Uninstall.lnk") -TargetPath "powershell.exe" -Arguments $uninstallArgs -WorkingDirectory $TargetRoot

    New-Shortcut -Path (Join-Path $startMenuDir "Trading Agent.lnk") -TargetPath "cmd.exe" -Arguments $launchArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $startMenuDir "Trading Agent Monitor.lnk") -TargetPath "cmd.exe" -Arguments $monitorArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $startMenuDir "Trading Agent Stop.lnk") -TargetPath "cmd.exe" -Arguments $stopArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $startMenuDir "Trading Agent Status.lnk") -TargetPath "cmd.exe" -Arguments $statusArgs -WorkingDirectory $TargetRoot
    New-Shortcut -Path (Join-Path $startMenuDir "Trading Agent Uninstall.lnk") -TargetPath "powershell.exe" -Arguments $uninstallArgs -WorkingDirectory $TargetRoot

    $taskRegister = Join-Path $TargetRoot "ops\windows\28_register_weekly_full_retrain_task.bat"
    if (Test-Path $taskRegister) {
        Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","call `"$taskRegister`"" -Wait -WindowStyle Hidden
    }

    if (-not $SkipStart.IsPresent) {
        Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat live 10000" -Wait -WindowStyle Hidden
        Start-Process "http://127.0.0.1:3000" | Out-Null
    }

    Write-Host "[install] Trading Agent installed to $TargetRoot"
    Write-Host "[install] Desktop shortcuts created."
} finally {
    if (Test-Path $tempRoot) {
        Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
    }
}
