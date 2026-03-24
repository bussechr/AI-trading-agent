param(
    [switch]$RestartMt4
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
$repoExperts = Join-Path $root "MQL4\\Experts"
$repoInclude = Join-Path $root "MQL4\\Include"

function Resolve-TerminalDataDir {
    $terminalRoot = Join-Path $env:APPDATA "MetaQuotes\\Terminal"
    if (-not (Test-Path $terminalRoot)) {
        throw "MetaQuotes terminal root not found: $terminalRoot"
    }

    $candidates = Get-ChildItem $terminalRoot -Directory | Where-Object {
        Test-Path (Join-Path $_.FullName "MQL4\\Experts\\BridgeEA.mq4")
    }
    if (-not $candidates) {
        $candidates = Get-ChildItem $terminalRoot -Directory | Where-Object {
            Test-Path (Join-Path $_.FullName "MQL4\\Experts")
        }
    }
    if (-not $candidates) {
        throw "No MT4 terminal data directories found under $terminalRoot"
    }

    return ($candidates | Sort-Object LastWriteTime -Descending | Select-Object -First 1).FullName
}

function Resolve-MetaEditorPath {
    $proc = Get-Process terminal -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($proc -and $proc.Path) {
        $candidate = Join-Path (Split-Path $proc.Path -Parent) "metaeditor.exe"
        if (Test-Path $candidate) {
            return $candidate
        }
    }

    $paths = @(
        "$env:ProgramFiles(x86)\\IG MetaTrader 4 Terminal\\metaeditor.exe",
        "$env:ProgramFiles\\IG MetaTrader 4 Terminal\\metaeditor.exe"
    )
    foreach ($candidate in $paths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

$dataDir = Resolve-TerminalDataDir
$targetExperts = Join-Path $dataDir "MQL4\\Experts"
$targetInclude = Join-Path $dataDir "MQL4\\Include"

Copy-Item (Join-Path $repoExperts "BridgeEA.mq4") (Join-Path $targetExperts "BridgeEA.mq4") -Force
Copy-Item (Join-Path $repoInclude "BridgeHttp.mqh") (Join-Path $targetInclude "BridgeHttp.mqh") -Force
Copy-Item (Join-Path $repoInclude "BridgeUtils.mqh") (Join-Path $targetInclude "BridgeUtils.mqh") -Force

$metaEditor = Resolve-MetaEditorPath
$compiled = $false
if ($metaEditor) {
    $compileTarget = Join-Path $targetExperts "BridgeEA.mq4"
    $compileLog = Join-Path $env:TEMP "bridgeea_compile.log"
    if (Test-Path $compileLog) {
        Remove-Item $compileLog -Force -ErrorAction SilentlyContinue
    }

    $proc = Start-Process -FilePath $metaEditor -ArgumentList "/compile:`"$compileTarget`"","/log:`"$compileLog`"" -Wait -PassThru -WindowStyle Hidden
    $ex4 = Join-Path $targetExperts "BridgeEA.ex4"
    if ((Test-Path $ex4) -and ((Get-Item $ex4).LastWriteTime -ge (Get-Item $compileTarget).LastWriteTime)) {
        $compiled = $true
    } elseif ($proc.ExitCode -eq 0) {
        $compiled = $true
    }
}

if ($RestartMt4) {
    $terminal = Get-Process terminal -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($terminal) {
        $terminalPath = $terminal.Path
        Stop-Process -Id $terminal.Id -Force
        Start-Sleep -Seconds 3
        if ($terminalPath) {
            Start-Process -FilePath $terminalPath | Out-Null
        }
    }
}

Write-Host ("[bridge-ea] data_dir={0}" -f $dataDir)
Write-Host ("[bridge-ea] compiled={0}" -f ($(if ($compiled) { "yes" } else { "no" })))
