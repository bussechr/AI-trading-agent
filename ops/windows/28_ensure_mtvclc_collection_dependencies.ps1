[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$PythonExe,

    [Parameter(Mandatory = $true)]
    [string]$TerminalExe,

    [Parameter(Mandatory = $true)]
    [string]$ApiKeyFile,

    [string]$BaseUrl = "http://127.0.0.1:58710",

    [ValidateRange(10, 300)]
    [int]$ReadinessTimeoutSeconds = 90,

    [ValidateRange(1, 15)]
    [int]$HttpTimeoutSeconds = 3,

    [string]$ExpectedEnsureSha256 = "",
    [string]$ExpectedBridgeLauncherSha256 = "",
    [string]$ExpectedMt4LauncherSha256 = "",
    [string]$ExpectedEnvSha256 = "",

    [switch]$AuditOnly
)

# AGENT: ROLE: collection-only dependency bootstrap for the sealed MTVCLC capture.
# AGENT: HANDSHAKE: exact repo bridge on 127.0.0.1:58710 -> visible configured IG MT4 -> authenticated exact-22 readiness.
# AGENT: ISOLATION: reads one local API-key file for GET probes, never emits it, and has no runtime, command, evaluation, or trading path.
# AGENT: SIDE EFFECTS: when not AuditOnly, starts only an absent bridge through the absent-only launcher and an absent configured terminal.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$StatusSchema = "fxstack.mtvclc_collection_dependencies_watchdog.v1"
$ExpectedBaseUrl = "http://127.0.0.1:58710"
$ExpectedBridgeHost = "127.0.0.1"
$ExpectedBridgePort = 58710
$ExpectedSymbols = @(
    "EURUSD", "USDJPY", "AUDUSD", "GBPUSD", "USDCAD", "USDCHF",
    "EURGBP", "EURJPY", "NZDUSD", "AUDJPY", "CADJPY", "CHFJPY",
    "EURAUD", "EURCAD", "EURCHF", "GBPCAD", "GBPCHF", "GBPJPY",
    "BTCUSD", "ETHUSD", "AUDCAD", "NZDJPY"
)
$RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$BridgeLauncherPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\20_start_bridge.bat")
)
$Mt4LauncherPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\19_start_mt4.ps1")
)
$EnvPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "ops\windows\_env.bat")
)
$BridgePidPath = [IO.Path]::GetFullPath(
    (Join-Path $RepositoryRoot "logs\bridge_58710.pid")
)
$WindowsPowerShell = [IO.Path]::GetFullPath(
    (Join-Path $env:WINDIR "System32\WindowsPowerShell\v1.0\powershell.exe")
)
$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$CurrentUserSid = $CurrentIdentity.User.Value

function Write-StatusAndExit {
    param([hashtable]$Payload, [int]$Code)
    $Payload["schema_version"] = $StatusSchema
    $Payload["collection_only"] = $true
    $Payload["dependency_bootstrap_only"] = $true
    $Payload["runtime_start_attempted"] = $false
    $Payload["runtime_authorized"] = $false
    $Payload["activation_authorized"] = $false
    $Payload["command_authorized"] = $false
    $Payload["trading_authorized"] = $false
    $Payload["healthy_process_stop_attempted"] = $false
    $Payload["foreign_process_stop_attempted"] = $false
    $Payload["api_key_emitted"] = $false
    Write-Output ($Payload | ConvertTo-Json -Compress -Depth 6)
    exit $Code
}

function Resolve-ExistingFile {
    param([string]$Path, [string]$Reason)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw $Reason
    }
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw $Reason
    }
    return [IO.Path]::GetFullPath($item.FullName)
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

function Test-PathWithinRepository {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) {
        return $false
    }
    try {
        $candidate = [IO.Path]::GetFullPath($Path)
        $prefix = $RepositoryRoot.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
        return $candidate.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)
    }
    catch {
        return $false
    }
}

function Get-FileSha256 {
    param([string]$Path)
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        return ([BitConverter]::ToString($sha256.ComputeHash($stream))).Replace("-", "").ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
        $stream.Dispose()
    }
}

function Assert-ExpectedHash {
    param([string]$Path, [string]$Expected, [string]$Reason)
    if ([string]::IsNullOrWhiteSpace($Expected)) {
        return
    }
    if ($Expected -notmatch "^[0-9a-fA-F]{64}$") {
        throw "dependency_expected_source_hash_invalid"
    }
    if ((Get-FileSha256 $Path) -ne $Expected.ToLowerInvariant()) {
        throw $Reason
    }
}

