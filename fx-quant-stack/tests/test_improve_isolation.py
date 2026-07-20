"""Pin the self-improvement loop to an offline, file-only trust boundary."""

from __future__ import annotations

import ast
from pathlib import Path

from fxstack.improve.loop import run_improvement_loop


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "fx-quant-stack" / "src" / "fxstack"


def _absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.add(node.module)
    return imports


def test_production_self_correction_crossover_surfaces_are_absent() -> None:
    removed = (
        REPO_ROOT / "ops" / "windows" / "29_start_self_correction_loop.bat",
        REPO_ROOT / "tools" / "autonomous_self_correction_supervisor.py",
        PACKAGE_ROOT / "improve" / "factory_bridge.py",
    )
    assert all(not path.exists() for path in removed)


def test_improve_package_has_no_live_control_plane_or_database_import() -> None:
    forbidden_roots = (
        "fxstack.runtime",
        "fxstack.api",
        "sqlalchemy",
        "psycopg",
        "psycopg2",
    )
    violations: list[str] = []
    for path in sorted((PACKAGE_ROOT / "improve").rglob("*.py")):
        for imported in _absolute_imports(path):
            if any(imported == root or imported.startswith(f"{root}.") for root in forbidden_roots):
                violations.append(f"{path.relative_to(REPO_ROOT)} -> {imported}")
        source = path.read_text(encoding="utf-8")
        if "upsert_experiment_proposal" in source or "RuntimeService" in source:
            violations.append(f"{path.relative_to(REPO_ROOT)} -> runtime database symbol")
    assert violations == []


def test_improvement_result_is_file_only_advisory_evidence(tmp_path: Path) -> None:
    result = run_improvement_loop(iterations=4, seed=17, artifact_dir=tmp_path)

    assert (tmp_path / "best_config.json").is_file()
    assert (tmp_path / "summary.json").is_file()
    assert (tmp_path / "proposal.json").is_file()
    assert (tmp_path / "reflection_memory.json").is_file()
    assert not (tmp_path / "experiment_lineage.json").exists()
    assert "registration" not in result.as_dict()
    isolation = result.experiment_proposal["evaluation_plan"]["isolation"]
    assert isolation["research_only"] is True
    assert isolation["authorizes_activation"] is False
    assert isolation["authorizes_runtime_registration"] is False
