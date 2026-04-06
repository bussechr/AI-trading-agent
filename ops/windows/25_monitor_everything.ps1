# AGENT: ROLE: Aggregate monitor for registry progress, bridge, dashboard, runtime, and active training processes.
# AGENT: ENTRYPOINT: invoked by `ops/windows/25_monitor_everything.bat` or directly in PowerShell.
# AGENT: PRIMARY INPUTS: registry root, expected pair list, bridge/dashboard URLs, local process table.
# AGENT: PRIMARY OUTPUTS: console watch output and aggregated status snapshots.
# AGENT: DEPENDS ON: bridge/dashboard HTTP endpoints and Windows process inspection.
# AGENT: CALLED BY: operators and deployment workflows.
# AGENT: STATE / SIDE EFFECTS: read-only monitoring.
# AGENT: HANDSHAKES: `/v2/ready`, dashboard HTTP root, registry artifact layout.
# AGENT: SEE: `docs/agents/ops-entrypoints.md` -> `ops/windows/23_start_monitor.bat` -> `docs/agents/bridge-and-api-handshakes.md`
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

function Resolve-ArtifactRoot {
  param([string]$RegistryRootPath)

  $leaf = Split-Path $RegistryRootPath -Leaf
  if ($leaf -like "registry_*") {
    $artifactLeaf = $leaf.Substring("registry_".Length)
    return Join-Path (Split-Path $RegistryRootPath -Parent) $artifactLeaf
  }
  return ""
}

