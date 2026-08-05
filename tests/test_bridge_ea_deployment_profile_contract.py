from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "ops" / "windows" / "24_deploy_bridge_ea.ps1"
SCALP_LAUNCHER = ROOT / "ops" / "windows" / "21_start_scalp_runtime.bat"
BRIDGE_EA = ROOT / "MQL4" / "Experts" / "BridgeEA.mq4"
IG_CATALOG = (
    ROOT
    / "fx-quant-stack"
    / "src"
    / "fxstack"
    / "providers"
    / "ig_mt4_catalog.py"
)


def test_deployment_pins_active_saved_chart_to_canonical_exact_22_scope() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    launcher = SCALP_LAUNCHER.read_text(encoding="utf-8")
    bridge_ea = BRIDGE_EA.read_text(encoding="utf-8")
    catalog = IG_CATALOG.read_text(encoding="utf-8")

    deploy_match = re.search(
        r'^\$canonicalScalpSymbolsCsv = "([A-Z,]+)"$', deploy, re.MULTILINE
    )
    launcher_match = re.search(
        r'^set "SCALP_IG_MT4_PAIRS=([A-Z,]+)"$', launcher, re.MULTILINE
    )
    ea_match = re.search(r'input string SymbolsCsv = "([A-Z,]+)";', bridge_ea)
    catalog_symbols = tuple(re.findall(r'canonical_symbol="([A-Z]{6})"', catalog))

    assert deploy_match is not None
    assert launcher_match is not None
    assert ea_match is not None
    deploy_symbols = tuple(deploy_match.group(1).split(","))
    assert len(deploy_symbols) == len(set(deploy_symbols)) == 22
    assert deploy_symbols == tuple(launcher_match.group(1).split(","))
    assert deploy_symbols == tuple(ea_match.group(1).split(","))
    assert deploy_symbols == catalog_symbols

    assert 'Join-Path $profilesRoot "lastprofile.ini"' in deploy
    assert "Resolve-ActiveProfileDir -DataDir $DataDir" in deploy
    assert '$scopeInputNames = @("SymbolsCsv", "BarHistorySymbolsCsv")' in deploy
    assert '$scopeMatch.Groups[1].Value + "=" + $canonicalScalpSymbolsCsv' in deploy
    assert "No saved BridgeEA chart was found in the active MT4 profile." in deploy
    assert "Active BridgeEA saved chart scope verification failed" in deploy


def test_saved_chart_update_has_backup_and_full_attempt_rollback() -> None:
    deploy = DEPLOY.read_text(encoding="utf-8")
    write_loop = deploy.split("$writtenPlans = @()", 1)[1].split(
        "# Verify the exact persisted active-profile scope", 1
    )[0]
    rollback = deploy.split("catch {", 1)[1].split(
        "return [pscustomobject]", 1
    )[0]

    assert ".bak_bridge_deploy_$stamp" in deploy
    assert write_loop.index("Copy-Item -LiteralPath $plan.Path") < write_loop.index(
        "$writtenPlans += $plan"
    )
    assert write_loop.index("$writtenPlans += $plan") < write_loop.index(
        "[IO.File]::WriteAllText($plan.Path"
    )
    assert "for ($index = $writtenPlans.Count - 1; $index -ge 0; $index--)" in rollback
    assert "Copy-Item -LiteralPath $plan.BackupPath -Destination $plan.Path -Force" in rollback
    assert "rollback was incomplete" in rollback
    assert "update failed and was rolled back" in rollback

    host_lines = [line for line in deploy.splitlines() if "Write-Host" in line]
    assert host_lines
    assert all("$apiKey" not in line and "$commandToken" not in line for line in host_lines)


