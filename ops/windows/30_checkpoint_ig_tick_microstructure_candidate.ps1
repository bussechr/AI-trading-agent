[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [Parameter(Mandatory = $true)][string]$ApiKeyFile,
    [Parameter(Mandatory = $true)][string]$Preregistration,
    [Parameter(Mandatory = $true)][string]$StartReceipt,
    [Parameter(Mandatory = $true)][string]$ReadinessRoot,
    [Parameter(Mandatory = $true)][string]$CheckpointRoot,
    [string]$BaseUrl = "http://127.0.0.1:58710",
    [ValidateRange(1, 15)][int]$HttpTimeoutSeconds = 3,
    [string]$ExpectedWrapperSha256 = "",
    [string]$ExpectedReadinessToolSha256 = "",
    [string]$ExpectedContinuityToolSha256 = "",
    [string]$ExpectedSealerSha256 = "",
    [string]$ExpectedCaptureToolSha256 = "",
    [string]$ExpectedEnvSha256 = ""
)

# AGENT: ROLE: collection-only scheduled checkpoint wrapper for the two-cell tick candidate.
# AGENT: HANDSHAKE: exact local env + authenticated readiness -> append-only continuity chain.
# AGENT: ISOLATION: reads collection metadata only; no signal, outcome, runtime, command, or broker-order path.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$WrapperPath = [IO.Path]::GetFullPath($PSCommandPath)
$ReadinessTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\check_ig_tick_history_readiness.py"))
$ContinuityTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\check_ig_tick_microstructure_candidate_continuity.py"))
$SealerTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\seal_ig_tick_microstructure_candidate_preregistration.py"))
$CaptureTool = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "tools\capture_ig_scalp_cost_model.py"))
$EnvPath = [IO.Path]::GetFullPath((Join-Path $RepositoryRoot "ops\windows\_env.bat"))

function Resolve-ExistingFile {
    param([string]$Path, [string]$Reason)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw $Reason }
    return [IO.Path]::GetFullPath($item.FullName)
}

function Resolve-RepositoryDirectory {
    param([string]$Path, [string]$Reason)
    $full = [IO.Path]::GetFullPath($Path)
    $prefix = $RepositoryRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (-not $full.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw $Reason }
    [IO.Directory]::CreateDirectory($full) | Out-Null
    $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw $Reason }
    return $full
}

function Get-FileSha256 {
    param([string]$Path)
    return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
}

function Assert-Hash {
    param([string]$Path, [string]$Expected, [string]$Reason)
    if ($Expected -notmatch '^[0-9a-fA-F]{64}$' -or (Get-FileSha256 $Path) -ne $Expected.ToLowerInvariant()) { throw $Reason }
}

if (-not [string]::Equals($BaseUrl.TrimEnd('/'), "http://127.0.0.1:58710", [StringComparison]::Ordinal)) {
    throw "bridge_base_url_must_be_exact_127_0_0_1_58710"
}
$Python = Resolve-ExistingFile $PythonExe "python_executable_invalid"
$KeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
$Prereg = Resolve-ExistingFile $Preregistration "preregistration_invalid"
$Receipt = Resolve-ExistingFile $StartReceipt "start_receipt_invalid"
foreach ($sourcePath in @($WrapperPath, $ReadinessTool, $ContinuityTool, $SealerTool, $CaptureTool, $EnvPath)) {
    [void](Resolve-ExistingFile $sourcePath "checkpoint_source_invalid")
}
Assert-Hash $WrapperPath $ExpectedWrapperSha256 "checkpoint_wrapper_hash_mismatch"
Assert-Hash $ReadinessTool $ExpectedReadinessToolSha256 "readiness_tool_hash_mismatch"
Assert-Hash $ContinuityTool $ExpectedContinuityToolSha256 "continuity_tool_hash_mismatch"
Assert-Hash $SealerTool $ExpectedSealerSha256 "sealer_tool_hash_mismatch"
Assert-Hash $CaptureTool $ExpectedCaptureToolSha256 "capture_tool_hash_mismatch"
Assert-Hash $EnvPath $ExpectedEnvSha256 "env_source_hash_mismatch"
$ReadinessDirectory = Resolve-RepositoryDirectory $ReadinessRoot "readiness_root_outside_repository"
$CheckpointDirectory = Resolve-RepositoryDirectory $CheckpointRoot "checkpoint_root_outside_repository"

# Load the repository environment in-process without printing any value. The
# Scheduled Task stores only file paths and hashes, never the API key or DB URL.
$environmentLines = & $env:ComSpec /d /q /c ('call "' + $EnvPath + '" >nul && set')
if ($LASTEXITCODE -ne 0) { throw "windows_environment_load_failed" }
foreach ($line in $environmentLines) {
    $separator = $line.IndexOf('=')
    if ($separator -gt 0) {
        Set-Item -LiteralPath ("Env:" + $line.Substring(0, $separator)) -Value $line.Substring($separator + 1)
    }
}
if ([string]::IsNullOrWhiteSpace($env:FXSTACK_DATABASE_URL)) { throw "database_url_missing_after_env_load" }

$stamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ") + "_" + [Guid]::NewGuid().ToString("N")
$readinessPath = Join-Path $ReadinessDirectory ("ig_tick_history_readiness_continuity_" + $stamp + ".json")
& $Python $ReadinessTool `
    --base-url $BaseUrl `
    --api-key-file $KeyFile `
    --http-timeout-secs ([string]$HttpTimeoutSeconds) `
    --output $readinessPath | Out-Null
if ($LASTEXITCODE -ne 0) { throw "authenticated_readiness_checkpoint_failed" }

& $Python $ContinuityTool `
    --preregistration $Prereg `
    --start-receipt $Receipt `
    --readiness $readinessPath `
    --checkpoint-root $CheckpointDirectory
if ($LASTEXITCODE -ne 0) { throw "candidate_continuity_checkpoint_failed" }

