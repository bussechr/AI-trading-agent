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
        Path   = $_.FullName
      }
    }
  }

  $done = @($ExpectedPairs | Where-Object { $entries.ContainsKey($_) })
  [pscustomobject]@{
    Entries = $entries
    Done    = $done
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

while ($true) {
  $state = Get-RegistryStatus -RootPath $registryRootPath -ExpectedPairs $pairs
  $active = @(Get-ActiveTraining)
  $current = @($active | Where-Object { $_.Pair } | Select-Object -ExpandProperty Pair -Unique)
  $pending = @($pairs | Where-Object { -not $state.Entries.ContainsKey($_) })
  if ($current.Count -gt 0) {
    $currentText = $current -join ", "
  } elseif ($pending.Count -gt 0) {
    $currentText = $pending[0] + " (inferred)"
  } else {
    $currentText = "none"
  }

  Write-Line "============================================================"
  Write-Line ("TRAINING MONITOR  " + (Get-Date -Format "yyyy-MM-dd HH:mm:ss"))
  Write-Line "============================================================"
  Write-Line ("Registry : " + $registryRootPath)
  Write-Line ("Progress : " + $state.Count + "/" + $state.Total)
  Write-Line ("Current  : " + $currentText)
  Write-Line ""
  Write-Line "Completed pairs:"
  if ($state.Done.Count -eq 0) {
    Write-Line "  none"
  } else {
    foreach ($pair in $state.Done) {
      $entry = $state.Entries[$pair]
      $status = $entry.Status
      if (-not $status) {
        $status = "unknown"
      }
      Write-Line ("  " + $pair + "  " + $status + "  " + $entry.File)
    }
  }
  Write-Line ""
  Write-Line "Active workers:"
  if ($active.Count -eq 0) {
    if ($pending.Count -gt 0) {
      Write-Line "  not visible from this shell"
    } else {
      Write-Line "  none"
    }
  } else {
    foreach ($item in $active) {
      Write-Line ("  " + $item.Source + " PID " + $item.Pid + "  " + $item.Pair)
    }
  }

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
