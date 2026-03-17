param(
    [string]$BridgeUrl = "http://127.0.0.1:58710",
    [int]$PollSeconds = 2
)

if ($PollSeconds -lt 1) {
    $PollSeconds = 1
}

$ErrorActionPreference = "Stop"
$openSamples = New-Object System.Collections.ArrayList
$closeSamples = New-Object System.Collections.ArrayList

function Safe-Double {
    param([object]$Value)
    try {
        return [double]$Value
    }
    catch {
        return 0.0
    }
}

function Add-RollingSample {
    param(
        [System.Collections.ArrayList]$Samples,
        [double]$Value,
        [datetime]$Now
    )
    [void]$Samples.Add([PSCustomObject]@{ ts = $Now; val = $Value })
    $cutoff = $Now.AddMinutes(-5)
    while ($Samples.Count -gt 0 -and $Samples[0].ts -lt $cutoff) {
        $Samples.RemoveAt(0)
    }
}

function Get-RollingAverage {
    param(
        [System.Collections.ArrayList]$Samples,
        [int]$WindowSeconds,
        [datetime]$Now
    )
    if ($Samples.Count -eq 0) {
        return 0.0
    }
    $cutoff = $Now.AddSeconds(-$WindowSeconds)
    $vals = @()
    foreach ($row in $Samples) {
        if ($row.ts -ge $cutoff) {
            $vals += [double]$row.val
        }
    }
    if ($vals.Count -eq 0) {
        return 0.0
    }
    return [double](($vals | Measure-Object -Average).Average)
}

function Pct {
    param([double]$Value)
    return ("{0,6:N1}" -f $Value)
}

while ($true) {
    try {
        $now = Get-Date
        $resp = Invoke-RestMethod -Method Get -Uri "$BridgeUrl/v2/monitor" -TimeoutSec 4

        $entry = $resp.monitor.entry
        $close = $resp.monitor.close

        [double]$openNow = Safe-Double $entry.open_proximity_pct
        [double]$closeNow = Safe-Double $close.close_proximity_pct

        Add-RollingSample -Samples $openSamples -Value $openNow -Now $now
        Add-RollingSample -Samples $closeSamples -Value $closeNow -Now $now

        $open1m = Get-RollingAverage -Samples $openSamples -WindowSeconds 60 -Now $now
        $open5m = Get-RollingAverage -Samples $openSamples -WindowSeconds 300 -Now $now
        $close1m = Get-RollingAverage -Samples $closeSamples -WindowSeconds 60 -Now $now
        $close5m = Get-RollingAverage -Samples $closeSamples -WindowSeconds 300 -Now $now

        $entrySymbol = ""
        $entrySide = ""
        $entryBlocker = "none"
        $entryReady = $false
        if ($null -ne $entry) {
            $entrySymbol = [string]$entry.symbol
            $entrySide = [string]$entry.side
            if ($entry.blocked_by) {
                $entryBlocker = [string]$entry.blocked_by
            }
            $entryReady = [bool]$entry.execution_ready
        }

        $closeReason = "none"
        $closeOpenCount = 0
        $closeTopSymbol = ""
        $closeTopSide = ""
        $closeTopAction = ""
        $closeTopPct = 0.0
        $closePositions = @()
        if ($null -ne $close) {
            if ($close.dominant_close_reason) {
                $closeReason = [string]$close.dominant_close_reason
            }
            $closeOpenCount = [int](Safe-Double $close.positions_open)
            if ($null -ne $close.positions) {
                $closePositions = @($close.positions)
            }
            if ($closePositions.Count -gt 0) {
                $top = $closePositions | Sort-Object { Safe-Double $_.close_proximity_pct } -Descending | Select-Object -First 1
                $closeTopSymbol = [string]$top.symbol
                $closeTopSide = [string]$top.side
                $closeTopAction = [string]$top.last_action
                $closeTopPct = Safe-Double $top.close_proximity_pct
            }
        }

        $bridgeStatus = [string]$resp.bridge.system_status
        $equity = Safe-Double $resp.account.equity
        $warmup = [bool]$resp.monitor.warmup_mode
        $starvation = [bool]$resp.monitor.starvation_mode
        $relaxLevel = Safe-Double $resp.monitor.relax_level

        Clear-Host
        Write-Host "TRADE CONFIDENCE MONITOR" -ForegroundColor Cyan
        Write-Host "Bridge: $BridgeUrl | Poll: ${PollSeconds}s | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        Write-Host "Status: $bridgeStatus | Equity: $($equity.ToString('N2')) | Warmup: $warmup | Starvation: $starvation (rL=$($relaxLevel.ToString('N2')))"
        Write-Host ""

        Write-Host (("OPEN  now {0}% | 1m {1}% | 5m {2}% | {3} {4}") -f (Pct $openNow), (Pct $open1m), (Pct $open5m), $entrySymbol, $entrySide) -ForegroundColor Yellow
        Write-Host "      blocker=$entryBlocker | execution_ready=$entryReady"

        Write-Host (("CLOSE now {0}% | 1m {1}% | 5m {2}% | top_reason={3} | open_pos={4}") -f (Pct $closeNow), (Pct $close1m), (Pct $close5m), $closeReason, $closeOpenCount) -ForegroundColor Green
        if ($closePositions.Count -gt 0) {
            Write-Host (("      top={0} {1} {2}% | action={3}") -f $closeTopSymbol, $closeTopSide, (Pct $closeTopPct), $closeTopAction)
            $shown = 0
            foreach ($p in ($closePositions | Sort-Object { Safe-Double $_.close_proximity_pct } -Descending)) {
                Write-Host (("      - {0} {1}: {2}% ({3})") -f [string]$p.symbol, [string]$p.side, (Pct (Safe-Double $p.close_proximity_pct)), [string]$p.dominant_close_reason)
                $shown += 1
                if ($shown -ge 3) {
                    break
                }
            }
        }
        else {
            Write-Host "      no open positions"
        }
    }
    catch {
        Clear-Host
        Write-Host "TRADE CONFIDENCE MONITOR" -ForegroundColor Cyan
        Write-Host "Bridge: $BridgeUrl | Poll: ${PollSeconds}s | $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
        Write-Host "Failed to query /v2/monitor: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Retrying..."
    }

    Start-Sleep -Seconds $PollSeconds
}
