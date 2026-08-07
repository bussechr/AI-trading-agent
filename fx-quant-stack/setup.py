"""Package selection for development and the physically isolated runtime wheel."""

from __future__ import annotations

import os
from pathlib import Path
import shutil

from setuptools import find_packages, setup
from setuptools.command.build_py import build_py as _build_py


runtime_distribution = os.environ.get("FXSTACK_BUILD_RUNTIME_DISTRIBUTION", "").strip() == "1"
excluded_packages = (
    [
        "fxstack.backtest",
        "fxstack.backtest.*",
        "fxstack.improve",
        "fxstack.improve.*",
        "fxstack.labels",
        "fxstack.labels.*",
        "fxstack.llm",
        "fxstack.llm.*",
        "fxstack.research",
        "fxstack.research.*",
        # Offline scalp research remains outside the production trust boundary.
        # The signed production strategy is implemented by installed runtime/
        # strategy modules and must never import this research-only package.
        "fxstack.scalp",
        "fxstack.scalp.*",
        "fxstack.rl.envs",
        "fxstack.rl.envs.*",
        "fxstack.security",
        "fxstack.security.*",
    ]
    if runtime_distribution
    else []
)

runtime_excluded_modules = frozenset(
    {
        "fxstack.tasks",
        "fxstack.api.__main__",
        "fxstack.belief.adapters",
        "fxstack.belief.dataset",
        "fxstack.belief.labels",
        "fxstack.belief.outcome_labels",
        "fxstack.data.provider_migration",
        "fxstack.feast.compaction",
        "fxstack.feast.offline_builder",
        "fxstack.feast.parquet_adapter",
        "fxstack.feast.repository",
        "fxstack.mlops.lineage",
        "fxstack.mlops.model_uri",
        "fxstack.mlops.registry",
        "fxstack.mlops.run_context",
        "fxstack.mlops.types",
        "fxstack.models.patchtst",
        "fxstack.orchestration.approvals",
        "fxstack.orchestration.experiments",
        "fxstack.orchestration.promotion",
        "fxstack.orchestration.replay",
        "fxstack.providers.execution.paper",
        "fxstack.providers.paper_execution",
        "fxstack.rl._common",
        "fxstack.rl.evaluate",
        "fxstack.rl.export_replay",
        "fxstack.rl.offline_dataset",
        "fxstack.rl.portfolio_env",
        "fxstack.rl.reward",
        "fxstack.rl.stress_harness",
        "fxstack.rl.trainer",
        "fxstack.rl.train_offline",
        "fxstack.rl.train_online",
        "fxstack.schemas.bars",
        "fxstack.training.activation",
        "fxstack.training.belief",
        "fxstack.training.counterfactual_eval",
        "fxstack.training.datasets",
        "fxstack.training.fingerprint",
        "fxstack.training.lifecycle_validation",
        "fxstack.training.objectives",
        "fxstack.training.phase4_types",
        "fxstack.training.phase5_gates",
        "fxstack.training.promotion",
        "fxstack.training.registry",
        "fxstack.training.release_package",
        "fxstack.training.release_workflow",
        "fxstack.training.research_manifest",
        "fxstack.training.sequence_dataset",
        "fxstack.training.splits",
        "fxstack.training.uncertainty",
        "fxstack.utils.time",
    }
)
runtime_excluded_package_roots = frozenset(
    package for package in excluded_packages if not package.endswith(".*")
)


class RuntimeBuildPy(_build_py):
    """Drop offline modules that share packages with production code."""

    def find_package_modules(self, package: str, package_dir: str):
        modules = super().find_package_modules(package, package_dir)
        if not runtime_distribution:
            return modules
        return [
            item
            for item in modules
            if f"{item[0]}.{item[1]}" not in runtime_excluded_modules
        ]

    def run(self) -> None:
        super().run()
        if not runtime_distribution:
            return

        # setuptools reuses ``build/lib`` across builds. Merely omitting a
        # source module from this run is insufficient: install_lib will still
        # sweep a stale copy from an earlier development build into the wheel.
        # Purge every forbidden module/package from the staged tree after the
        # normal copy phase so a dirty cache cannot weaken physical isolation.
        build_root = Path(self.build_lib)
        for module_name in runtime_excluded_modules:
            module_path = build_root.joinpath(*module_name.split("."))
            for suffix in (".py", ".pyi", ".pyc"):
                module_path.with_suffix(suffix).unlink(missing_ok=True)
        for package_name in runtime_excluded_package_roots:
            package_path = build_root.joinpath(*package_name.split("."))
            if package_path.exists():
                shutil.rmtree(package_path)
        for cache_path in build_root.rglob("__pycache__"):
            shutil.rmtree(cache_path, ignore_errors=True)


setup(
    packages=find_packages(where="src", exclude=excluded_packages),
    # PEP 517/setuptools defaults include-package-data to true for pyproject
    # builds.  Without this explicit override, excluded Python packages can be
    # copied back into the wheel as data because they sit below ``fxstack``.
    include_package_data=False,
    cmdclass={"build_py": RuntimeBuildPy},
)
