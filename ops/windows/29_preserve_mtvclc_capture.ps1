[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Preregistration,

    [Parameter(Mandatory = $true)]
    [string]$CaptureRoot,

    [Parameter(Mandatory = $true)]
    [string]$BackupRoot,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreregistrationSha256,

    [Parameter(Mandatory = $true)]
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$ExpectedGuardIdentitySha256,

    [ValidatePattern("^$|^[0-9a-fA-F]{64}$")]
    [string]$ExpectedPreservationScriptSha256 = "",

    [ValidatePattern("^[A-Za-z]$")]
    [string]$ExpectedCaptureDriveLetter = "D",

    [long]$WarningFreeBytes = 500GB,
    [long]$HardMinimumFreeBytes = 311GB,

    [ValidateRange(10.0, 86400.0)]
    [double]$MaximumJournalAgeSeconds = 120.0,

    [ValidateRange(60.0, 86400.0)]
    [double]$MaximumManifestAgeSeconds = 4200.0,

    [ValidateRange(0.0, 300.0)]
    [double]$MinimumClosedAgeSeconds = 120.0,

    [ValidateRange(0, 60)]
    [int]$StabilityProbeSeconds = 2,

    [ValidateRange(0.0, 300.0)]
    [double]$MaximumFutureClockSkewSeconds = 5.0
)

# AGENT: ROLE: collection-only durability monitor and append-only replica for one exact resilient MTVCLC capture tuple.
# AGENT: HANDSHAKE: exact external preregistration/capture identities -> metadata freshness/capacity gates -> separate-volume immutable-file replica.
# AGENT: ISOLATION: never parses preregistration, manifest, journal, chunk, signal, outcome, or performance records.
# AGENT: SIDE EFFECTS: creates only append-only files beneath the explicit backup root; never mutates sources or deletes/overwrites destination data.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StatusSchema = "fxstack.mtvclc_capture_durability_status.v1"
$ManifestName = "manifest.sha256.jsonl"
$ActiveJournalName = "active-hour.journal.sha256.jsonl"
$ChunksDirectoryName = "chunks"
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))

function Write-StatusAndExit {
    param([hashtable]$Payload, [int]$Code)

    $Payload["schema_version"] = $StatusSchema
    $Payload["collection_only"] = $true
    $Payload["metadata_only_monitoring"] = $true
    $Payload["evidence_rows_interpreted"] = $false
    $Payload["active_journal_copied"] = $false
    $Payload["active_journal_copy_authorized"] = $false
    $Payload["source_mutation_performed"] = $false
    $Payload["destination_delete_performed"] = $false
    $Payload["evaluation_performed"] = $false
    $Payload["signal_computation_authorized"] = $false
    $Payload["outcome_access_authorized"] = $false
    $Payload["performance_computation_authorized"] = $false
    $Payload["success_claim_authorized"] = $false
    $Payload["issuer_authorized"] = $false
    $Payload["signature_authorized"] = $false
    $Payload["authority"] = $false
    $Payload["authority_granted"] = $false
    $Payload["runtime_authorized"] = $false
    $Payload["activation_authorized"] = $false
    $Payload["broker_access_authorized"] = $false
    $Payload["order_authorized"] = $false
    Write-Output ($Payload | ConvertTo-Json -Compress -Depth 10)
    exit $Code
}

