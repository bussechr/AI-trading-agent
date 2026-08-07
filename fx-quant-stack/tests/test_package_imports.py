from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


LAZY_EXPORT_PACKAGES = (
    "fxstack.providers",
    "fxstack.providers.execution",
    "fxstack.providers.history",
    "fxstack.providers.market",
    "fxstack.improve",
    "fxstack.validation",
    "fxstack.rl",
    "fxstack.rl.envs",
    "fxstack.backtest.pnl",
    "fxstack.portfolio",
    "fxstack.belief",
    "fxstack.mlops",
    "fxstack.llm",
    "fxstack.orchestration",
    "fxstack.orchestration.agents.committee",
    "fxstack.risk",
    "fxstack.research",
    "fxstack.security",
    "fxstack.scalp",
    "fxstack.feast",
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_child(statement: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", statement],
        check=False,
        capture_output=True,
        text=True,
        cwd=PROJECT_ROOT,
    )


def test_lightweight_submodule_import_defers_settings_stack() -> None:
    check = _run_child(
        "import sys; import fxstack.runtime.sqlite_url; "
        "assert 'fxstack.settings' not in sys.modules; "
        "assert 'pydantic' not in sys.modules; "
        "assert 'pydantic_settings' not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_policy_session_string_fast_path_preserves_cold_pandas_import() -> None:
    check = _run_child(
        "import sys; import fxstack.live.policy as policy; "
        "assert 'fxstack.features.session_contract' not in sys.modules; "
        "assert 'pandas' not in sys.modules; "
        "assert policy.normalize_session_bucket('ny') == 'new_york'; "
        "assert 'fxstack.features.session_contract' in sys.modules; "
        "assert 'pandas' not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_protocol_identity_does_not_import_api_schema_stack() -> None:
    check = _run_child(
        "import sys; from fxstack.api import protocol_identity; "
        "assert protocol_identity.BRIDGE_PROTOCOL_VERSION == 'v3.0.0'; "
        "assert 'fxstack.api.wire' not in sys.modules; "
        "assert 'pydantic' not in sys.modules; "
        "from fxstack.api.wire import BRIDGE_PROTOCOL_VERSION; "
        "assert BRIDGE_PROTOCOL_VERSION is "
        "protocol_identity.BRIDGE_PROTOCOL_VERSION"
    )

    assert check.returncode == 0, check.stderr


def test_scalp_loop_defers_api_schema_stack() -> None:
    check = _run_child(
        "import sys; import fxstack.runtime.scalp_live_loop; "
        "assert 'fxstack.api.wire' not in sys.modules; "
        "assert 'pydantic' not in sys.modules; "
        "assert 'fxstack.runtime.service' not in sys.modules; "
        "assert 'fxstack.runtime.postgres_store' not in sys.modules; "
        "assert 'fxstack.settings' not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_retired_demo_execution_probe_has_no_shipped_module() -> None:
    module_path = PROJECT_ROOT / "src" / "fxstack" / "runtime" / "scalp_demo_execution_probe.py"
    assert not module_path.exists()

    check = _run_child(
        "import importlib.util; "
        "assert importlib.util.find_spec('fxstack.runtime.scalp_demo_execution_probe') is None"
    )
    assert check.returncode == 0, check.stderr


def test_final_entry_approval_contract_defers_service_and_scalp_authority() -> None:
    check = _run_child(
        "import sys; "
        "from fxstack.runtime.service_contract import FinalEntryApproval; "
        "deferred={'fxstack.runtime.service', 'fxstack.runtime.postgres_store', "
        "'fxstack.runtime.scalp_execution_authority', 'fxstack.settings', "
        "'pydantic', 'pydantic_settings'}; "
        "assert not (deferred & set(sys.modules)); "
        "payload={'symbol':'EURUSD','cmd':'BUY','lots':0.1,"
        "'correlation_id':'c','trace_id':'t',"
        "'orchestration_meta_json':{'trace_id':'t','authority_revision':1,"
        "'adaptive_sleeve':'adaptive'}}; "
        "approval=FinalEntryApproval(pair='EURUSD',side='BUY',"
        "risk_approved_payload={'symbol':'EURUSD','cmd':'BUY','lots':0.1},"
        "canonical_ready=True,governed_allowed=True,rollout_active=True,"
        "rollout_mode='live',rollout_pair_allowlisted=True,"
        "correlation_id='c',trace_id='t',broker_account_mode='demo',"
        "broker_account_scope='scope',authority_revision=1,sleeve='adaptive'); "
        "assert approval.validation_error(payload) == ''; "
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


def test_release_authority_config_hash_defers_evidence_and_trust_stacks() -> None:
    check = _run_child(
        "import sys; from types import SimpleNamespace; "
        "from fxstack.runtime import release_authority; "
        "deferred={'fxstack.runtime.release_contract', "
        "'fxstack.runtime.release_trust'}; "
        "assert not (deferred & set(sys.modules)); "
        "digest=release_authority.runtime_config_sha256("
        "SimpleNamespace(data_provider='mt4')); "
        "assert len(digest) == 64; "
        "assert not (deferred & set(sys.modules)); "
        "assert release_authority.is_sha256(digest); "
        "assert 'fxstack.runtime.release_contract' in sys.modules; "
        "assert 'fxstack.runtime.release_trust' not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_scalp_authority_defers_runtime_release_until_projection() -> None:
    check = _run_child(
        "import sys\n"
        "from fxstack.runtime import scalp_execution_authority as authority\n"
        "release_deferred={'fxstack.runtime.mtvclc_runtime_release', "
        "'fxstack.runtime.mtvclc_validation_evidence_v3'}\n"
        "unused='fxstack.runtime.scalp_validation_evidence'\n"
        "assert not (release_deferred & set(sys.modules))\n"
        "assert unused not in sys.modules\n"
        "try:\n"
        "    authority.expectation_from_mtvclc_runtime_release("
        "object(), runtime_boot_id='boot', authority_revision=1)\n"
        "except ValueError as exc:\n"
        "    assert str(exc) == "
        "'scalp_authority_runtime_release_verification_required'\n"
        "else:\n"
        "    raise AssertionError('invalid verification accepted')\n"
        "assert release_deferred <= set(sys.modules)\n"
        "assert unused not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_runtime_service_defers_runtime_release_until_authority_mutation() -> None:
    check = _run_child(
        "import sys\n"
        "from fxstack.runtime.service import RuntimeService\n"
        "deferred={'fxstack.runtime.mtvclc_runtime_release', "
        "'fxstack.runtime.mtvclc_validation_evidence_v3'}\n"
        "settings='fxstack.settings'\n"
        "assert not (deferred & set(sys.modules))\n"
        "assert settings not in sys.modules\n"
        "service=RuntimeService.__new__(RuntimeService)\n"
        "service.get_state=lambda: {}\n"
        "result=service.compare_and_set_production_scalp_authority(\n"
        "    next_authority={}, validation_verification=object())\n"
        "assert result['reason'] == 'scalp_validation_witness_missing'\n"
        "assert deferred <= set(sys.modules)\n"
        "assert settings not in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_postgres_store_defers_settings_until_first_settings_operation() -> None:
    check = _run_child(
        "import sys\n"
        "from fxstack.runtime import postgres_store\n"
        "settings='fxstack.settings'\n"
        "assert settings not in sys.modules\n"
        "assert postgres_store._get_settings() is not None\n"
        "assert settings in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_root_settings_exports_remain_backward_compatible() -> None:
    check = _run_child(
        "import fxstack; from fxstack import Settings, get_settings; "
        "from fxstack.settings import Settings as DirectSettings; "
        "from fxstack.settings import get_settings as direct_get_settings; "
        "assert Settings is DirectSettings; "
        "assert get_settings is direct_get_settings; "
        "assert fxstack.Settings is DirectSettings; "
        "assert fxstack.get_settings is direct_get_settings; "
        "assert {'Settings', 'get_settings'} <= set(dir(fxstack))"
    )

    assert check.returncode == 0, check.stderr


def test_root_package_still_resolves_submodules() -> None:
    check = _run_child(
        "import sys; from fxstack import tasks; "
        "assert tasks.__name__ == 'fxstack.tasks'; "
        "deferred={'pandas','numpy','sklearn','hmmlearn','torch','xgboost',"
        "'fxstack.settings','fxstack.io.parquet_store',"
        "'fxstack.training.lifecycle_validation','fxstack.training.belief'}; "
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


def test_train_all_help_defers_operation_specific_training_stacks() -> None:
    check = _run_child(
        "import runpy,sys\n"
        "sys.argv=['train_all.py','--help']\n"
        "try:\n"
        "    runpy.run_path('scripts/train_all.py',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'pandas','numpy','sklearn','hmmlearn','torch','xgboost',"
        "'fxstack.settings','fxstack.io.parquet_store',"
        "'fxstack.feast.compaction','fxstack.feast.repository',"
        "'fxstack.backtest.harness','fxstack.mlops.registry'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize(
    "script_name",
    (
        "train_intraday_xgb.py",
        "train_swing_xgb.py",
        "train_regime.py",
        "train_deep_stale.py",
        "train_swing_transformer.py",
        "train_intraday_tcn.py",
    ),
)
def test_focused_training_help_defers_runtime_dependencies(script_name: str) -> None:
    check = _run_child(
        "import runpy,sys\n"
        f"sys.argv=[{script_name!r},'--help']\n"
        "try:\n"
        f"    runpy.run_path('scripts/{script_name}',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'pandas','numpy','sklearn','hmmlearn','torch','xgboost',"
        "'fxstack.settings','fxstack.tasks','fxstack.io.parquet_store',"
        "'fxstack.models.intraday_xgb','fxstack.models.swing_xgb',"
        "'fxstack.models.regime_hmm'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("script_name", ("activate_models.py", "gpu_check.py", "preflight.py"))
def test_external_control_help_defers_settings_and_operation_stacks(script_name: str) -> None:
    check = _run_child(
        "import runpy,sys\n"
        f"sys.argv=[{script_name!r},'--help']\n"
        "try:\n"
        f"    runpy.run_path('scripts/{script_name}',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'torch','sqlalchemy','mlflow','fxstack.settings',"
        "'fxstack.training.activation','fxstack.training.activation_cli',"
        "'fxstack.training.environment'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


def test_score_live_help_defers_bridge_scoring_and_model_stacks() -> None:
    check = _run_child(
        "import runpy,sys\n"
        "sys.argv=['score_live.py','--help']\n"
        "try:\n"
        "    runpy.run_path('scripts/score_live.py',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'pandas','numpy','sklearn','hmmlearn','xgboost',"
        "'fxstack.settings','fxstack.data.live_quotes','fxstack.io.parquet_store',"
        "'fxstack.live.policy','fxstack.live.scorer',"
        "'fxstack.models.intraday_xgb','fxstack.models.meta_filter',"
        "'fxstack.models.regime_hmm','fxstack.models.swing_xgb'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("script_name", ("build_features.py", "build_labels.py", "backtest.py"))
def test_data_pipeline_help_defers_operation_stacks(script_name: str) -> None:
    check = _run_child(
        "import runpy,sys\n"
        f"sys.argv=[{script_name!r},'--help']\n"
        "try:\n"
        f"    runpy.run_path('scripts/{script_name}',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'pandas','numpy','fxstack.settings','fxstack.io.parquet_store',"
        "'fxstack.data.ingest','fxstack.features.build',"
        "'fxstack.labels.triple_barrier','fxstack.backtest.engine',"
        "'fxstack.backtest.reports'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize(
    "script_name",
    (
        "migrate_provider_partitions.py",
        "remediate_state_snapshot.py",
        "ingest_bars.py",
    ),
)
def test_maintenance_cli_help_defers_operation_stacks(script_name: str) -> None:
    check = _run_child(
        "import runpy,sys\n"
        f"sys.argv=[{script_name!r},'--help']\n"
        "try:\n"
        f"    runpy.run_path('scripts/{script_name}',run_name='__main__')\n"
        "except SystemExit as exc:\n"
        "    assert exc.code == 0\n"
        "deferred={'pandas','numpy','requests','sqlalchemy','fxstack.settings',"
        "'fxstack.data.ingest','fxstack.data.provider_migration',"
        "'fxstack.io.parquet_store','fxstack.runtime.service'}\n"
        "assert not (deferred & set(sys.modules))"
    )

    assert check.returncode == 0, check.stderr


def test_deferred_pandas_proxy_hydrates_once_on_first_use() -> None:
    check = _run_child(
        "import sys; from fxstack._lazy import lazy_pandas; "
        "assert 'pandas' not in sys.modules; "
        "series_type = lazy_pandas.Series; "
        "assert 'pandas' in sys.modules; "
        "assert series_type is sys.modules['pandas'].Series; "
        "assert lazy_pandas.Series is series_type; "
        "sentinel = object(); sys.modules['pandas'].read_parquet = sentinel; "
        "assert lazy_pandas.read_parquet is sentinel"
    )

    assert check.returncode == 0, check.stderr


def test_deferred_numpy_proxy_hydrates_once_on_first_use() -> None:
    check = _run_child(
        "import sys; from fxstack._lazy import lazy_numpy; "
        "assert 'numpy' not in sys.modules; "
        "array_type = lazy_numpy.ndarray; "
        "assert 'numpy' in sys.modules; "
        "assert array_type is sys.modules['numpy'].ndarray; "
        "assert lazy_numpy.ndarray is array_type; "
        "sentinel = object(); sys.modules['numpy'].testing = sentinel; "
        "assert lazy_numpy.testing is sentinel"
    )

    assert check.returncode == 0, check.stderr


def test_deferred_callable_tracks_the_loaded_implementation() -> None:
    check = _run_child(
        "import sys; from fxstack._lazy import deferred_callable; "
        "assert 'fractions' not in sys.modules; "
        "make_fraction = deferred_callable('fractions', 'Fraction'); "
        "assert str(make_fraction(1, 2)) == '1/2'; "
        "assert 'fractions' in sys.modules; "
        "sentinel = object(); "
        "sys.modules['fractions'].Fraction = lambda *_args: sentinel; "
        "assert make_fraction(1, 3) is sentinel"
    )

    assert check.returncode == 0, check.stderr


def test_deferred_attribute_supports_construction_and_class_attributes() -> None:
    check = _run_child(
        "import sys; from fxstack._lazy import deferred_attribute; "
        "assert 'fractions' not in sys.modules; "
        "Fraction=deferred_attribute('fractions','Fraction'); "
        "assert Fraction.__name__ == 'Fraction'; "
        "assert str(Fraction(1,2)) == '1/2'; "
        "sentinel=object(); "
        "sys.modules['fractions'].Fraction=lambda *_args: sentinel; "
        "assert Fraction(1,3) is sentinel"
    )

    assert check.returncode == 0, check.stderr


def test_runner_settings_wrapper_hydrates_once_on_first_use() -> None:
    check = _run_child(
        "import sys; import fxstack.runtime.runner as runner; "
        "assert 'fxstack.settings' not in sys.modules; "
        "assert 'pydantic_settings' not in sys.modules; "
        "settings = runner.get_settings(); "
        "assert 'fxstack.settings' in sys.modules; "
        "assert 'pydantic_settings' in sys.modules; "
        "assert runner.get_settings() is settings"
    )

    assert check.returncode == 0, check.stderr


def test_runner_defers_unused_data_feature_and_model_stack_planes() -> None:
    deferred_modules = (
        "fxstack.belief.cross_pair",
        "fxstack.data.live_quotes",
        "fxstack.feast.online_features",
        "fxstack.feast.push",
        "fxstack.features.fx_lifecycle",
        "fxstack.features.multi_tf_contract",
        "fxstack.io.parquet_store",
        "fxstack.providers.registry",
        "fxstack.rl.proposal",
        "fxstack.rl.checkpoint",
        "fxstack.live.scorer",
        "fxstack.portfolio.allocator",
        "fxstack.portfolio.book",
        "fxstack.portfolio.correlation",
        "fxstack.runtime.managed_state",
        "fxstack.runtime.decisions",
        "fxstack.runtime.orchestration_bridge",
        "fxstack.risk.contracts",
        "fxstack.risk.envelope",
        "fxstack.risk.kernel",
        "fxstack.risk.sizing",
        "fxstack.strategy.adaptive_policy",
        "fxstack.strategy.allocator",
        "fxstack.strategy.allocator_types",
        "fxstack.strategy.campaign",
        "fxstack.strategy.campaign_types",
        "fxstack.strategy.complementarity",
        "fxstack.strategy.desk_overlay",
        "fxstack.strategy.desk_overlay_types",
        "fxstack.strategy.sleeve_governance",
    )
    check = _run_child(
        "import sys; import fxstack.runtime.runner as runner; "
        f"deferred={deferred_modules!r}; "
        "assert not ({name for name in deferred if name in sys.modules}); "
        "assert runner.provider_roles_from_settings(type('S', (), {})()) == {"
        "'history_provider': 'dukascopy', "
        "'market_data_provider': 'mt4_bridge', "
        "'execution_provider': 'mt4'}; "
        "assert 'fxstack.providers.registry' in sys.modules; "
        "assert runner.playbook_to_sleeve('') == 'no_trade'; "
        "assert not ({name for name in deferred if "
        "name != 'fxstack.providers.registry' and name in sys.modules}); "
        "store=runner.ParquetStore('.'); "
        "assert type(store).__module__ == 'fxstack.io.parquet_store'; "
        "assert 'fxstack.io.parquet_store' in sys.modules"
    )

    assert check.returncode == 0, check.stderr


def test_shared_strategy_constants_preserve_legacy_imports() -> None:
    check = _run_child(
        "from fxstack.strategy import constants; "
        "from fxstack.strategy import adaptive_policy, allocator, campaign; "
        "assert adaptive_policy.PLAYBOOK_TREND_PULLBACK == "
        "constants.PLAYBOOK_TREND_PULLBACK; "
        "assert campaign.CAMPAIGN_STATE_ABANDONED == "
        "constants.CAMPAIGN_STATE_ABANDONED; "
        "assert allocator.playbook_to_sleeve is constants.playbook_to_sleeve; "
        "assert allocator.playbook_to_sleeve('') == constants.PLAYBOOK_NO_TRADE"
    )

    assert check.returncode == 0, check.stderr


def test_shared_risk_constants_preserve_legacy_kernel_imports() -> None:
    check = _run_child(
        "from fxstack.risk import constants; "
        "from fxstack.risk import kernel; "
        "assert kernel.ROLLOUT_EXECUTION_MODES is "
        "constants.ROLLOUT_EXECUTION_MODES; "
        "assert kernel.ROLLOUT_BUDGET_THROTTLED_MODES is "
        "constants.ROLLOUT_BUDGET_THROTTLED_MODES; "
        "assert constants.ROLLOUT_EXECUTION_MODES == {'canary', 'live'}; "
        "assert constants.ROLLOUT_BUDGET_THROTTLED_MODES == {'canary'}"
    )

    assert check.returncode == 0, check.stderr


def test_mt4_bridge_requests_proxy_hydrates_on_first_http_access() -> None:
    check = _run_child(
        "import sys; from fxstack.providers.market import mt4_bridge; "
        "assert 'requests' not in sys.modules; "
        "request_get = mt4_bridge.requests.get; "
        "assert 'requests' in sys.modules; "
        "assert request_get is sys.modules['requests'].get"
    )

    assert check.returncode == 0, check.stderr


@pytest.mark.parametrize("package_name", LAZY_EXPORT_PACKAGES)
def test_package_exports_are_lazy_and_backward_compatible(
    package_name: str,
) -> None:
    check = _run_child(
        "import importlib, sys; "
        f"package = importlib.import_module({package_name!r}); "
        "children = {name for name in sys.modules "
        "if name.startswith(package.__name__ + '.')}; "
        "assert not children, children; "
        "assert set(package.__all__) == set(package._EXPORTS); "
        "assert set(package.__all__) <= set(dir(package)); "
        "resolved = {name: getattr(package, name) "
        "for name in package.__all__}; "
        "targets = {name: (target if isinstance(target, tuple) else (target, name)) "
        "for name, target in package._EXPORTS.items()}; "
        "assert all(value is getattr(importlib.import_module(targets[name][0]), targets[name][1]) "
        "for name, value in resolved.items()); "
        "assert all(getattr(package, name) is value "
        "for name, value in resolved.items())"
    )

    assert check.returncode == 0, check.stderr
