from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_DOCS = (
    "README.md",
    "QUICKSTART.md",
    "fx-quant-stack/README.md",
    "docs/IG_MT4_SETUP.md",
    "docs/IG_MT4_LIVE_SETUP.md",
    "docs/FULL_SCALE_E2E_RUNBOOK.md",
    "docs/FULL_PROCESS_AUDIT_RUNBOOK.md",
    "docs/SHADOW_DUAL_RUN_RUNBOOK.md",
    "fx-quant-stack/docs/runbooks.md",
    "fx-quant-stack/docs/promotion_gate.md",
)

SOURCE_RESEARCH_DOCS = (
    "docs/OFFLINE_STACK.md",
    "docs/SELF_IMPROVEMENT_LOOP.md",
)

EXPECTED_SOURCE_RESEARCH_COMMANDS = {
    ("agent", "build-dataset"),
    ("agent", "explain"),
    ("agent", "improve"),
    ("agent", "llm-check"),
    ("agent", "metrics"),
    ("agent", "propose"),
    ("agent", "robustness"),
    ("agent", "verify-weights"),
    ("backtest", "export-lean"),
    ("security", "secret"),
    ("security", "validate-offline"),
}

SOURCE_CLI_COMMAND_RE = re.compile(
    r"python -m src\.trader\.cli\s+(agent|backtest|security)\s+([a-z-]+)"
)

STALE_OPERATOR_MARKERS = (
    "src.trader.cli",
    "run_full_scale_e2e.bat",
    "run_canary_shadow.bat",
    "operator-plane.md",
    "VALIDATION_CHECKLIST.md",
)

MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]]+\]\(([^)]+)\)")
CREDENTIAL_FIELD_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?\*\*(?:ig account reference|mt4 login(?: \(account number\))?|account number|server|password)\*\*\s*:"
)


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_public_operator_docs_do_not_route_through_retired_entrypoints() -> None:
    for relative_path in PUBLIC_DOCS:
        text = _read(relative_path)
        for marker in STALE_OPERATOR_MARKERS:
            assert marker not in text, f"{relative_path} still references {marker}"


def test_repo_has_one_authoritative_python_dependency_manifest() -> None:
    for retired in ("pyproject.toml", "poetry.lock", "requirements.txt", "uv.lock"):
        assert not (ROOT / retired).exists(), retired

    manifest = tomllib.loads(_read("fx-quant-stack/pyproject.toml"))
    assert manifest["project"]["name"] == "fx-quant-stack"
    assert (ROOT / "fx-quant-stack" / "uv.lock").is_file()
    assert manifest.get("project", {}).get("scripts", {}) == {}


def test_archived_wpf_dashboard_sources_are_absent() -> None:
    desktop = ROOT / "desktop"
    assert not (desktop / "TradingAgent.Dashboard.sln").exists()
    assert not (desktop / "README.md").exists()
    assert not any(desktop.rglob("*.csproj"))


def test_empty_root_services_package_is_absent() -> None:
    assert not (ROOT / "services" / "__init__.py").exists()


def test_documented_trader_compatibility_calls_are_explicitly_source_only() -> None:
    documented_commands: set[tuple[str, str]] = set()
    for relative_path in SOURCE_RESEARCH_DOCS:
        text = _read(relative_path)
        assert "python -m src.trader.cli" in text
        assert re.search(r"(?m)(?<![-\w])(?:fx-trader|trader)\s+(?:agent|backtest|security)", text) is None
        documented_commands.update(SOURCE_CLI_COMMAND_RE.findall(text))

    assert documented_commands == EXPECTED_SOURCE_RESEARCH_COMMANDS

    e2e = _read("docs/E2E_MT4_TESTING.md")
    assert "python fx-quant-stack/scripts/backtest.py" in e2e
    assert "python tools/fxstack_full_backtest.py" in e2e
    assert "python -m fxstack.runtime.db_tools migrate" in e2e
    assert "python -m uvicorn fxstack.api.app:app" in e2e


def test_public_markdown_links_resolve() -> None:
    missing: list[str] = []
    for relative_path in PUBLIC_DOCS:
        source = ROOT / relative_path
        for raw_target in MARKDOWN_LINK_RE.findall(source.read_text(encoding="utf-8")):
            target = raw_target.strip().strip("<>").split("#", 1)[0]
            if not target or "://" in target or target.startswith(("mailto:", "#")):
                continue
            resolved = (source.parent / unquote(target)).resolve()
            if not resolved.exists():
                missing.append(f"{relative_path} -> {raw_target}")
    assert not missing, "missing public documentation links:\n" + "\n".join(missing)


def test_mt4_setup_is_credential_free_and_uses_operator_placeholders() -> None:
    setup = _read("docs/IG_MT4_SETUP.md")
    retired = _read("docs/IG_MT4_LIVE_SETUP.md")
    assert "<YOUR_MT4_LOGIN>" in setup
    assert "<YOUR_IG_SERVER>" in setup
    assert CREDENTIAL_FIELD_RE.search(setup) is None
    assert CREDENTIAL_FIELD_RE.search(retired) is None
    assert "24_deploy_bridge_ea.bat" in setup
    assert "launch_all.bat endpoints" in setup
    assert "curl http://" not in setup


def test_validation_docs_preserve_production_quarantine_boundary() -> None:
    full_scale = _read("docs/FULL_SCALE_E2E_RUNBOOK.md")
    shadow = _read("docs/SHADOW_DUAL_RUN_RUNBOOK.md")
    process = _read("docs/FULL_PROCESS_AUDIT_RUNBOOK.md")
    assert "nonzero quarantine stub" in full_scale
    assert "external isolated" in full_scale.lower()
    assert "python tools/shadow_dual_run.py" in shadow
    assert "--pair <PAIR>" in shadow
    assert "--model-manifest <ISOLATED_MODEL_MANIFEST>" in shadow
    assert "--require-nonzero-entries" not in process
    assert "production host runs one baseline" in process.lower().replace("`", "")
