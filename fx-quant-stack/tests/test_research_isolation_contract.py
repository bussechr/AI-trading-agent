from __future__ import annotations

import ast
import json
from pathlib import Path
import subprocess
import sys
import tomllib

from fxstack.runtime.scalp_engine_identity import SCALP_ENGINE_COMPONENTS
from fxstack.runtime.startup_preflight import FORBIDDEN_RUNTIME_MODULES


REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPO_ROOT / "fx-quant-stack" / "src" / "fxstack"


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: set[str] = set()
    try:
        relative = path.relative_to(PACKAGE_ROOT).with_suffix("")
    except ValueError:
        package_parts: list[str] = []
    else:
        module_parts = ["fxstack", *relative.parts]
        package_parts = module_parts[:-1] if path.name != "__init__.py" else module_parts[:-1]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                keep = max(0, len(package_parts) - (node.level - 1))
                base = package_parts[:keep]
                if node.module:
                    imported.add(".".join([*base, *node.module.split(".")]))
                else:
                    imported.update(".".join([*base, alias.name]) for alias in node.names)
            elif node.module:
                imported.add(node.module)
    return imported


def _python_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _local_module_path(module: str) -> Path | None:
    if module == "fxstack":
        return PACKAGE_ROOT / "__init__.py"
    if not module.startswith("fxstack."):
        return None
    relative = Path(*module.split(".")[1:])
    module_path = PACKAGE_ROOT / relative.with_suffix(".py")
    if module_path.is_file():
        return module_path
    package_path = PACKAGE_ROOT / relative / "__init__.py"
    return package_path if package_path.is_file() else None


def _transitive_violations(start_paths: tuple[Path, ...], forbidden_roots: tuple[str, ...]) -> list[str]:
    queue = list(start_paths)
    seen: set[Path] = set()
    violations: list[str] = []
    while queue:
        path = queue.pop()
        if path in seen:
            continue
        seen.add(path)
        for imported in _imports(path):
            if any(imported == root or imported.startswith(f"{root}.") for root in forbidden_roots):
                violations.append(f"{path.relative_to(REPO_ROOT)} -> {imported}")
            local_path = _local_module_path(imported)
            if local_path is not None and local_path not in seen:
                queue.append(local_path)
    return sorted(set(violations))


def test_production_decision_packages_do_not_import_offline_research() -> None:
    production_roots = (
        PACKAGE_ROOT / "runtime",
        PACKAGE_ROOT / "live",
        PACKAGE_ROOT / "api",
        PACKAGE_ROOT / "strategy",
    )
    violations: list[str] = []
    production_files = [path for root in production_roots for path in _python_files(root)]
    production_files.extend(
        (
            PACKAGE_ROOT / "rl" / "contracts.py",
            PACKAGE_ROOT / "rl" / "checkpoint.py",
            PACKAGE_ROOT / "rl" / "proposal.py",
        )
    )
    for path in production_files:
        for imported in _imports(path):
            if any(
                imported == root or imported.startswith(f"{root}.")
                for root in (
                    "fxstack.backtest",
                    "fxstack.improve",
                    "fxstack.research",
                    "fxstack.scalp",
                )
            ):
                violations.append(f"{path.relative_to(REPO_ROOT)} -> {imported}")
    assert violations == []


def test_runtime_transitive_import_graph_does_not_reach_offline_research() -> None:
    runner = PACKAGE_ROOT / "runtime" / "runner.py"
    assert _transitive_violations(
        (runner,),
        (
            "fxstack.backtest",
            "fxstack.improve",
            "fxstack.research",
            "fxstack.scalp",
            "fxstack.rl._common",
            "fxstack.rl.trainer",
        ),
    ) == []


