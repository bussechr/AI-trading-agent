param(
  [Parameter(Mandatory = $true)]
  [string]$RegistryRoot,
  [Parameter(Mandatory = $true)]
  [string]$PairList,
  [string]$Mode = "watch"
)

$ErrorActionPreference = "Stop"

$registryRootPath = [System.IO.Path]::GetFullPath($RegistryRoot)
$pairs = @($PairList -split ",") | ForEach-Object { $_.Trim().ToUpperInvariant() } | Where-Object { $_ }
$oneShot = @("--once", "once") -contains $Mode.ToLowerInvariant()
$bridgeUrl = "http://127.0.0.1:58710"
$dashboardUrl = "http://127.0.0.1:3000"

function Write-Line {
  param([string]$Text = "")
  [Console]::Write($Text + "`r`n")
}

function Get-RegistryStatus {
  param(
    [string]$RootPath,
    [string[]]$ExpectedPairs
  )

  $entries = @{}
  if (Test-Path $RootPath) {
    Get-ChildItem -Path $RootPath -Filter *.json -File | Sort-Object Name | ForEach-Object {
      try {
        $obj = Get-Content $_.FullName -Raw | ConvertFrom-Json
      } catch {
        return
      }
      $pair = ("" + $obj.pair).Trim().ToUpperInvariant()
      if (-not $pair) {
        return
      }
      $entries[$pair] = [pscustomobject]@{
        Pair   = $pair
        File   = $_.Name
        Status = ("" + $obj.promotion_status).Trim()
      }
    }
  }

  $done = @($ExpectedPairs | Where-Object { $entries.ContainsKey($_) })
  $pending = @($ExpectedPairs | Where-Object { -not $entries.ContainsKey($_) })
  [pscustomobject]@{
    Entries = $entries
    Done    = $done
    Pending = $pending
    Count   = $done.Count
    Total   = $ExpectedPairs.Count
  }
}

function Get-ActiveTraining {
  $items = @()

  $winProcs = @(Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -and $_.CommandLine -match "src\.trader\.cli train all --pair"
  })

  foreach ($p in $winProcs) {
    $cmd = "" + $p.CommandLine
    $pair = ""
    if ($cmd -match "--pair\s+([^\s]+)") {
      $pair = $matches[1].Trim().ToUpperInvariant()
    }
    $items += [pscustomobject]@{
      Source = "win"
      Pair   = $pair
      Pid    = ("" + $p.ProcessId)
      Cmd    = $cmd
    }
  }

  if ($items.Count -eq 0) {
    try {
      $raw = & wsl.exe "bash" "-lc" "ps -eo pid,cmd | grep -E 'src\.trader\.cli train all --pair|scripts/train_all\.py --pair' | grep -v grep" 2>$null
      foreach ($line in @($raw)) {
        $text = ("" + $line).Trim()
        if (-not $text) {
          continue
        }
        $pid = (($text -split "\s+", 3)[0]).Trim()
        $pair = ""
        if ($text -match "--pair\s+([^\s]+)") {
          $pair = $matches[1].Trim().ToUpperInvariant()
        }
        $items += [pscustomobject]@{
          Source = "wsl"
          Pair   = $pair
          Pid    = $pid
          Cmd    = $text
        }
      }
    } catch {
    }
  }

  $items
}