@pytest.mark.skipif(os.name != "nt", reason="PowerShell saved-chart fixture contract")
def test_deployment_function_updates_direct_child_active_chart_fixture(
    tmp_path: Path,
) -> None:
    profiles = tmp_path / "profiles"
    active_profile = profiles / "Default"
    active_profile.mkdir(parents=True)
    (profiles / "lastprofile.ini").write_text("Default\n", encoding="utf-8")
    chart = active_profile / "chart01.chr"
    original = """<chart>
<expert>
name=BridgeEA
ApiBase=http://127.0.0.1:58710
ApiKey=legacy-value-must-not-survive
CommandToken=legacy-value-must-not-survive
ConsumerIdentity=legacy-value-must-not-survive
TerminalLeaseScope=legacy-value-must-not-survive
CredentialGenerationId=legacy-value-must-not-survive
SymbolsCsv=EURUSD,USDJPY,GBPUSD,AUDUSD
BarHistorySymbolsCsv=EURUSD,BTCUSD,ETHUSD,LTCUSD,XAUUSD
</expert>
</chart>
"""
    chart.write_text(original, encoding="utf-8")
    key_file = tmp_path / "api-key.txt"
    command_token_file = tmp_path / "command-token.txt"
    key_file.write_text("a" * 64, encoding="ascii")
    command_token_file.write_text("b" * 64, encoding="ascii")

    deploy_source = DEPLOY.read_text(encoding="utf-8")
    canonical_match = re.search(
        r'^\$canonicalScalpSymbolsCsv = "([A-Z,]+)"$',
        deploy_source,
        re.MULTILINE,
    )
    assert canonical_match is not None
    expected_csv = canonical_match.group(1)
    harness = r"""
$source = [IO.File]::ReadAllText($env:DEPLOY_SCRIPT, [Text.Encoding]::UTF8)
$tokens = $null
$parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseInput($source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { throw "deployment script failed to parse" }
foreach ($functionName in @("Resolve-ActiveProfileDir", "Install-BridgeEaAuthFiles")) {
    $definition = $ast.Find({
        param($node)
        $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $functionName
    }, $true)
    if ($null -eq $definition) { throw "deployment function not found" }
    Invoke-Expression $definition.Extent.Text
}
$canonicalScalpSymbolsCsv = $env:EXPECTED_CSV
function Get-Process {
    [CmdletBinding()]
    param([string]$Name)
    return $null
}
$result = Install-BridgeEaAuthFiles `
    -DataDir $env:FIXTURE_DATA_DIR `
    -KeyFile $env:FIXTURE_KEY_FILE `
    -CommandTokenFile $env:FIXTURE_COMMAND_TOKEN_FILE `
    -Consumer "fixture-consumer" `
    -LeaseScope "fixture-terminal" `
    -GenerationId "fixture-generation"
$result | ConvertTo-Json -Compress
"""
    process_env = os.environ.copy()
    process_env.update(
        {
            "DEPLOY_SCRIPT": str(DEPLOY),
            "EXPECTED_CSV": expected_csv,
            "FIXTURE_DATA_DIR": str(tmp_path),
            "FIXTURE_KEY_FILE": str(key_file),
            "FIXTURE_COMMAND_TOKEN_FILE": str(command_token_file),
        }
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoLogo",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            harness,
        ],
        check=False,
        capture_output=True,
        text=True,
        env=process_env,
        cwd=ROOT,
        timeout=20,
    )

    output = f"{completed.stdout}\n{completed.stderr}"
    assert completed.returncode == 0, output
    result = json.loads(completed.stdout.strip())
    assert result["ActiveBridgeCount"] == 1
    assert result["BackupCount"] == 1
    persisted = chart.read_text(encoding="utf-8")
    assert f"SymbolsCsv={expected_csv}" in persisted
    assert f"BarHistorySymbolsCsv={expected_csv}" in persisted
    assert "legacy-value-must-not-survive" not in persisted
    backups = list(active_profile.glob("chart01.chr.bak_bridge_deploy_*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    assert "a" * 64 not in output
    assert "b" * 64 not in output
