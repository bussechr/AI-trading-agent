param(
    [int]$WaitSeconds = 30
)

$ErrorActionPreference = "Stop"

function Get-RunningTerminal {
    param([string]$ExpectedPath = "")

    $expected = if ($ExpectedPath) { [IO.Path]::GetFullPath($ExpectedPath) } else { "" }
    $processes = @(Get-CimInstance Win32_Process -Filter "Name='terminal.exe'" -ErrorAction SilentlyContinue)
    foreach ($process in $processes) {
        $path = [string]$process.ExecutablePath
        if (-not $path) {
            continue
        }
        if (-not $expected -or [IO.Path]::GetFullPath($path) -eq $expected) {
            return $process
        }
    }
    return $null
}

function Resolve-TerminalPath {
    $configured = [string]$env:FXSTACK_MT4_TERMINAL_EXE
    if ($configured) {
        $resolved = [IO.Path]::GetFullPath($configured)
        if (-not (Test-Path -LiteralPath $resolved -PathType Leaf)) {
            throw "Configured MT4 terminal was not found: $resolved"
        }
        return $resolved
    }

    $running = Get-RunningTerminal
    if ($running) {
        return [string]$running.ExecutablePath
    }

    $candidates = @(
        "${env:ProgramFiles(x86)}\IG MetaTrader 4 Terminal\terminal.exe",
        "$env:ProgramFiles\IG MetaTrader 4 Terminal\terminal.exe"
    )
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) {
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw "MT4 terminal.exe was not found. Set FXSTACK_MT4_TERMINAL_EXE to its absolute path."
}

if ($WaitSeconds -lt 1 -or $WaitSeconds -gt 120) {
    throw "WaitSeconds must be between 1 and 120."
}

$terminalPath = Resolve-TerminalPath
$running = Get-RunningTerminal -ExpectedPath $terminalPath
if ($running) {
    Write-Host ("[mt4] already running pid={0} path={1}" -f $running.ProcessId, $terminalPath)
    exit 0
}

# MT4 is an operator-facing broker terminal; keep its window visible so the
# account, AutoTrading state, chart, and EA attachment remain inspectable.
Start-Process -FilePath $terminalPath | Out-Null
$deadline = (Get-Date).AddSeconds($WaitSeconds)
do {
    Start-Sleep -Milliseconds 250
    $running = Get-RunningTerminal -ExpectedPath $terminalPath
    if ($running) {
        Write-Host ("[mt4] started pid={0} path={1}" -f $running.ProcessId, $terminalPath)
        exit 0
    }
} while ((Get-Date) -lt $deadline)

throw "MT4 did not remain running within $WaitSeconds seconds: $terminalPath"