function Test-EqualOrDescendantPath {
    param([string]$Candidate, [string]$Parent)

    $candidatePath = [IO.Path]::GetFullPath($Candidate).TrimEnd('\', '/')
    $parentPath = [IO.Path]::GetFullPath($Parent).TrimEnd('\', '/')
    if ($candidatePath.Equals($parentPath, [StringComparison]::OrdinalIgnoreCase)) {
        return $true
    }
    return $candidatePath.StartsWith(
        $parentPath + [IO.Path]::DirectorySeparatorChar,
        [StringComparison]::OrdinalIgnoreCase
    )
}

function Assert-NoReparsePath {
    param([string]$Path)

    $currentPath = [IO.Path]::GetFullPath($Path)
    while ($true) {
        $currentItem = Get-Item -LiteralPath $currentPath -Force -ErrorAction Stop
        if ($currentItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "reparse_path_refused"
        }
        $parent = [IO.Directory]::GetParent($currentPath)
        if ($null -eq $parent) {
            break
        }
        $currentPath = $parent.FullName
    }
}

function Resolve-ExactFile {
    param([string]$Path, [string]$FailureReason)

    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw $FailureReason
    }
    try {
        $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    }
    catch {
        throw $FailureReason
    }
    if ($item.PSIsContainer) {
        throw $FailureReason
    }
    Assert-NoReparsePath $item.FullName
    return $item
}

function Resolve-ExactDirectory {
    param([string]$Path, [string]$FailureReason)

    if (-not [IO.Path]::IsPathRooted($Path)) {
        throw $FailureReason
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if (-not $item.PSIsContainer) {
        throw $FailureReason
    }
    Assert-NoReparsePath $item.FullName
    return $item
}

function Get-FileSha256 {
    param([string]$Path)

    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::Read
    )
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Get-FileSnapshot {
    param([string]$Path)

    $before = Resolve-ExactFile $Path "source_file_invalid"
    $beforeLength = [long]$before.Length
    $beforeWriteTicks = [long]$before.LastWriteTimeUtc.Ticks
    $sha256 = Get-FileSha256 $before.FullName
    $after = Resolve-ExactFile $Path "source_file_invalid"
    if (
        [long]$after.Length -ne $beforeLength -or
        [long]$after.LastWriteTimeUtc.Ticks -ne $beforeWriteTicks
    ) {
        throw "source_file_changed_during_identity_probe"
    }
    return [pscustomobject]@{
        FullName = [string]$after.FullName
        Length = $beforeLength
        LastWriteTimeUtcTicks = $beforeWriteTicks
        Sha256 = $sha256
    }
}

function Test-SnapshotStillMatches {
    param([object]$Snapshot)

    try {
        $item = Resolve-ExactFile ([string]$Snapshot.FullName) "source_file_invalid"
        if (
            [long]$item.Length -ne [long]$Snapshot.Length -or
            [long]$item.LastWriteTimeUtc.Ticks -ne [long]$Snapshot.LastWriteTimeUtcTicks
        ) {
            return $false
        }
        return (Get-FileSha256 $item.FullName) -eq [string]$Snapshot.Sha256
    }
    catch {
        return $false
    }
}

function Ensure-AppendOnlyDirectory {
    param([string]$Path, [string]$Boundary)

    $fullPath = [IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $fullBoundary = [IO.Path]::GetFullPath($Boundary).TrimEnd('\', '/')
    if (-not (Test-EqualOrDescendantPath $fullPath $fullBoundary)) {
        throw "backup_path_escaped_explicit_root"
    }
    if (Test-Path -LiteralPath $fullPath) {
        $existing = Resolve-ExactDirectory $fullPath "backup_directory_invalid"
        return [string]$existing.FullName
    }
    $parentPath = [IO.Directory]::GetParent($fullPath)
    if ($null -eq $parentPath) {
        throw "backup_directory_parent_missing"
    }
    $null = Ensure-AppendOnlyDirectory $parentPath.FullName $fullBoundary
    try {
        $null = New-Item -ItemType Directory -Path $fullPath -ErrorAction Stop
    }
    catch {
        if (-not (Test-Path -LiteralPath $fullPath -PathType Container)) {
            throw
        }
    }
    $created = Resolve-ExactDirectory $fullPath "backup_directory_invalid"
    return [string]$created.FullName
}

function Copy-AppendOnlyStableFile {
    param(
        [object]$Snapshot,
        [string]$Destination,
        [string]$TupleRoot
    )

    $destinationPath = [IO.Path]::GetFullPath($Destination)
    if (-not (Test-EqualOrDescendantPath $destinationPath $TupleRoot)) {
        throw "backup_file_escaped_tuple_root"
    }
    $destinationParent = [IO.Directory]::GetParent($destinationPath)
    if ($null -eq $destinationParent) {
        throw "backup_file_parent_missing"
    }
    $null = Ensure-AppendOnlyDirectory $destinationParent.FullName $TupleRoot

    if (Test-Path -LiteralPath $destinationPath) {
        $existing = Resolve-ExactFile $destinationPath "backup_existing_file_invalid"
        if (
            [long]$existing.Length -ne [long]$Snapshot.Length -or
            (Get-FileSha256 $existing.FullName) -ne [string]$Snapshot.Sha256
        ) {
            throw "backup_existing_file_identity_mismatch"
        }
        if (-not (Test-SnapshotStillMatches $Snapshot)) {
            throw "source_file_changed_during_copy"
        }
        return [pscustomobject]@{
            Action = "reused"
            Bytes = [long]$Snapshot.Length
        }
    }

    $incomingPath = $destinationPath + ".incoming." + ([Guid]::NewGuid().ToString("N"))
    $null = Copy-Item -LiteralPath ([string]$Snapshot.FullName) -Destination $incomingPath -ErrorAction Stop
    $incoming = Resolve-ExactFile $incomingPath "backup_incoming_file_invalid"
    if (
        [long]$incoming.Length -ne [long]$Snapshot.Length -or
        (Get-FileSha256 $incoming.FullName) -ne [string]$Snapshot.Sha256
    ) {
        throw "backup_copy_identity_mismatch"
    }
    if (-not (Test-SnapshotStillMatches $Snapshot)) {
        throw "source_file_changed_during_copy"
    }
    if (Test-Path -LiteralPath $destinationPath) {
        throw "backup_destination_appeared_during_copy"
    }
    $null = Move-Item -LiteralPath $incomingPath -Destination $destinationPath -ErrorAction Stop
    $final = Resolve-ExactFile $destinationPath "backup_final_file_invalid"
    if (
        [long]$final.Length -ne [long]$Snapshot.Length -or
        (Get-FileSha256 $final.FullName) -ne [string]$Snapshot.Sha256
    ) {
        throw "backup_final_identity_mismatch"
    }
    return [pscustomobject]@{
        Action = "copied"
        Bytes = [long]$Snapshot.Length
    }
}

function Get-MetadataAgeSeconds {
    param([IO.FileSystemInfo]$Item, [DateTime]$NowUtc)

    return ($NowUtc - $Item.LastWriteTimeUtc).TotalSeconds
}

try {
    $preservationScriptSha256 = Get-FileSha256 $PSCommandPath
    if (
        -not [string]::IsNullOrWhiteSpace($ExpectedPreservationScriptSha256) -and
        $preservationScriptSha256 -ne $ExpectedPreservationScriptSha256.ToLowerInvariant()
    ) {
        throw "preservation_script_sha256_mismatch"
    }
    if ($WarningFreeBytes -le 0 -or $HardMinimumFreeBytes -le 0) {
        throw "free_space_threshold_invalid"
    }
    if ($WarningFreeBytes -lt $HardMinimumFreeBytes) {
        throw "warning_threshold_below_hard_minimum"
    }

    $preregItem = Resolve-ExactFile $Preregistration "preregistration_path_invalid"
    $captureItem = Resolve-ExactDirectory $CaptureRoot "capture_root_invalid"
    $backupItem = Resolve-ExactDirectory $BackupRoot "backup_root_must_exist_and_be_explicit"

    if (
        (Test-EqualOrDescendantPath $preregItem.FullName $RepositoryRoot) -or
        (Test-EqualOrDescendantPath $captureItem.FullName $RepositoryRoot) -or
        (Test-EqualOrDescendantPath $backupItem.FullName $RepositoryRoot)
    ) {
        throw "repository_path_refused"
    }
    if (
        (Test-EqualOrDescendantPath $backupItem.FullName $captureItem.FullName) -or
        (Test-EqualOrDescendantPath $captureItem.FullName $backupItem.FullName) -or
        (Test-EqualOrDescendantPath $backupItem.FullName $preregItem.Directory.FullName)
    ) {
        throw "backup_root_not_separate_from_source_tuple"
    }

    $legacyPreregMatch = [regex]::Match(
        $preregItem.Name,
        "^mtvclc_v1_preregistration_([0-9a-f]{64})\.json$",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    $gapV3PreregMatch = [regex]::Match(
        $preregItem.Name,
        "^mtvclc_gap_v3_preregistration_([0-9a-f]{64})\.json$",
        [Text.RegularExpressions.RegexOptions]::IgnoreCase
    )
    if ($legacyPreregMatch.Success) {
        $preregMatch = $legacyPreregMatch
        $allowedPreregistrationParents = @(
            "mtvclc_prereg_sealed_resilient_v1",
            "mtvclc_prereg_sealed_watermark_v2"
        )
        $captureLeafPrefix = "mtvclc_prospective_capture_"
        $GuardIdentityName = "collector-guard.identity.resilient.v1.json"
        $PreservationFilenameSchemaVersion = "legacy_resilient_filename_contract"
    }
    elseif ($gapV3PreregMatch.Success) {
        $preregMatch = $gapV3PreregMatch
        $captureLeafPrefix = "mtvclc_prospective_capture_gap_v3_"
        if ($preregItem.Directory.Name.Equals(
            "mtvclc_prereg_sealed_runtime_bound_v5",
            [StringComparison]::OrdinalIgnoreCase
        )) {
            $allowedPreregistrationParents = @(
                "mtvclc_prereg_sealed_runtime_bound_v5"
            )
            $GuardIdentityName = "collector-guard.identity.gap-v5.v1.json"
            $PreservationFilenameSchemaVersion = (
                "fxstack.scalp.mtvclc_preservation_filenames.v5"
            )
        }
        else {
            $allowedPreregistrationParents = @(
                "mtvclc_prereg_sealed_runtime_bound_v3",
                "mtvclc_prereg_sealed_runtime_bound_v4"
            )
            $GuardIdentityName = "collector-guard.identity.gap-v3.v1.json"
            $PreservationFilenameSchemaVersion = (
                "fxstack.scalp.mtvclc_preservation_filenames.v3"
            )
        }
    }
    else {
        throw "preregistration_filename_not_exact_resilient_contract"
    }
    if (-not ($allowedPreregistrationParents | Where-Object {
        $preregItem.Directory.Name.Equals($_, [StringComparison]::OrdinalIgnoreCase)
    })) {
        throw "preregistration_parent_not_exact_resilient_contract"
    }
    $captureLeafExpected = $captureLeafPrefix + $preregMatch.Groups[1].Value.Substring(0, 16)
    if (-not $captureItem.Name.Equals($captureLeafExpected, [StringComparison]::OrdinalIgnoreCase)) {
        throw "preregistration_capture_tuple_mismatch"
    }

    $captureVolume = Get-Volume -FilePath $captureItem.FullName -ErrorAction Stop
    $expectedDrive = $ExpectedCaptureDriveLetter.ToUpperInvariant()
    if ([string]$captureVolume.DriveLetter -ne $expectedDrive) {
        throw "capture_volume_drive_letter_mismatch"
    }
    $backupVolumeIdentity = ""
    try {
        $backupVolume = Get-Volume -FilePath $backupItem.FullName -ErrorAction Stop
        $backupVolumeIdentity = [string]$backupVolume.UniqueId
    }
    catch {
        $backupRootPath = [IO.Path]::GetPathRoot($backupItem.FullName)
        if ([string]::IsNullOrWhiteSpace($backupRootPath) -or -not $backupRootPath.StartsWith("\\")) {
            throw "backup_volume_identity_unavailable"
        }
        $backupVolumeIdentity = $backupRootPath.ToLowerInvariant()
    }
    if ([string]$captureVolume.UniqueId -eq $backupVolumeIdentity) {
        throw "backup_volume_must_differ_from_capture_volume"
    }

    $freeBytes = [long]$captureVolume.SizeRemaining
    if ($freeBytes -lt $HardMinimumFreeBytes) {
        throw "capture_volume_free_space_below_hard_minimum"
    }
    $capacityStatus = if ($freeBytes -lt $WarningFreeBytes) { "warning" } else { "healthy" }

    $guardItem = Resolve-ExactFile (
        Join-Path $captureItem.FullName $GuardIdentityName
    ) "guard_identity_missing_or_invalid"
    $manifestItem = Resolve-ExactFile (
        Join-Path $captureItem.FullName $ManifestName
    ) "manifest_missing_or_invalid"
    $journalItem = Resolve-ExactFile (
        Join-Path $captureItem.FullName $ActiveJournalName
    ) "active_journal_missing_or_invalid"
    $chunksItem = Resolve-ExactDirectory (
        Join-Path $captureItem.FullName $ChunksDirectoryName
    ) "chunks_directory_missing_or_invalid"

    $preregSnapshot = Get-FileSnapshot $preregItem.FullName
    $guardSnapshot = Get-FileSnapshot $guardItem.FullName
    $expectedPreregHash = $ExpectedPreregistrationSha256.ToLowerInvariant()
    $expectedGuardHash = $ExpectedGuardIdentitySha256.ToLowerInvariant()
    if ([string]$preregSnapshot.Sha256 -ne $expectedPreregHash) {
        throw "preregistration_sha256_mismatch"
    }
    if ([string]$guardSnapshot.Sha256 -ne $expectedGuardHash) {
        throw "guard_identity_sha256_mismatch"
    }

    $nowUtc = [DateTime]::UtcNow
    $journalAgeSeconds = Get-MetadataAgeSeconds $journalItem $nowUtc
    $manifestAgeSeconds = Get-MetadataAgeSeconds $manifestItem $nowUtc
    if (
        $journalAgeSeconds -lt -$MaximumFutureClockSkewSeconds -or
        $manifestAgeSeconds -lt -$MaximumFutureClockSkewSeconds
    ) {
        throw "capture_metadata_timestamp_in_future"
    }
    if ($journalItem.Length -le 0 -or $journalAgeSeconds -gt $MaximumJournalAgeSeconds) {
        throw "active_journal_metadata_stale"
    }
    if ($manifestItem.Length -le 0 -or $manifestAgeSeconds -gt $MaximumManifestAgeSeconds) {
        throw "manifest_metadata_stale"
    }

    $closedChunkSnapshots = @()
    $recentChunkCount = 0
    $chunkInventory = @{}
    $chunkEntries = @(Get-ChildItem -LiteralPath $chunksItem.FullName -Force -Recurse -ErrorAction Stop)
    foreach ($entry in $chunkEntries) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "chunk_reparse_path_refused"
        }
        $inventoryKey = $entry.FullName.ToLowerInvariant()
        $chunkInventory[$inventoryKey] = if ($entry.PSIsContainer) {
            "directory"
        }
        else {
            "file|{0}|{1}" -f ([long]$entry.Length), ([long]$entry.LastWriteTimeUtc.Ticks)
        }
        if ($entry.PSIsContainer) {
            if (
                -not $entry.Parent.FullName.Equals($chunksItem.FullName, [StringComparison]::OrdinalIgnoreCase) -or
                $entry.Name -notmatch "^[0-9]{8}T[0-9]{2}$"
            ) {
                throw "unexpected_chunk_directory_topology"
            }
            continue
        }
        if (
            -not $entry.Directory.Parent.FullName.Equals($chunksItem.FullName, [StringComparison]::OrdinalIgnoreCase) -or
            $entry.Directory.Name -notmatch "^[0-9]{8}T[0-9]{2}$" -or
            $entry.Name -notmatch "^ig-mt4-m1-activity-s[0-9]{4}-q[0-9]{10}\.json$"
        ) {
            throw "unexpected_chunk_file_topology"
        }
        $chunkAgeSeconds = Get-MetadataAgeSeconds $entry $nowUtc
        if ($chunkAgeSeconds -lt -$MaximumFutureClockSkewSeconds) {
            throw "chunk_timestamp_in_future"
        }
        if ($chunkAgeSeconds -lt $MinimumClosedAgeSeconds) {
            $recentChunkCount += 1
            continue
        }
        if ($entry.Length -le 0) {
            throw "closed_chunk_empty"
        }
        $closedChunkSnapshots += Get-FileSnapshot $entry.FullName
    }

    if ($StabilityProbeSeconds -gt 0) {
        Start-Sleep -Seconds $StabilityProbeSeconds
    }
    foreach ($snapshot in $closedChunkSnapshots) {
        if (-not (Test-SnapshotStillMatches $snapshot)) {
            throw "closed_chunk_changed_during_stability_probe"
        }
    }

    $manifestSnapshot = Get-FileSnapshot $manifestItem.FullName
    $chunkEntriesAfterManifest = @(
        Get-ChildItem -LiteralPath $chunksItem.FullName -Force -Recurse -ErrorAction Stop
    )
    if ($chunkEntriesAfterManifest.Count -ne $chunkInventory.Count) {
        throw "chunk_topology_changed_during_manifest_probe"
    }
    foreach ($entry in $chunkEntriesAfterManifest) {
        if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "chunk_reparse_path_refused"
        }
        $inventoryKey = $entry.FullName.ToLowerInvariant()
        $inventoryValue = if ($entry.PSIsContainer) {
            "directory"
        }
        else {
            "file|{0}|{1}" -f ([long]$entry.Length), ([long]$entry.LastWriteTimeUtc.Ticks)
        }
        if (-not $chunkInventory.ContainsKey($inventoryKey)) {
            throw "chunk_topology_changed_during_manifest_probe"
        }
        if ([string]$chunkInventory[$inventoryKey] -ne $inventoryValue) {
            throw "chunk_topology_changed_during_manifest_probe"
        }
    }
    $tupleName = "{0}__p{1}__g{2}" -f (
        $captureItem.Name,
        $expectedPreregHash.Substring(0, 16),
        $expectedGuardHash.Substring(0, 16)
    )
    $tupleRoot = Join-Path $backupItem.FullName $tupleName
    $tupleRoot = Ensure-AppendOnlyDirectory $tupleRoot $backupItem.FullName
    $lockPath = Join-Path $tupleRoot ".durability-replicator.lock"
    $lockStream = [IO.File]::Open(
        $lockPath,
        [IO.FileMode]::OpenOrCreate,
        [IO.FileAccess]::ReadWrite,
        [IO.FileShare]::None
    )
    try {
        $results = @()
        $results += Copy-AppendOnlyStableFile `
            $preregSnapshot `
            (Join-Path (Join-Path $tupleRoot "preregistration") $preregItem.Name) `
            $tupleRoot
        $results += Copy-AppendOnlyStableFile `
            $guardSnapshot `
            (Join-Path (Join-Path $tupleRoot "metadata") $GuardIdentityName) `
            $tupleRoot

        foreach ($snapshot in $closedChunkSnapshots) {
            $relativePath = ([string]$snapshot.FullName).Substring($chunksItem.FullName.Length).TrimStart('\', '/')
            $results += Copy-AppendOnlyStableFile `
                $snapshot `
                (Join-Path (Join-Path $tupleRoot $ChunksDirectoryName) $relativePath) `
                $tupleRoot
        }

        if ($recentChunkCount -eq 0) {
            $manifestSnapshotName = "manifest.sha256.{0}.jsonl" -f $manifestSnapshot.Sha256
            $results += Copy-AppendOnlyStableFile `
                $manifestSnapshot `
                (Join-Path (Join-Path $tupleRoot "metadata") $manifestSnapshotName) `
                $tupleRoot
        }
    }
    finally {
        $lockStream.Dispose()
    }

    $copied = @($results | Where-Object { $_.Action -eq "copied" })
    $reused = @($results | Where-Object { $_.Action -eq "reused" })
    $copiedBytes = [long]0
    foreach ($copyResult in $copied) {
        $copiedBytes += [long]$copyResult.Bytes
    }
    $status = if ($recentChunkCount -gt 0) {
        "replication_waiting_for_closed_chunks"
    }
    elseif ($capacityStatus -eq "warning") {
        "replicated_with_capacity_warning"
    }
    else {
        "replication_current"
    }
    Write-StatusAndExit @{
        status = $status
        preregistration = $preregItem.FullName
        capture_root = $captureItem.FullName
        backup_root = $backupItem.FullName
        backup_tuple_root = $tupleRoot
        preregistration_sha256 = $expectedPreregHash
        guard_identity_sha256 = $expectedGuardHash
        preservation_filename_schema_version = $PreservationFilenameSchemaVersion
        guard_identity_filename = $GuardIdentityName
        preservation_script_sha256 = $preservationScriptSha256
        capture_volume_drive_letter = [string]$captureVolume.DriveLetter
        capture_volume_free_bytes = $freeBytes
        warning_free_bytes = $WarningFreeBytes
        hard_minimum_free_bytes = $HardMinimumFreeBytes
        capacity_status = $capacityStatus
        manifest_age_seconds = [Math]::Round($manifestAgeSeconds, 3)
        active_journal_age_seconds = [Math]::Round($journalAgeSeconds, 3)
        closed_chunk_count = $closedChunkSnapshots.Count
        recent_chunk_skipped_count = $recentChunkCount
        manifest_snapshot_deferred = ($recentChunkCount -gt 0)
        files_copied = $copied.Count
        files_reused = $reused.Count
        bytes_copied = $copiedBytes
    } 0
}
catch {
    Write-StatusAndExit @{
        status = "durability_refused"
        reason = $_.Exception.Message
    } 2
}