def test_runtime_rl_checkpoint_module_is_read_only_and_trainer_free() -> None:
    checkpoint = PACKAGE_ROOT / "rl" / "checkpoint.py"
    runtime_consumers = (
        PACKAGE_ROOT / "runtime" / "runner.py",
        PACKAGE_ROOT / "rl" / "proposal.py",
        PACKAGE_ROOT / "rl" / "__init__.py",
        checkpoint,
    )
    forbidden_imports = {"fxstack.rl._common", "fxstack.rl.trainer"}

    for path in runtime_consumers:
        assert _imports(path).isdisjoint(forbidden_imports), path

    checkpoint_tree = ast.parse(
        checkpoint.read_text(encoding="utf-8"),
        filename=str(checkpoint),
    )
    function_names = {
        node.name
        for node in ast.walk(checkpoint_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    write_attributes = {
        node.func.attr
        for node in ast.walk(checkpoint_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "save" not in function_names
    assert "fit_replay_policy" not in function_names
    assert write_attributes.isdisjoint(
        {"mkdir", "open", "replace", "unlink", "write_bytes", "write_text"}
    )
    assert "mlflow" not in checkpoint.read_text(encoding="utf-8").lower()


def test_runner_live_and_service_imports_do_not_load_pruned_research_modules() -> None:
    script = f"""
import importlib
import json
import sys

sys.path.insert(0, {str(PACKAGE_ROOT.parent)!r})
for module_name in (
    "fxstack.runtime.runner",
    "fxstack.runtime.service",
    "fxstack.live.scorer",
    "fxstack.live.policy",
):
    importlib.import_module(module_name)
print(json.dumps(sorted(sys.modules)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
    imported_modules = set(json.loads(completed.stdout.strip().splitlines()[-1]))

    assert "fxstack.providers.execution.paper" not in imported_modules
    assert "fxstack.models.patchtst" not in imported_modules
    assert "fxstack.rl._common" not in imported_modules
    assert "fxstack.rl.trainer" not in imported_modules


def test_runtime_build_filter_keeps_checkpoint_and_drops_rl_writers() -> None:
    setup_path = REPO_ROOT / "fx-quant-stack" / "setup.py"
    setup_source = setup_path.read_text(encoding="utf-8")
    setup_tree = ast.parse(setup_source, filename=str(setup_path))
    excluded_assignment = next(
        node
        for node in setup_tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "runtime_excluded_modules"
            for target in node.targets
        )
    )
    assert isinstance(excluded_assignment.value, ast.Call)
    assert excluded_assignment.value.args
    excluded_modules = set(ast.literal_eval(excluded_assignment.value.args[0]))
    runtime_build_class = next(
        node
        for node in setup_tree.body
        if isinstance(node, ast.ClassDef) and node.name == "RuntimeBuildPy"
    )
    filter_names = {
        node.id
        for node in ast.walk(runtime_build_class)
        if isinstance(node, ast.Name)
    }
    method_names = {
        node.name
        for node in runtime_build_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    called_attributes = {
        node.func.attr
        for node in ast.walk(runtime_build_class)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }

    assert "runtime_excluded_modules" in filter_names
    assert "runtime_excluded_package_roots" in filter_names
    assert "run" in method_names
    assert {"unlink", "rmtree"} <= called_attributes
    assert "fxstack.rl.checkpoint" not in excluded_modules
    assert "fxstack.providers.ig_mt4_catalog" not in excluded_modules
    measured_python_modules = {
        "fxstack." + relative.removesuffix(".py").replace("/", ".")
        for relative in SCALP_ENGINE_COMPONENTS
        if relative.endswith(".py")
    }
    assert measured_python_modules.isdisjoint(excluded_modules)
    assert measured_python_modules.isdisjoint(FORBIDDEN_RUNTIME_MODULES)
    assert "fxstack.runtime.service_contract" in measured_python_modules
    assert "fxstack.rl._common" in excluded_modules
    assert "fxstack.rl.trainer" in excluded_modules


def test_production_runtime_distribution_physically_excludes_research_packages() -> None:
    setup_source = (REPO_ROOT / "fx-quant-stack" / "setup.py").read_text(encoding="utf-8")
    sync_source = (REPO_ROOT / "ops" / "windows" / "01_sync_python.bat").read_text(encoding="utf-8")
    launch_source = (REPO_ROOT / "ops" / "windows" / "21_start_runtime.bat").read_text(encoding="utf-8")

    assert "FXSTACK_BUILD_RUNTIME_DISTRIBUTION" in setup_source
    assert '"fxstack.backtest.*"' in setup_source
    assert '"fxstack.improve.*"' in setup_source
    assert '"fxstack.research.*"' in setup_source
    assert '"fxstack.scalp.*"' in setup_source
    assert "include_package_data=False" in setup_source
    assert "FXSTACK_BUILD_RUNTIME_DISTRIBUTION=1" in sync_source
    assert "pip install -e ." not in sync_source
    assert "runtime_physical_isolation_errors" in sync_source
    assert "--no-install-project --no-dev" in sync_source
    assert "--no-install-package torch" in sync_source
    assert "--no-install-package transformers" in sync_source
    assert "--no-install-package mlflow-skinny" in sync_source
    assert "--no-install-package mlflow-tracing" in sync_source
    assert '"fxstack.mlops.model_uri"' in setup_source
    project = tomllib.loads(
        (REPO_ROOT / "fx-quant-stack" / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )["project"]
    core_names = {
        requirement.split("[", 1)[0]
        .split("<", 1)[0]
        .split(">", 1)[0]
        .split("=", 1)[0]
        .strip()
        .lower()
        for requirement in project["dependencies"]
    }
    assert "mlflow" not in core_names
    assert "torch" not in core_names
    assert "transformers" not in core_names
    assert "pytorch-tcn" not in core_names
    assert "mlflow>=2.16" in project["optional-dependencies"]["external_mlops"]
    assert "torch>=2.6,<2.7" in project["optional-dependencies"]["deep_inference"]
    assert "uv pip install --python" in sync_source
    assert "--reinstall --no-deps ." in sync_source
    assert "active env is source-backed or not isolation-verified" in sync_source
    assert "call :build_side_by_side_uv_env" in sync_source
    assert "-I -B -m fxstack.runtime.model_manifest_preflight" in launch_source
    assert "tools\\preflight_active_models.py" not in launch_source
    assert "-I -u -m fxstack.runtime.runner" in launch_source
    assert "-u -m src.trader.cli runtime run" not in launch_source


def test_runtime_scalp_compatibility_endpoint_is_import_free_and_fail_closed() -> None:
    service_source = (PACKAGE_ROOT / "runtime" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "from fxstack.scalp" not in service_source
    assert "import fxstack.scalp" not in service_source
    assert "cryptography" not in service_source
    assert "_ScalpEntryApproved" not in service_source
    assert "scalp_approval" not in service_source
    assert "scalp_live_ingress_disabled_unvalidated_authority" in service_source


def test_alternate_sequence_shadow_loader_is_absent_from_production() -> None:
    runner_source = (PACKAGE_ROOT / "runtime" / "runner.py").read_text(encoding="utf-8")
    settings_source = (PACKAGE_ROOT / "settings.py").read_text(encoding="utf-8")

    assert "resolve_bundle_manifest_by_alias" not in runner_source
    assert "_load_sequence_shadow_bundle" not in runner_source
    assert "_sequence_shadow_metrics" not in runner_source
    assert "sequence_shadow_enabled" not in runner_source
    assert "FXSTACK_SEQUENCE_SHADOW_ENABLED" not in settings_source
    assert "FXSTACK_CHALLENGER_CONFLICT_MODE" not in settings_source


def test_runtime_module_filter_names_every_removed_write_side_surface() -> None:
    setup_source = (REPO_ROOT / "fx-quant-stack" / "setup.py").read_text(encoding="utf-8")
    startup_source = (PACKAGE_ROOT / "runtime" / "startup_preflight.py").read_text(
        encoding="utf-8"
    )
    for module_name in (
        "fxstack.tasks",
        "fxstack.mlops.model_uri",
        "fxstack.mlops.registry",
        "fxstack.models.patchtst",
        "fxstack.orchestration.experiments",
        "fxstack.orchestration.promotion",
        "fxstack.orchestration.replay",
        "fxstack.providers.execution.paper",
        "fxstack.rl._common",
        "fxstack.rl.trainer",
        "fxstack.training.activation",
        "fxstack.training.release_workflow",
        "fxstack.training.research_manifest",
        "fxstack.rl.train_offline",
        "fxstack.rl.train_online",
    ):
        assert f'"{module_name}"' in setup_source
        assert f'"{module_name}"' in startup_source
    assert '"mlflow"' in startup_source


def test_installed_runtime_uses_only_the_local_artifact_resolver() -> None:
    runner_source = (PACKAGE_ROOT / "runtime" / "runner.py").read_text(
        encoding="utf-8"
    )
    paths_source = (PACKAGE_ROOT / "runtime" / "artifact_paths.py").read_text(
        encoding="utf-8"
    )
    local_source = (PACKAGE_ROOT / "mlops" / "local_artifact.py").read_text(
        encoding="utf-8"
    )

    assert "fxstack.mlops.local_artifact" in runner_source
    assert "fxstack.mlops.local_artifact" in paths_source
    assert "fxstack.mlops.model_uri" not in runner_source
    assert "fxstack.mlops.model_uri" not in paths_source
    assert "import mlflow" not in local_source
    assert "download_artifacts" not in local_source
    assert "remote_model_artifact_unavailable_in_production_runtime" in local_source


def test_offline_research_entrypoints_do_not_import_live_control_planes() -> None:
    research_paths = (
        REPO_ROOT / "tools" / "fxstack_causal_research_backtest.py",
        REPO_ROOT / "tools" / "fxstack_lifecycle_equity_backtest.py",
        REPO_ROOT / "tools" / "build_walk_forward_snapshot.py",
        REPO_ROOT / "tools" / "run_causal_walk_forward.py",
        REPO_ROOT / "tools" / "scalp_causal_walk_forward.py",
        REPO_ROOT / "tools" / "autonomous_improve_loop.py",
        REPO_ROOT / "tools" / "compare_research_runs.py",
        REPO_ROOT / "tools" / "replay_orchestration.py",
        REPO_ROOT / "tools" / "orchestration_experiments.py",
        PACKAGE_ROOT / "orchestration" / "replay.py",
    )
    forbidden_roots = (
        "fxstack.runtime",
        "fxstack.api",
        "fxstack.providers.execution",
        "httpx",
        "psycopg",
        "psycopg2",
        "requests",
        "socket",
        "sqlalchemy",
        "urllib.request",
    )
    for path in research_paths:
        assert path.is_file(), path
    assert _transitive_violations(research_paths, forbidden_roots) == []


def test_improve_loop_transitively_cannot_reach_runtime_or_database_packages() -> None:
    loop = PACKAGE_ROOT / "improve" / "loop.py"
    forbidden_roots = (
        "fxstack.runtime",
        "fxstack.api",
        "fxstack.providers.execution",
        "psycopg",
        "psycopg2",
        "sqlalchemy",
    )
    assert _transitive_violations((loop,), forbidden_roots) == []


def test_operator_plane_cannot_launch_offline_research() -> None:
    """The operator plane was removed outright, which is a stronger guarantee.

    This used to read ``services/operator_plane/openclaw/service.py`` and assert
    it contained no research launchers. The whole plane (2,052 LOC, its tests,
    its ops entrypoint and its doc) has since been deleted, so the property is
    now structural rather than textual: absent code cannot launch anything.
    Kept under the original name because the invariant it guards is unchanged.
    """

    plane_root = REPO_ROOT / "services" / "operator_plane"
    # Assert on SOURCE, not on the directory: an orphaned __pycache__ left behind
    # by the removal is inert bytecode and must not read as a resurrection.
    surviving_source = sorted(str(p.relative_to(REPO_ROOT)) for p in plane_root.rglob("*.py"))
    assert surviving_source == [], (
        f"the operator plane is back: {surviving_source}; either restore the textual "
        "research-launcher assertions or keep it out of the tree"
    )
    # Nothing left in the launcher surface may reference it either.
    launchers = [REPO_ROOT / "launch_all.bat", *(REPO_ROOT / "ops" / "windows").glob("*")]
    offenders = [
        path.name
        for path in launchers
        if path.is_file() and "operator_plane" in path.read_text(encoding="utf-8", errors="ignore").lower()
    ]
    assert offenders == [], f"launchers still reference the removed operator plane: {offenders}"


def test_removed_self_correction_crossover_cannot_be_relaunched() -> None:
    removed_paths = (
        REPO_ROOT / "ops" / "windows" / "29_start_self_correction_loop.bat",
        REPO_ROOT / "tools" / "autonomous_self_correction_supervisor.py",
        PACKAGE_ROOT / "improve" / "factory_bridge.py",
    )
    assert all(not path.exists() for path in removed_paths)

    production_launchers = [REPO_ROOT / "launch_all.bat", *(REPO_ROOT / "ops" / "windows").glob("*")]
    forbidden = ("autonomous_self_correction_supervisor", "agent improve", "factory_bridge")
    violations: list[str] = []
    for path in production_launchers:
        if not path.is_file():
            continue
        source = path.read_text(encoding="utf-8", errors="ignore").lower()
        for fragment in forbidden:
            if fragment in source:
                violations.append(f"{path.relative_to(REPO_ROOT)} -> {fragment}")
    assert violations == []

    cli_source = (REPO_ROOT / "src" / "trader" / "cli.py").read_text(encoding="utf-8")
    assert '"--register"' not in cli_source
    assert '"--experiment-base-dir"' not in cli_source
    assert '"--no-service-upsert"' not in cli_source


def test_research_evidence_cannot_patch_live_canary_authority() -> None:
    removed_paths = (
        REPO_ROOT / "tools" / "enable_eurusd_canary.py",
        REPO_ROOT / "tools" / "patch_manifest_eurusd_canary.py",
    )

    assert all(not path.exists() for path in removed_paths)


def test_legacy_replay_surfaces_are_absent() -> None:
    legacy_label = "digital_" + "t" + "win"
    legacy_type = "t" + "win_types.py"
    legacy_service = "mcp_" + "t" + "win_artefacts"
    paths = (
        REPO_ROOT / "tools" / f"fxstack_{legacy_label}_backtest.py",
        PACKAGE_ROOT / "backtest" / legacy_type,
        REPO_ROOT / "services" / "operator_plane" / legacy_service,
    )
    assert all(not path.exists() for path in paths)


def test_autonomous_research_result_cannot_authorize_runtime_activation() -> None:
    source = (REPO_ROOT / "tools" / "autonomous_improve_loop.py").read_text(encoding="utf-8")

    assert '"research_only": True' in source
    assert '"authorizes_activation": False' in source
    assert '"required_next_stage": "independent_candidate_runtime_validation"' in source