function Get-ReadyStatus {
  try {
    $ready = Invoke-RestMethod -Uri ($bridgeUrl + "/v2/ready") -TimeoutSec 2
    [pscustomobject]@{
      BridgeApi    = $(if ($ready.bridge_up -eq $true) { "up" } else { "down" })
      Database     = $(if ($ready.database_ok -eq $true) { "ready" } else { "degraded" })
      Runtime      = $(if ($ready.runtime_ready -eq $true) { "ready" } else { ("" + $ready.runtime_status) })
      RuntimeCycle = $(if ($null -ne $ready.runtime_cycle_age_secs) { "{0:N1} s" -f [double]$ready.runtime_cycle_age_secs } else { "n/a" })
      MT4          = $(if ($ready.mt4_fresh -eq $true) { "live" } else { ("" + $ready.mt4_status) })
      Heartbeat    = $(if ($null -ne $ready.heartbeat_age_secs) { "{0:N1} s" -f [double]$ready.heartbeat_age_secs } else { "n/a" })
      Ticks        = $(if ($ready.ticks_fresh -eq $true) { "live" } else { ("" + $ready.tick_status) })
      StatusTier   = ("" + $ready.status_tier)
    }
  } catch {
    [pscustomobject]@{
      BridgeApi    = "down"
      Database     = "unknown"
      Runtime      = "unknown"
      RuntimeCycle = "n/a"
      MT4          = "unknown"
      Heartbeat    = "n/a"
      Ticks        = "unknown"
      StatusTier   = "unreachable"
    }
  }
}

function Get-DashboardStatus {
  try {
    $resp = Invoke-WebRequest -UseBasicParsing -Uri $dashboardUrl -TimeoutSec 2
    return ("" + $resp.StatusCode)
  } catch {
    return "0"
  }
}

while ($true) {
  $ready = Get-ReadyStatus
  $dashboard = Get-DashboardStatus
  $state = Get-RegistryStatus -RootPath $registryRootPath -ExpectedPairs $pairs
  $active = @(Get-ActiveTraining)
  $current = @($active | Where-Object { $_.Pair } | Select-Object -ExpandProperty Pair -Unique)

  if ($current.Count -gt 0) {
    $currentText = $current -join ", "
  } elseif ($state.Pending.Count -gt 0) {
    $currentText = $state.Pending[0] + " (inferred)"
  } else {
    $currentText = "none"
  }

  Write-Line "============================================================"
  Write-Line ("SYSTEM MONITOR  " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
  Write-Line "============================================================"
  Write-Line "Stack"
  Write-Line ("  Bridge API     : " + $ready.BridgeApi)
  Write-Line ("  Database       : " + $ready.Database)
  Write-Line ("  Runtime        : " + $ready.Runtime)
  Write-Line ("  Runtime cycle  : " + $ready.RuntimeCycle)
  Write-Line ("  MT4            : " + $ready.MT4)
  Write-Line ("  Heartbeat age  : " + $ready.Heartbeat)
  Write-Line ("  Ticks          : " + $ready.Ticks)
  Write-Line ("  Dashboard HTTP : " + $dashboard)
  Write-Line ("  Status tier    : " + $ready.StatusTier)
  Write-Line ""
  Write-Line "Training"
  Write-Line ("  Registry       : " + $registryRootPath)
  Write-Line ("  Progress       : " + $state.Count + "/" + $state.Total)
  Write-Line ("  Current pair   : " + $currentText)
  Write-Line "  Completed:"
  if ($state.Done.Count -eq 0) {
    Write-Line "    none"
  } else {
    foreach ($pair in $state.Done) {
      $entry = $state.Entries[$pair]
      $status = $entry.Status
      if (-not $status) {
        $status = "unknown"
      }
      Write-Line ("    " + $pair + "  " + $status)
    }
  }
  Write-Line "  Active workers:"
  if ($active.Count -eq 0) {
    if ($state.Pending.Count -gt 0) {
      Write-Line "    not visible from this shell"
    } else {
      Write-Line "    none"
    }
  } else {
    foreach ($item in $active) {
      Write-Line ("    " + $item.Source + " PID " + $item.Pid + "  " + $item.Pair)
    }
  }
  Write-Line ""
  Write-Line "Logs"
  Write-Line "  logs\\bridge_58710.log"
  Write-Line "  logs\\runtime_58710.log"
  Write-Line "  logs\\dashboard_3000.log"
  Write-Line "  logs\\full_train_remaining_pairs_20260323.log"

  if ($state.Count -ge $state.Total -and $active.Count -eq 0) {
    Write-Line ""
    Write-Line "TRAINING COMPLETE"
    exit 0
  }

  if ($oneShot) {
    exit 0
  }

  Start-Sleep -Seconds 15
  Write-Line ""
}
