param(
    [switch]$RestartMt4,
    [string]$BridgeApiKeyFile = "",
    [string]$BridgeCommandTokenFile = "",
    [string]$ConsumerIdentity = "",
    [string]$TerminalLeaseScope = "",
    [string]$CredentialGenerationId = ""
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
        "${env:ProgramFiles(x86)}\\IG MetaTrader 4 Terminal\\metaeditor.exe",
        "$env:ProgramFiles\\IG MetaTrader 4 Terminal\\metaeditor.exe"
    )
    foreach ($candidate in $paths) {
        if (Test-Path $candidate) {
            return $candidate
        }
    }
    return $null
}

function Install-BridgeEaAuthFiles {
    param(
        [Parameter(Mandatory = $true)][string]$DataDir,
        [Parameter(Mandatory = $true)][string]$KeyFile,
        [Parameter(Mandatory = $true)][string]$CommandTokenFile,
        [Parameter(Mandatory = $true)][string]$Consumer,
        [Parameter(Mandatory = $true)][string]$LeaseScope,
        [Parameter(Mandatory = $true)][string]$GenerationId
    )

    if (Get-Process terminal -ErrorAction SilentlyContinue) {
        throw "Stop MT4 before installing BridgeEA authentication state."
    }
    if (-not (Test-Path -LiteralPath $KeyFile -PathType Leaf)) {
        throw "Bridge API key file not found: $KeyFile"
    }
    $apiKey = (Get-Content -LiteralPath $KeyFile -Raw).Trim()
    if ($apiKey -notmatch "^[A-Fa-f0-9]{64}$") {
        throw "Bridge API key must contain exactly 64 hexadecimal characters."
    }
    if (-not (Test-Path -LiteralPath $CommandTokenFile -PathType Leaf)) {
        throw "Bridge command token file not found: $CommandTokenFile"
    }
    $commandToken = (Get-Content -LiteralPath $CommandTokenFile -Raw).Trim()
    if ($commandToken -notmatch "^[A-Fa-f0-9]{64}$" -or $commandToken -eq $apiKey) {
        throw "Bridge command token must be a distinct 64-character hexadecimal secret."
    }
    foreach ($value in @($Consumer, $LeaseScope, $GenerationId)) {
        if ($value -notmatch "^[A-Za-z0-9._:-]{1,128}$") {
            throw "Bridge consumer identity, lease scope, and generation must be URL-safe identifiers."
        }
    }

    $filesRoot = Join-Path $DataDir "MQL4\\Files"
    New-Item -ItemType Directory -Path $filesRoot -Force | Out-Null
    Copy-Item -LiteralPath $KeyFile -Destination (Join-Path $filesRoot "bridge_api_key.txt") -Force
    Copy-Item -LiteralPath $CommandTokenFile -Destination (Join-Path $filesRoot "bridge_command_token.txt") -Force
    [IO.File]::WriteAllText((Join-Path $filesRoot "bridge_consumer_identity.txt"), $Consumer + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $filesRoot "bridge_terminal_lease_scope.txt"), $LeaseScope + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText((Join-Path $filesRoot "bridge_credential_generation_id.txt"), $GenerationId + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))

    # MT4 automatically records every EA input in its journal. Keep ApiKey
    # empty in saved chart profiles so the secret is loaded from MQL4/Files and
    # never copied into terminal logs.
    $profilesRoot = Join-Path $DataDir "profiles"
    $charts = @(Get-ChildItem -LiteralPath $profilesRoot -Filter "*.chr" -Recurse -File -ErrorAction SilentlyContinue)
    $stamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $updatedCount = 0
    $expertPattern = [regex]::new("<expert>.*?</expert>", [Text.RegularExpressions.RegexOptions]::Singleline)
    $secretPattern = [regex]::new("(?m)^((?:ApiKey|CommandToken|ConsumerIdentity|TerminalLeaseScope|CredentialGenerationId)=)[^\r\n]*")

    foreach ($chart in $charts) {
        $text = [IO.File]::ReadAllText($chart.FullName, [Text.Encoding]::UTF8)
        $bridgeBlocks = @($expertPattern.Matches($text) | Where-Object { $_.Value -match "(?m)^name=BridgeEA\r?$" })
        if ($bridgeBlocks.Count -eq 0) {
            continue
        }
        foreach ($block in $bridgeBlocks) {
            if (-not $secretPattern.IsMatch($block.Value)) {
                throw "BridgeEA chart block is missing authentication inputs: $($chart.FullName)"
            }
        }
        $updated = $expertPattern.Replace($text, {
            param($match)
            $block = $match.Value
            if ($block -notmatch "(?m)^name=BridgeEA\r?$") {
                return $block
            }
            $replacement = '${1}'
            return $secretPattern.Replace($block, $replacement)
        })
        if ($updated -ne $text) {
            Copy-Item -LiteralPath $chart.FullName -Destination "$($chart.FullName).bak_bridge_auth_$stamp" -Force
            [IO.File]::WriteAllText($chart.FullName, $updated, [Text.UTF8Encoding]::new($false))
        }
        $updatedCount++
    }
    if ($updatedCount -eq 0) {
        throw "No saved BridgeEA chart profile was found under $profilesRoot"
    }
    return $updatedCount
}

$dataDir = Resolve-TerminalDataDir
$targetExperts = Join-Path $dataDir "MQL4\\Experts"
$targetInclude = Join-Path $dataDir "MQL4\\Include"

$profileCount = 0
if ($BridgeApiKeyFile) {
    $profileCount = Install-BridgeEaAuthFiles -DataDir $dataDir -KeyFile $BridgeApiKeyFile -CommandTokenFile $BridgeCommandTokenFile -Consumer $ConsumerIdentity -LeaseScope $TerminalLeaseScope -GenerationId $CredentialGenerationId
}

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

    # MetaEditor requires the quoted compile and log switches to arrive as one
    # command-line string. Passing them as a PowerShell argument array strips
    # the path quotes on installations whose terminal data directory contains
    # spaces, so the compiler exits without producing either output.
    $argumentLine = "/compile:`"$compileTarget`" /log:`"$compileLog`""
    $proc = Start-Process -FilePath $metaEditor -ArgumentList $argumentLine -Wait -PassThru -WindowStyle Hidden
    $ex4 = Join-Path $targetExperts "BridgeEA.ex4"
    $compileInputs = @(
        $compileTarget,
        (Join-Path $targetInclude "BridgeHttp.mqh"),
        (Join-Path $targetInclude "BridgeUtils.mqh")
    )
    $newestInputWrite = ($compileInputs | ForEach-Object { (Get-Item $_).LastWriteTime } | Sort-Object -Descending | Select-Object -First 1)
    # Some MetaEditor builds hand compilation to their existing GUI process and
    # return before the EX4 timestamp is visible. Wait briefly for the actual
    # output instead of treating the launcher process exit code as the result.
    for ($attempt = 0; $attempt -lt 50; $attempt++) {
        if ((Test-Path $ex4) -and ((Get-Item $ex4).LastWriteTime -ge $newestInputWrite)) {
            $compiled = $true
            break
        }
        Start-Sleep -Milliseconds 200
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
if ($BridgeApiKeyFile) {
    Write-Host ("[bridge-ea] key_file=installed profile_secrets_cleared={0}" -f $profileCount)
}
