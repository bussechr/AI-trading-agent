param(
    [string]$SourceRoot = "",
    [string]$TargetRoot = "",
    [switch]$StartAfterInstall
)

$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($SourceRoot)) {
    $SourceRoot = (Get-Location).Path
}

if ([string]::IsNullOrWhiteSpace($TargetRoot)) {
    $TargetRoot = Join-Path $env:LOCALAPPDATA "Programs\TradingAgent"
}
$TargetRoot = [IO.Path]::GetFullPath($TargetRoot)

$installMarkerValue = "fxstack.windows_install_root.v1"

function Test-OwnedInstallRoot {
    param([string]$Root)

    $marker = Join-Path $Root ".fxstack-install-root"
    if (
        (Test-Path -LiteralPath $marker -PathType Leaf) -and
        [string]::Equals(
            (Get-Content -LiteralPath $marker -Raw),
            $installMarkerValue,
            [StringComparison]::Ordinal
        )
    ) {
        return $true
    }

    # Compatibility for packages installed before the explicit root marker.
    return (
        (Test-Path -LiteralPath (Join-Path $Root "launch_all.bat") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root "monitor_trading_agent.bat") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root "runtime\python\python.exe") -PathType Leaf) -and
        (Test-Path -LiteralPath (Join-Path $Root "installer\windows\uninstall.ps1") -PathType Leaf)
    )
}

if (
    [string]::Equals(
        $TargetRoot.TrimEnd('\'),
        ([IO.Path]::GetPathRoot($TargetRoot)).TrimEnd('\'),
        [StringComparison]::OrdinalIgnoreCase
    )
) {
    throw "install_target_must_not_be_a_filesystem_root"
}

if (
    (Test-Path -LiteralPath $TargetRoot -PathType Container) -and
    $null -ne (Get-ChildItem -LiteralPath $TargetRoot -Force | Select-Object -First 1) -and
    -not (Test-OwnedInstallRoot $TargetRoot)
) {
    throw "install_target_not_owned_refusing_mirror:$TargetRoot"
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
    New-Item -ItemType Directory -Path $extractRoot -Force | Out-Null
    $archiveEntries = @(& tar.exe -tf $payloadPath)
    if ($LASTEXITCODE -ne 0) {
        throw "tar listing failed with code $LASTEXITCODE"
    }
    if ($archiveEntries.Count -eq 0) {
        throw "installer payload archive is empty"
    }
    foreach ($entry in $archiveEntries) {
        $normalizedEntry = ([string]$entry).Replace('\', '/').TrimEnd('/')
        $segments = @($normalizedEntry.Split('/'))
        if (
            [string]::IsNullOrWhiteSpace($normalizedEntry) -or
            $normalizedEntry.StartsWith('/') -or
            $normalizedEntry -match '^[A-Za-z]:' -or
            $segments.Count -eq 0 -or
            $segments[0] -ne 'app' -or
            $segments -contains '.' -or
            $segments -contains '..'
        ) {
            throw "unsafe installer payload member:$entry"
        }
    }
    $archiveDetails = @(& tar.exe -tvf $payloadPath)
    if ($LASTEXITCODE -ne 0) {
        throw "tar verbose listing failed with code $LASTEXITCODE"
    }
    foreach ($detail in $archiveDetails) {
        $trimmedDetail = ([string]$detail).TrimStart()
        if (-not $trimmedDetail -or $trimmedDetail[0] -notin @('-', 'd')) {
            throw "installer payload contains a link or special entry:$detail"
        }
    }
    & tar.exe -xf $payloadPath -C $extractRoot
    if ($LASTEXITCODE -ne 0) {
        throw "tar extraction failed with code $LASTEXITCODE"
    }

    $appSource = Join-Path $extractRoot "app"
    if (-not (Test-Path -LiteralPath $appSource -PathType Container)) {
        throw "installer payload did not contain an app directory"
    }

    # Do not interrupt a healthy installed stack until the replacement archive
    # has passed structure/type validation and extracted into the isolated temp
    # root successfully.
    if (Test-Path -LiteralPath (Join-Path $TargetRoot "launch_all.bat") -PathType Leaf) {
        Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat stop" -Wait -WindowStyle Hidden
    }

    New-Item -ItemType Directory -Path (Split-Path $TargetRoot -Parent) -Force | Out-Null
    if (-not (Test-Path $TargetRoot)) {
        New-Item -ItemType Directory -Path $TargetRoot -Force | Out-Null
    }

    $sourceLogs = Join-Path $appSource "logs"
    $targetLogs = Join-Path $TargetRoot "logs"
    $sourceData = Join-Path $appSource "fx-quant-stack\data"
    $targetData = Join-Path $TargetRoot "fx-quant-stack\data"

    # Mirror only package-owned paths. Logs contain local credentials and active
    # endpoint state; data contains the runtime DB and operator-owned snapshots.
    & robocopy.exe $appSource $TargetRoot /MIR /XD $sourceLogs $targetLogs $sourceData $targetData /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) {
        throw "robocopy failed with code $LASTEXITCODE"
    }

    # Seed or update packaged snapshots without purging any local data files.
    if (Test-Path -LiteralPath $sourceData -PathType Container) {
        New-Item -ItemType Directory -Path $targetData -Force | Out-Null
        & robocopy.exe $sourceData $targetData /E /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) {
            throw "mutable data merge failed with code $LASTEXITCODE"
        }
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
    $monitorArgs = "/c `"call `"$TargetRoot\monitor_trading_agent.bat`"`""
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

    if ($StartAfterInstall.IsPresent) {
        Start-Process -FilePath "cmd.exe" -WorkingDirectory $TargetRoot -ArgumentList "/c","set LAUNCH_NO_PAUSE=1&& call launch_all.bat live 10000" -Wait -WindowStyle Hidden
        Start-Process "http://127.0.0.1:3000" | Out-Null
    }

    Write-Host "[install] Trading Agent installed to $TargetRoot"
    Write-Host "[install] Desktop shortcuts created."
    if (-not $StartAfterInstall.IsPresent) {
        Write-Host "[install] Services were not started. Use the Trading Agent shortcut after release authority and MT4 are ready."
    }
} finally {
    if (Test-Path $tempRoot) {
        Remove-Item -Recurse -Force $tempRoot -ErrorAction SilentlyContinue
    }
}