function Get-InProgressPairs {
  param(
    [string]$ArtifactRootPath,
    [hashtable]$Entries,
    [string[]]$ExpectedPairs
  )

  if (-not $ArtifactRootPath -or -not (Test-Path $ArtifactRootPath)) {
    return @()
  }

  $out = @()
  foreach ($pair in $ExpectedPairs) {
    if ($Entries.ContainsKey($pair)) {
      continue
    }
    $pairRoot = Join-Path $ArtifactRootPath $pair.ToLowerInvariant()
    if (-not (Test-Path $pairRoot)) {
      continue
    }
    $fileCount = @(Get-ChildItem -Path $pairRoot -Recurse -File -ErrorAction SilentlyContinue).Count
    if ($fileCount -gt 0) {
      $out += [pscustomobject]@{
        Pair      = $pair
        FileCount = $fileCount
      }
    }
  }
  return @($out | Sort-Object Pair)
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
      RuntimePhase = ("" + $ready.runtime_phase)
      RuntimePair  = ("" + $ready.runtime_phase_pair)
      RuntimeBoot  = ("" + $ready.runtime_boot_id)
      RuntimeProg  = $(if ($null -ne $ready.runtime_last_progress_age_secs) { "{0:N1} s" -f [double]$ready.runtime_last_progress_age_secs } else { "n/a" })
      RuntimeFail  = ("" + $ready.runtime_failure_reason)
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
      RuntimePhase = ""
      RuntimePair  = ""
      RuntimeBoot  = ""
      RuntimeProg  = "n/a"
      RuntimeFail  = ""
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

function Get-LiveState {
  try {
    $state = Invoke-RestMethod -Uri ($bridgeUrl + "/v2/state") -TimeoutSec 2
    [pscustomobject]@{
      ActivePairCount          = [int]($state.active_pair_count | ForEach-Object { $_ })
      ActiveRegistryRoot       = ("" + $state.active_registry_root)
      ActivationMismatchCount  = [int]($state.activation_mismatch_count | ForEach-Object { $_ })
      ActivationMismatchPairs  = @($state.activation_mismatch_pairs)
      InferenceErrors          = [int]((($state.runtime_diag).inference_errors) | ForEach-Object { $_ })
      StartupInferenceFailures = [int]($state.startup_inference_failures | ForEach-Object { $_ })
      BrokerSymbolFailures     = @($state.broker_symbol_failures)
      BrokerSymbolReadyCount   = [int]($state.broker_symbol_ready_count | ForEach-Object { $_ })
    }
  } catch {
    [pscustomobject]@{
      ActivePairCount          = 0
      ActiveRegistryRoot       = ""
      ActivationMismatchCount  = 0
      ActivationMismatchPairs  = @()
      InferenceErrors          = 0
      StartupInferenceFailures = 0
      BrokerSymbolFailures     = @()
      BrokerSymbolReadyCount   = 0
    }
  }
}

while ($true) {
  $ready = Get-ReadyStatus
  $dashboard = Get-DashboardStatus
  $liveState = Get-LiveState
  $state = Get-RegistryStatus -RootPath $registryRootPath -ExpectedPairs $pairs
  $artifactRootPath = Resolve-ArtifactRoot -RegistryRootPath $registryRootPath
  $inProgress = @(Get-InProgressPairs -ArtifactRootPath $artifactRootPath -Entries $state.Entries -ExpectedPairs $pairs)
  $active = @(Get-ActiveTraining)
  $current = @($active | Where-Object { $_.Pair } | Select-Object -ExpandProperty Pair -Unique)

  if ($current.Count -gt 0) {
    $currentText = $current -join ", "
  } elseif ($inProgress.Count -gt 0) {
    $currentText = (($inProgress | Select-Object -ExpandProperty Pair) -join ", ") + " (artifact-inferred)"
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
  if ($ready.Runtime -ne "ready") {
    if ($ready.RuntimePhase) {
      Write-Line ("  Runtime phase  : " + $ready.RuntimePhase)
    }
    if ($ready.RuntimePair) {
      Write-Line ("  Runtime pair   : " + $ready.RuntimePair)
    }
    Write-Line ("  Runtime prog   : " + $ready.RuntimeProg)
    if ($ready.RuntimeBoot) {
      Write-Line ("  Runtime boot   : " + $ready.RuntimeBoot)
    }
    if ($ready.RuntimeFail) {
      Write-Line ("  Runtime fail   : " + $ready.RuntimeFail)
    }
  }
  Write-Line ("  MT4            : " + $ready.MT4)
  Write-Line ("  Heartbeat age  : " + $ready.Heartbeat)
  Write-Line ("  Ticks          : " + $ready.Ticks)
  Write-Line ("  Dashboard HTTP : " + $dashboard)
  Write-Line ("  Status tier    : " + $ready.StatusTier)
  Write-Line ("  Active pairs   : " + $liveState.ActivePairCount)
  Write-Line ("  Registry root  : " + $(if ($liveState.ActiveRegistryRoot) { $liveState.ActiveRegistryRoot } else { "n/a" }))
  Write-Line ("  Inference errs : " + $liveState.InferenceErrors + "  (startup " + $liveState.StartupInferenceFailures + ")")
  Write-Line ("  Mismatch count : " + $liveState.ActivationMismatchCount)
  if ($liveState.BrokerSymbolFailures.Count -gt 0) {
    Write-Line ("  Broker symbols : " + ($liveState.BrokerSymbolReadyCount) + " ready / failures=" + (($liveState.BrokerSymbolFailures -join ", ")))
  } else {
    Write-Line ("  Broker symbols : " + ($liveState.BrokerSymbolReadyCount) + " ready")
  }
  Write-Line ""
  Write-Line "Training"
  Write-Line ("  Registry       : " + $registryRootPath)
  Write-Line ("  Progress       : " + $state.Count + "/" + $state.Total)
  Write-Line ("  Current pair   : " + $currentText)
  if ($artifactRootPath) {
    Write-Line ("  Artifact root  : " + $artifactRootPath)
  }
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
    if ($inProgress.Count -gt 0) {
      foreach ($item in $inProgress) {
        Write-Line ("    artifact " + $item.Pair + "  files=" + $item.FileCount)
      }
    } elseif ($state.Pending.Count -gt 0) {
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
