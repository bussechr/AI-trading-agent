from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_ci_covers_locked_python_and_dashboard_contracts() -> None:
    workflow = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    required = (
        "uv sync --project fx-quant-stack --extra dev --extra security --extra market_data_download --extra external_mlops --extra deep_inference --frozen",
        "ruff check --ignore E402",
        "tools/audit_agent_nav_graph.py",
        "uv run --project fx-quant-stack python -m pytest -q tests",
        "fxstack-tests:",
        "shard: [0, 1, 2]",
        "working-directory: fx-quant-stack",
        'files = sorted(Path("tests").glob("test_*.py"))',
        "index % 3 == shard",
        'pytest.main(["-q", *selected])',
        "pnpm install --frozen-lockfile",
        "pnpm doctor",
        "pnpm lint",
        "pnpm exec tsc --noEmit",
        "node --test tests/*.mjs",
        "pnpm build",
    )
    for command in required:
        assert command in workflow

    forbidden_mutations = (
        "launch_all.bat",
        "21_start_runtime.bat",
        "20_start_bridge.bat",
        "19_start_mt4.ps1",
    )
    for command in forbidden_mutations:
        assert command not in workflow


def test_dashboard_package_manager_matches_ci() -> None:
    package = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["packageManager"] == "pnpm@10.6.2"