function Read-ApiKey {
    param([string]$Path)
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if ($item.Length -lt 32 -or $item.Length -gt 4096) {
        throw "bridge_api_key_file_size_invalid"
    }
    $value = [IO.File]::ReadAllText($Path).Trim()
    if (
        $value.Length -lt 32 -or
        $value.Length -gt 4096 -or
        $value.Contains([char]10) -or
        $value.Contains([char]13)
    ) {
        throw "bridge_api_key_file_value_invalid"
    }
    return $value
}

function Get-ProcessOwnerSid {
    param([object]$Process)
    try {
        $owner = Invoke-CimMethod -InputObject $Process -MethodName GetOwner -ErrorAction Stop
        if ([int]$owner.ReturnValue -ne 0) {
            return ""
        }
        $account = [Security.Principal.NTAccount]::new(
            ([string]$owner.Domain),
            ([string]$owner.User)
        )
        return $account.Translate([Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        return ""
    }
}

function Test-BridgeCommandLine {
    param([string]$CommandLine)
    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }
    return (
        $CommandLine -match '(?i)(^|\s)-m\s+uvicorn\s+fxstack\.api\.app:app(?:\s|$)' -and
        $CommandLine -match '(?i)(^|\s)--loop\s+asyncio:SelectorEventLoop(?:\s|$)' -and
        $CommandLine -match '(?i)(^|\s)--host\s+127\.0\.0\.1(?:\s|$)' -and
        $CommandLine -match '(?i)(^|\s)--port\s+58710(?:\s|$)'
    )
}

function Get-TerminalState {
    param([string]$ExpectedTerminal)
    $terminals = @(
        Get-CimInstance Win32_Process -Filter "Name='terminal.exe'" -ErrorAction SilentlyContinue
    )
    if ($terminals.Count -gt 1) {
        throw "multiple_mt4_terminal_processes_refused"
    }
    if ($terminals.Count -eq 0) {
        return [pscustomobject]@{ Present = $false; ProcessId = 0 }
    }
    $terminal = $terminals[0]
    if (-not (Test-SamePath ([string]$terminal.ExecutablePath) $ExpectedTerminal)) {
        throw "foreign_or_unconfigured_mt4_terminal_refused"
    }
    if ((Get-ProcessOwnerSid $terminal) -ne $CurrentUserSid) {
        throw "mt4_terminal_owner_identity_mismatch"
    }
    return [pscustomobject]@{
        Present = $true
        ProcessId = [int]$terminal.ProcessId
    }
}

function Test-InteractiveDesktopAvailable {
    try {
        $sessionId = (Get-Process -Id $PID -ErrorAction Stop).SessionId
        if ([int]$sessionId -eq 0) {
            return $false
        }
        $explorers = @(
            Get-CimInstance Win32_Process -Filter "Name='explorer.exe'" -ErrorAction SilentlyContinue |
                Where-Object { [int]$_.SessionId -eq [int]$sessionId }
        )
        return $explorers.Count -gt 0
    }
    catch {
        return $false
    }
}

function Get-BridgeListenerState {
    param([string]$ExpectedPython)
    $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $byPid = @{}
    foreach ($process in $processes) {
        $byPid[[int]$process.ProcessId] = $process
    }
    $bridgeCandidates = @(
        $processes | Where-Object { Test-BridgeCommandLine ([string]$_.CommandLine) }
    )
    $listeners = @(
        Get-NetTCPConnection `
            -State Listen `
            -LocalPort $ExpectedBridgePort `
            -ErrorAction SilentlyContinue
    )
    if ($listeners.Count -eq 0) {
        if ($bridgeCandidates.Count -gt 0) {
            throw "bridge_process_without_exact_listener_refused"
        }
        if (Test-Path -LiteralPath $BridgePidPath -PathType Leaf) {
            $markerItem = Get-Item -LiteralPath $BridgePidPath -Force -ErrorAction Stop
            if ($markerItem.Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "bridge_pid_marker_reparse_point_refused"
            }
            # A PID marker intentionally survives an unclean reboot. With no
            # listener and no matching bridge command it is stale, even if
            # Windows has reused the numeric PID for an unrelated process.
        }
        return [pscustomobject]@{ Present = $false; ProcessId = 0 }
    }
    if ($listeners.Count -ne 1) {
        throw "bridge_listener_count_mismatch"
    }
    $listener = $listeners[0]
    if ([string]$listener.LocalAddress -ne $ExpectedBridgeHost) {
        throw "bridge_listener_not_exact_loopback"
    }
    $listenerPid = [int]$listener.OwningProcess
    if (-not $byPid.ContainsKey($listenerPid)) {
        throw "bridge_listener_process_missing"
    }
    $listenerProcess = $byPid[$listenerPid]
    if (-not (Test-BridgeCommandLine ([string]$listenerProcess.CommandLine))) {
        throw "bridge_listener_command_identity_mismatch"
    }
    if ((Get-ProcessOwnerSid $listenerProcess) -ne $CurrentUserSid) {
        throw "bridge_listener_owner_identity_mismatch"
    }

    $chain = @()
    $cursor = $listenerPid
    $seen = @{}
    for ($depth = 0; $depth -lt 12 -and $cursor -gt 0; $depth++) {
        if ($seen.ContainsKey($cursor) -or -not $byPid.ContainsKey($cursor)) {
            break
        }
        $seen[$cursor] = $true
        $node = $byPid[$cursor]
        $chain += $node
        $cursor = [int]$node.ParentProcessId
    }
    $pythonIdentityPresent = $false
    $repositoryIdentityPresent = $false
    foreach ($node in $chain) {
        if (Test-SamePath ([string]$node.ExecutablePath) $ExpectedPython) {
            $pythonIdentityPresent = $true
        }
        if (Test-PathWithinRepository ([string]$node.ExecutablePath)) {
            $repositoryIdentityPresent = $true
        }
    }
    if (-not $pythonIdentityPresent -or -not $repositoryIdentityPresent) {
        throw "bridge_listener_repository_ownership_mismatch"
    }
    $chainIds = @($chain | ForEach-Object { [int]$_.ProcessId })
    foreach ($candidate in $bridgeCandidates) {
        if ($chainIds -notcontains [int]$candidate.ProcessId) {
            throw "independent_bridge_process_identity_mismatch"
        }
    }

    $marker = Resolve-ExistingFile $BridgePidPath "bridge_pid_marker_missing_or_invalid"
    $markerText = [IO.File]::ReadAllText($marker).Trim()
    if ($markerText -notmatch '^\d{1,10}$' -or [int]$markerText -ne $listenerPid) {
        throw "bridge_pid_marker_identity_mismatch"
    }
    return [pscustomobject]@{
        Present = $true
        ProcessId = $listenerPid
    }
}

function Invoke-AuthenticatedJsonGet {
    param([string]$Path, [string]$ApiKey)
    try {
        $body = Invoke-RestMethod `
            -Method Get `
            -Uri ($ExpectedBaseUrl + $Path) `
            -Headers @{ "X-API-Key" = $ApiKey } `
            -TimeoutSec $HttpTimeoutSeconds `
            -ErrorAction Stop
        return [pscustomobject]@{
            Success = $true
            StatusCode = 200
            Body = $body
        }
    }
    catch {
        $statusCode = 0
        try {
            if ($null -ne $_.Exception.Response) {
                $statusCode = [int]$_.Exception.Response.StatusCode
            }
        }
        catch {
            $statusCode = 0
        }
        return [pscustomobject]@{
            Success = $false
            StatusCode = $statusCode
            Body = $null
        }
    }
}

function Test-BridgeCoreReady {
    param([string]$ApiKey)
    $handshake = Invoke-AuthenticatedJsonGet "/v2/handshake" $ApiKey
    if (-not $handshake.Success) {
        return $false
    }
    if (
        [string]$handshake.Body.server -ne "fxstack-bridge" -or
        $handshake.Body.auth_required -ne $true -or
        [string]$handshake.Body.protocol_version -notmatch '^v3\.'
    ) {
        throw "bridge_handshake_identity_mismatch"
    }
    $ready = Invoke-AuthenticatedJsonGet "/v2/ready" $ApiKey
    if (@(401, 403) -contains [int]$ready.StatusCode) {
        throw "bridge_api_key_identity_mismatch"
    }
    if (-not $ready.Success) {
        return $false
    }
    return ($ready.Body.bridge_up -eq $true -and $ready.Body.database_ok -eq $true)
}

function Test-ExactCollectionReadiness {
    param([string]$ApiKey)
    if (-not (Test-BridgeCoreReady $ApiKey)) {
        return $false
    }
    $ready = Invoke-AuthenticatedJsonGet "/v2/ready" $ApiKey
    $state = Invoke-AuthenticatedJsonGet "/v2/state" $ApiKey
    foreach ($response in @($ready, $state)) {
        if (@(401, 403) -contains [int]$response.StatusCode) {
            throw "bridge_api_key_identity_mismatch"
        }
        if (-not $response.Success) {
            return $false
        }
    }
    if (
        $ready.Body.bridge_up -ne $true -or
        $ready.Body.database_ok -ne $true -or
        $ready.Body.mt4_fresh -ne $true -or
        [string]$ready.Body.mt4_status -ne "connected"
    ) {
        return $false
    }
    if (
        [string]$state.Body.broker_venue_id -ne "ig_mt4" -or
        [string]$state.Body.broker_account_mode -ne "demo" -or
        [string]$state.Body.broker_server -ne "IG-DEMO" -or
        [string]$state.Body.broker_company -ne "IG Group Limited" -or
        [string]::IsNullOrWhiteSpace([string]$state.Body.bridge_producer_instance_id) -or
        [int]$state.Body.symbol_ready_count -ne $ExpectedSymbols.Count -or
        [int]$state.Body.broker_symbol_ready_count -ne $ExpectedSymbols.Count -or
        @($state.Body.unsupported_pairs).Count -ne 0
    ) {
        return $false
    }
    $actualSymbols = @($state.Body.symbol_readiness.PSObject.Properties.Name)
    if ($actualSymbols.Count -ne $ExpectedSymbols.Count) {
        return $false
    }
    for ($index = 0; $index -lt $ExpectedSymbols.Count; $index++) {
        if (-not [string]::Equals(
            [string]$actualSymbols[$index],
            [string]$ExpectedSymbols[$index],
            [StringComparison]::Ordinal
        )) {
            return $false
        }
    }
    return $true
}

try {
    if (-not [string]::Equals($BaseUrl.TrimEnd('/'), $ExpectedBaseUrl, [StringComparison]::Ordinal)) {
        throw "bridge_base_url_must_be_exact_127_0_0_1_58710"
    }
    $ResolvedPython = Resolve-ExistingFile $PythonExe "python_executable_invalid"
    $ResolvedTerminal = Resolve-ExistingFile $TerminalExe "mt4_terminal_executable_invalid"
    $ResolvedApiKeyFile = Resolve-ExistingFile $ApiKeyFile "bridge_api_key_file_invalid"
    $null = Resolve-ExistingFile $BridgeLauncherPath "bridge_launcher_missing"
    $null = Resolve-ExistingFile $Mt4LauncherPath "mt4_launcher_missing"
    $null = Resolve-ExistingFile $EnvPath "windows_env_launcher_missing"
    $null = Resolve-ExistingFile $WindowsPowerShell "windows_powershell_missing"
    if (
        [IO.Path]::GetFileName($ResolvedTerminal) -ne "terminal.exe" -or
        $ResolvedTerminal -notmatch '(?i)\\IG MetaTrader 4 Terminal\\terminal\.exe$'
    ) {
        throw "configured_terminal_is_not_exact_ig_mt4"
    }
    if (-not (Test-PathWithinRepository $ResolvedPython)) {
        throw "bridge_python_must_be_repository_owned"
    }
    Assert-ExpectedHash $PSCommandPath $ExpectedEnsureSha256 "dependency_watchdog_source_identity_mismatch"
    Assert-ExpectedHash $BridgeLauncherPath $ExpectedBridgeLauncherSha256 "bridge_launcher_source_identity_mismatch"
    Assert-ExpectedHash $Mt4LauncherPath $ExpectedMt4LauncherSha256 "mt4_launcher_source_identity_mismatch"
    Assert-ExpectedHash $EnvPath $ExpectedEnvSha256 "windows_env_source_identity_mismatch"
    $apiKey = Read-ApiKey $ResolvedApiKeyFile

    # Resolve every process identity before starting either dependency.
    $terminal = Get-TerminalState $ResolvedTerminal
    $bridge = Get-BridgeListenerState $ResolvedPython
    if ($bridge.Present -and -not (Test-BridgeCoreReady $apiKey)) {
        Write-StatusAndExit @{
            status = "bridge_unready_refused_no_process_stop"
            bridge_pid = [int]$bridge.ProcessId
            terminal_pid = [int]$terminal.ProcessId
        } 3
    }
    if (
        $bridge.Present -and
        $terminal.Present -and
        (Test-ExactCollectionReadiness $apiKey)
    ) {
        Write-StatusAndExit @{
            status = "dependencies_ready"
            bridge_pid = [int]$bridge.ProcessId
            terminal_pid = [int]$terminal.ProcessId
            expected_symbol_count = $ExpectedSymbols.Count
            bridge_started = $false
            terminal_started = $false
            audit_only = [bool]$AuditOnly
        } 0
    }
    if ($AuditOnly) {
        Write-StatusAndExit @{
            status = "dependencies_not_ready_audit_only"
            bridge_present = [bool]$bridge.Present
            terminal_present = [bool]$terminal.Present
            expected_symbol_count = $ExpectedSymbols.Count
            audit_only = $true
        } 3
    }
    if (-not $terminal.Present -and -not (Test-InteractiveDesktopAvailable)) {
        Write-StatusAndExit @{
            status = "visible_mt4_start_requires_interactive_desktop"
            bridge_present = [bool]$bridge.Present
            terminal_present = $false
        } 3
    }

    $bridgeStarted = $false
    if (-not $bridge.Present) {
        $priorApiKey = [Environment]::GetEnvironmentVariable("FXSTACK_BRIDGE_API_KEY", "Process")
        try {
            [Environment]::SetEnvironmentVariable("FXSTACK_BRIDGE_API_KEY", $apiKey, "Process")
            $bridgeOutput = @(
                & $BridgeLauncherPath `
                    "--background-if-absent" `
                    ([string]$ExpectedBridgePort) `
                    $ResolvedPython `
                    $ResolvedApiKeyFile 2>&1
            )
            $bridgeExitCode = [int]$LASTEXITCODE
            $null = $bridgeOutput
        }
        finally {
            [Environment]::SetEnvironmentVariable("FXSTACK_BRIDGE_API_KEY", $priorApiKey, "Process")
        }
        if ($bridgeExitCode -ne 0) {
            Write-StatusAndExit @{
                status = "absent_bridge_start_refused"
                bridge_launcher_exit_code = $bridgeExitCode
            } 3
        }
        $bridgeStarted = $true
        $bridge = Get-BridgeListenerState $ResolvedPython
        if (-not $bridge.Present -or -not (Test-BridgeCoreReady $apiKey)) {
            Write-StatusAndExit @{
                status = "started_bridge_failed_identity_or_readiness"
                bridge_pid = [int]$bridge.ProcessId
            } 3
        }
    }

    $terminalStarted = $false
    if (-not $terminal.Present) {
        $priorTerminal = [Environment]::GetEnvironmentVariable(
            "FXSTACK_MT4_TERMINAL_EXE",
            "Process"
        )
        try {
            [Environment]::SetEnvironmentVariable(
                "FXSTACK_MT4_TERMINAL_EXE",
                $ResolvedTerminal,
                "Process"
            )
            $mt4Output = @(
                & $WindowsPowerShell `
                    -NoProfile `
                    -NonInteractive `
                    -ExecutionPolicy Bypass `
                    -File $Mt4LauncherPath `
                    -WaitSeconds ([Math]::Min(120, $ReadinessTimeoutSeconds)) 2>&1
            )
            $mt4ExitCode = [int]$LASTEXITCODE
            $null = $mt4Output
        }
        finally {
            [Environment]::SetEnvironmentVariable(
                "FXSTACK_MT4_TERMINAL_EXE",
                $priorTerminal,
                "Process"
            )
        }
        if ($mt4ExitCode -ne 0) {
            Write-StatusAndExit @{
                status = "visible_mt4_start_refused"
                mt4_launcher_exit_code = $mt4ExitCode
                bridge_pid = [int]$bridge.ProcessId
            } 3
        }
        $terminalStarted = $true
        $terminal = Get-TerminalState $ResolvedTerminal
        if (-not $terminal.Present) {
            Write-StatusAndExit @{
                status = "visible_mt4_process_not_confirmed"
                bridge_pid = [int]$bridge.ProcessId
            } 3
        }
    }

    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($ReadinessTimeoutSeconds)
    while ([DateTimeOffset]::UtcNow -lt $deadline) {
        if (Test-ExactCollectionReadiness $apiKey) {
            Write-StatusAndExit @{
                status = "dependencies_ready"
                bridge_pid = [int]$bridge.ProcessId
                terminal_pid = [int]$terminal.ProcessId
                expected_symbol_count = $ExpectedSymbols.Count
                bridge_started = $bridgeStarted
                terminal_started = $terminalStarted
                audit_only = $false
            } 0
        }
        Start-Sleep -Seconds 1
    }
    Write-StatusAndExit @{
        status = "exact_ig_mt4_collection_readiness_timed_out"
        bridge_pid = [int]$bridge.ProcessId
        terminal_pid = [int]$terminal.ProcessId
        expected_symbol_count = $ExpectedSymbols.Count
        bridge_started = $bridgeStarted
        terminal_started = $terminalStarted
    } 3
}
catch {
    Write-StatusAndExit @{
        status = "dependency_bootstrap_refused"
        reason = $_.Exception.Message
    } 2
}
