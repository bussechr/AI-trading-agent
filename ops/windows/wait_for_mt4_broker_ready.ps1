[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$BridgeUrl,
    [Parameter(Mandatory = $true)]
    [ValidateSet("demo", "real")]
    [string]$ExpectedAccountMode,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedSymbols,
    [ValidateRange(5, 300)]
    [int]$TimeoutSeconds = 120,
    [ValidateRange(100, 5000)]
    [int]$PollMilliseconds = 500
)

# AGENT: ROLE: bounded authenticated MT4/EA broker-attestation gate before runtime spawn.
# AGENT: CALLED BY: launch_all.bat after the configured visible terminal is running.
# AGENT: SIDE EFFECTS: none; reads only loopback bridge readiness and state.

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$uri = [Uri]$BridgeUrl
if ($uri.Scheme -notin @("http", "https") -or $uri.Host -notin @("127.0.0.1", "localhost", "::1")) {
    throw "broker readiness bridge URL must be loopback"
}

$symbols = @(
    $ExpectedSymbols.Split(",", [StringSplitOptions]::RemoveEmptyEntries) |
        ForEach-Object { $_.Trim().ToUpperInvariant() } |
        Where-Object { $_ }
)
if ($symbols.Count -eq 0 -or @($symbols | Sort-Object -Unique).Count -ne $symbols.Count) {
    throw "expected broker symbol scope is empty or contains duplicates"
}

$headers = @{ Accept = "application/json" }
$apiKey = [string]$env:FXSTACK_BRIDGE_API_KEY
if (-not [string]::IsNullOrWhiteSpace($apiKey)) {
    $headers["X-API-Key"] = $apiKey.Trim()
}

$deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
$lastReasons = @("bridge_not_observed")
do {
    try {
        $ready = Invoke-RestMethod -Uri ($BridgeUrl.TrimEnd("/") + "/v2/ready") -Headers $headers -TimeoutSec 3
        $state = Invoke-RestMethod -Uri ($BridgeUrl.TrimEnd("/") + "/v2/state") -Headers $headers -TimeoutSec 3
        $reasons = [Collections.Generic.List[string]]::new()
        if ($ready.bridge_up -ne $true -or $ready.database_ok -ne $true) { $reasons.Add("bridge_or_database_not_ready") }
        if ($ready.mt4_fresh -ne $true) { $reasons.Add("mt4_heartbeat_not_fresh") }
        if ($ready.ticks_fresh -ne $true) { $reasons.Add("mt4_ticks_not_fresh") }
        if ([string]$state.system_status -ne "connected") { $reasons.Add("terminal_not_connected") }
        if ([string]$state.broker_account_mode -ne $ExpectedAccountMode) { $reasons.Add("account_mode_unattested") }
        if ([string]::IsNullOrWhiteSpace([string]$state.broker_account_scope)) { $reasons.Add("account_scope_unattested") }
        if ([int]$state.broker_account_magic -le 0) { $reasons.Add("account_magic_unattested") }
        if ([string]$state.broker_venue_id -ne "ig_mt4") { $reasons.Add("venue_unattested") }
        if ([string]::IsNullOrWhiteSpace([string]$state.bridge_producer_instance_id)) { $reasons.Add("producer_unattested") }
        if ([int]$state.broker_symbol_ready_count -ne $symbols.Count) { $reasons.Add("broker_symbol_scope_incomplete") }
        if (@($state.unsupported_pairs).Count -ne 0) { $reasons.Add("unsupported_pairs_present") }
        if ([double]$state.freemargin -le 0.0) { $reasons.Add("free_margin_unattested") }
        if ($reasons.Count -eq 0) {
            Write-Host ("[mt4-ready] authenticated {0} account; symbols={1}; broker heartbeat and ticks are fresh" -f $ExpectedAccountMode, $symbols.Count)
            exit 0
        }
        $lastReasons = @($reasons)
    }
    catch {
        $lastReasons = @("bridge_read_failed:" + $_.Exception.GetType().Name)
    }
    Start-Sleep -Milliseconds $PollMilliseconds
} while ([DateTimeOffset]::UtcNow -lt $deadline)

Write-Error ("MT4 broker attestation did not become ready within {0}s: {1}" -f $TimeoutSeconds, ($lastReasons -join ","))
exit 2
