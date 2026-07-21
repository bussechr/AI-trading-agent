# AGENT: ROLE: Stop only repo-owned stack process trees and prove their listeners are gone.
# AGENT: STATE / SIDE EFFECTS: terminates validated repo process trees; never targets MT4.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [Parameter(Mandatory = $true)]
    [string]$PidDirectory,

    [string]$PortsCsv = "",

    [ValidateRange(1, 120)]
    [int]$WaitSeconds = 10
)

$ErrorActionPreference = "Stop"
$resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
$rootBoundaryPattern = "(?i)(?:^|[`"'\s=])" + [regex]::Escape($resolvedRoot) + "(?=$|[`"'\s\\/])"
$taskkill = Join-Path $env:SystemRoot "System32\taskkill.exe"
$ports = @(
    $PortsCsv.Split(',', [System.StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object {
            $parsed = 0
            if ([int]::TryParse($_.Trim(), [ref]$parsed) -and $parsed -gt 0) {
                $parsed
            }
        } |
        Sort-Object -Unique
)

function Test-RootOwnership {
    param([object]$Process)

    $commandLine = [string]$Process.CommandLine
    $executablePath = [string]$Process.ExecutablePath
    $executableOwned = $false
    if ($executablePath.Trim()) {
        try {
            $resolvedExecutable = [System.IO.Path]::GetFullPath($executablePath)
            $executableOwned = (
                $resolvedExecutable.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
                $resolvedExecutable.StartsWith(
                    $resolvedRoot + [System.IO.Path]::DirectorySeparatorChar,
                    [System.StringComparison]::OrdinalIgnoreCase
                )
            )
        } catch {
            $executableOwned = $false
        }
    }
    return $executableOwned -or $commandLine -match $rootBoundaryPattern
}

function Test-StackWorker {
    param([object]$Process)

    $commandLine = [string]$Process.CommandLine
    return (
        $commandLine -match "(?i)(?:-m\s+)?uvicorn\s+fxstack\.api\.app:app(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?fxstack\.runtime\.runner(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?fxstack\.runtime\.feature_push_worker(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?fxstack\.runtime\.monitor(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+bridge\s+serve(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+runtime\s+run(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+features\s+push-worker(?:\s|$)" -or
        $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+monitor\s+confidence(?:\s|$)" -or
        $commandLine -match "(?i)24_start_feature_push_worker\.bat.*\s--run(?:\s|$)" -or
        $commandLine -match "(?i)node_modules[\\/]next[\\/]dist[\\/]bin[\\/]next.*\sstart\s+-p\s+" -or
        $commandLine -match "(?i)\.next[\\/]standalone[\\/]server\.js(?:\s|$)" -or
        $commandLine -match "(?i)node_modules[\\/]next[\\/]dist[\\/]bin[\\/]next.*\sbuild(?:\s|$)"
    )
}

function Get-ProcessSnapshot {
    return @(
        Get-CimInstance Win32_Process -ErrorAction Stop |
            Where-Object { [int]$_.ProcessId -ne [int]$PID }
    )
}

function Get-ValidatedPidMarkerIds {
    param([object[]]$Processes)

    $byId = @{}
    foreach ($process in $Processes) {
        $byId[[int]$process.ProcessId] = $process
    }
    $ids = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($marker in @(Get-ChildItem -LiteralPath $PidDirectory -Filter '*.pid' -File -ErrorAction SilentlyContinue)) {
        $markerPid = 0
        $raw = ""
        try {
            $raw = (Get-Content -LiteralPath $marker.FullName -Raw -ErrorAction Stop).Trim()
        } catch {
            continue
        }
        if (-not [int]::TryParse($raw, [ref]$markerPid) -or $markerPid -le 0) {
            continue
        }
        $process = $byId[$markerPid]
        if ($null -eq $process -or -not (Test-StackWorker $process)) {
            continue
        }
        try {
            $createdUtc = ([datetime]$process.CreationDate).ToUniversalTime()
            $markerDelaySeconds = ($marker.LastWriteTimeUtc - $createdUtc).TotalSeconds
        } catch {
            continue
        }
        # A PID marker is ownership evidence only when it was written by the
        # launcher immediately after that exact process was created. This
        # rejects stale files whose PID has since been reused.
        if ($markerDelaySeconds -lt -5 -or $markerDelaySeconds -gt 300) {
            continue
        }
        [void]$ids.Add($markerPid)
    }
    return @($ids)
}

function Get-OwnedTargetIds {
    param([object[]]$Processes)

    $ids = [System.Collections.Generic.HashSet[int]]::new()
    foreach ($process in $Processes) {
        if ((Test-StackWorker $process) -and (Test-RootOwnership $process)) {
            [void]$ids.Add([int]$process.ProcessId)
        }
    }
    foreach ($markerPid in @(Get-ValidatedPidMarkerIds -Processes $Processes)) {
        [void]$ids.Add([int]$markerPid)
    }
    return @($ids)
}

function Get-ListenerIds {
    if ($ports.Count -eq 0) {
        return @()
    }
    return @(
        Get-NetTCPConnection -State Listen -ErrorAction Stop |
            Where-Object { $ports -contains [int]$_.LocalPort } |
            Select-Object -ExpandProperty OwningProcess -Unique
    )
}

$initialProcesses = Get-ProcessSnapshot
$targetIds = @(Get-OwnedTargetIds -Processes $initialProcesses | Sort-Object -Unique)
foreach ($targetId in $targetIds) {
    try {
        Start-Process -FilePath $taskkill -ArgumentList @(
            '/F', '/T', '/PID', [string]$targetId
        ) -WindowStyle Hidden -Wait | Out-Null
    } catch {
        # Verification below is authoritative. A target may already have been
        # removed as a child of an earlier process-tree termination.
    }
}

$remainingTargets = @()
$remainingListeners = @()
for ($attempt = 0; $attempt -le $WaitSeconds; $attempt++) {
    $remainingTargets = @(Get-OwnedTargetIds -Processes (Get-ProcessSnapshot))
    $remainingListeners = @(Get-ListenerIds)
    if ($remainingTargets.Count -eq 0 -and $remainingListeners.Count -eq 0) {
        Write-Output "[stop] process-tree and listener verification passed."
        exit 0
    }
    if ($attempt -lt $WaitSeconds) {
        Start-Sleep -Seconds 1
    }
}

$targetText = ($remainingTargets | Sort-Object -Unique) -join ','
$listenerText = ($remainingListeners | Sort-Object -Unique) -join ','
Write-Error (
    "repo stack stop verification failed; remaining_workers=" + $targetText +
    "; remaining_listener_pids=" + $listenerText
)
exit 2
