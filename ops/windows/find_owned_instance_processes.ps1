# AGENT: ROLE: Select repo-owned Windows runtime/feature-push processes for one validated stack instance.
# AGENT: STATE / SIDE EFFECTS: read-only CIM or JSON snapshot inspection; emits matching process IDs only.
param(
    [Parameter(Mandatory = $true)]
    [string]$Root,

    [Parameter(Mandatory = $true)]
    [ValidateSet("runtime", "feature-push")]
    [string]$Role,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")]
    [string]$InstanceId,

    [int]$ProcessId = 0,

    [string]$SnapshotPath = ""
)

# Read-only process selector shared by the Windows runtime/feature-worker
# launchers. Keeping selection separate from termination makes the ownership
# and instance rules testable without starting or stopping any process.
$ErrorActionPreference = "Stop"
$resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
$normalizedInstance = $InstanceId.Trim().ToLowerInvariant()
$rootBoundaryPattern = "(?i)(?:^|[`"'\s=])" + [regex]::Escape($resolvedRoot) + "(?=$|[`"'\s\\/])"
$instancePattern = "(?i)(?:^|[`"'\s])--instance-id(?:=|\s+)" + [regex]::Escape($normalizedInstance) + "(?=[`"'\s]|$)"
$anyInstancePattern = "(?i)(?:^|[`"'\s])--instance-id(?:=|\s+)"

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

function Test-Role {
    param([object]$Process)

    $commandLine = [string]$Process.CommandLine
    if ($Role -eq "runtime") {
        return $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+runtime\s+run(?:\s|$)"
    }
    return (
        $commandLine -match "(?i)24_start_feature_push_worker\.bat.*\s--run(?:\s|$)" -or
        $commandLine -match "(?i)feature_push_worker_loop\.py" -or
        $commandLine -match "(?i)(?:-m\s+)?src\.trader\.cli\s+features\s+push-worker(?:\s|$)"
    )
}

function Test-Instance {
    param([object]$Process)

    $commandLine = [string]$Process.CommandLine
    if ($commandLine -match $instancePattern) {
        return $true
    }

    # Processes created before instance markers existed belong to the ordinary
    # baseline stack. Candidate/alternate launches must never claim them.
    return $normalizedInstance -eq "baseline" -and $commandLine -notmatch $anyInstancePattern
}

if ($SnapshotPath.Trim()) {
    $parsedProcesses = Get-Content -LiteralPath $SnapshotPath -Raw | ConvertFrom-Json
    $processes = @()
    foreach ($parsedProcess in $parsedProcesses) {
        $processes += $parsedProcess
    }
} else {
    $processes = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
}

$processes |
    Where-Object { $ProcessId -le 0 -or [int]$_.ProcessId -eq $ProcessId } |
    Where-Object { Test-RootOwnership $_ } |
    Where-Object { Test-Role $_ } |
    Where-Object { Test-Instance $_ } |
    ForEach-Object { [int]$_.ProcessId }
