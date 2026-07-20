from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.runtime import runner
from fxstack.runtime import startup_preflight
from fxstack.runtime.startup_preflight import RuntimeStartupPreflightError
from fxstack.mlops.local_artifact import resolve_model_artifact_path
from fxstack.settings import Settings


PAIR = "EURUSD"


def _live_settings(**overrides: str) -> Settings:
    values = {
        "FXSTACK_START_PROFILE": "live",
        "FXSTACK_AGENT_MODE": "live",
        "FXSTACK_LIVE_ARMED": "true",
        "FXSTACK_PAIRS": PAIR,
        "FXSTACK_AGENT_LIVE_PAIR_ALLOWLIST": PAIR,
        "FXSTACK_AGENT_LIVE_SLEEVE_ALLOWLIST": "trend_pullback",
        "FXSTACK_AGENT_LIVE_INTENT_ALLOWLIST": "enter",
        "FXSTACK_ADAPTIVE_SHADOW_ENABLED": "true",
        "FXSTACK_ADAPTIVE_EXECUTION_ENABLED": "false",
        "FXSTACK_USE_STRUCTURE_TIMING_SHADOW": "true",
        "FXSTACK_USE_UNCERTAINTY_GATE": "true",
        "FXSTACK_BELIEF_SHADOW_ENABLED": "true",
        "FXSTACK_BELIEF_RUNTIME_REQUIRED": "true",
        "FXSTACK_BELIEF_INFLUENCE_MODE": "hard_gate",
        "FXSTACK_CAMPAIGN_MANAGER_ENABLED": "true",
        "FXSTACK_CAMPAIGN_SHADOW_ONLY": "false",
        "FXSTACK_CAPITAL_GOVERNANCE_ENABLED": "true",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_live_posture_keeps_adaptive_execution_independent() -> None:
    settings = _live_settings(FXSTACK_ADAPTIVE_EXECUTION_ENABLED="false")

    assert startup_preflight.runtime_launch_posture_errors(settings) == []


@pytest.mark.parametrize(
    ("env_name", "expected_fragment"),
    [
        ("FXSTACK_ADAPTIVE_SHADOW_ENABLED", "FXSTACK_ADAPTIVE_SHADOW_ENABLED"),
        ("FXSTACK_USE_STRUCTURE_TIMING_SHADOW", "FXSTACK_USE_STRUCTURE_TIMING_SHADOW"),
        ("FXSTACK_USE_UNCERTAINTY_GATE", "FXSTACK_USE_UNCERTAINTY_GATE"),
        ("FXSTACK_BELIEF_SHADOW_ENABLED", "FXSTACK_BELIEF_SHADOW_ENABLED"),
        ("FXSTACK_BELIEF_RUNTIME_REQUIRED", "FXSTACK_BELIEF_RUNTIME_REQUIRED"),
        ("FXSTACK_CAMPAIGN_MANAGER_ENABLED", "FXSTACK_CAMPAIGN_MANAGER_ENABLED"),
        ("FXSTACK_CAPITAL_GOVERNANCE_ENABLED", "FXSTACK_CAPITAL_GOVERNANCE_ENABLED"),
    ],
)
def test_live_posture_requires_every_binding_entry_producer(
    env_name: str,
    expected_fragment: str,
) -> None:
    settings = _live_settings(**{env_name: "false"})

    errors = startup_preflight.runtime_launch_posture_errors(settings)

    assert any(expected_fragment in error for error in errors), errors


def test_live_posture_requires_binding_belief_and_campaign_modes() -> None:
    belief_errors = startup_preflight.runtime_launch_posture_errors(
        _live_settings(FXSTACK_BELIEF_INFLUENCE_MODE="off")
    )
    campaign_errors = startup_preflight.runtime_launch_posture_errors(
        _live_settings(FXSTACK_CAMPAIGN_SHADOW_ONLY="true")
    )

    assert any("FXSTACK_BELIEF_INFLUENCE_MODE=hard_gate" in item for item in belief_errors)
    assert any("FXSTACK_CAMPAIGN_SHADOW_ONLY=false" in item for item in campaign_errors)


def test_paper_posture_is_unavailable_in_production_runtime() -> None:
    settings = Settings(
        _env_file=None,
        FXSTACK_START_PROFILE="paper",
        FXSTACK_AGENT_MODE="paper",
        FXSTACK_ADAPTIVE_SHADOW_ENABLED="false",
        FXSTACK_USE_STRUCTURE_TIMING_SHADOW="false",
        FXSTACK_USE_UNCERTAINTY_GATE="false",
    )

    assert startup_preflight.runtime_launch_posture_errors(settings) == [
        "FXSTACK_START_PROFILE=paper is unavailable in the production runtime distribution"
    ]


def test_physical_isolation_probe_rejects_importable_research_package() -> None:
    seen: list[str] = []

    def _find_spec(module_name: str) -> object | None:
        seen.append(module_name)
        return object() if module_name == "fxstack.backtest" else None

    errors = startup_preflight.runtime_physical_isolation_errors(
        find_spec=_find_spec
    )

    assert seen == list(startup_preflight.FORBIDDEN_RUNTIME_MODULES)
    assert errors == [
        "runtime distribution must not contain importable module fxstack.backtest"
    ]


@pytest.mark.parametrize(
    "module_name",
    (
        "fxstack.mlops.model_uri",
        "fxstack.models.patchtst",
        "fxstack.providers.execution.paper",
        "mlflow",
    ),
)
def test_physical_isolation_probe_rejects_pruned_simulation_modules(
    module_name: str,
) -> None:
    errors = startup_preflight.runtime_physical_isolation_errors(
        find_spec=lambda candidate: object() if candidate == module_name else None
    )

    assert errors == [
        f"runtime distribution must not contain importable module {module_name}"
    ]


def test_production_artifact_resolver_is_local_only(tmp_path: Path) -> None:
    local = tmp_path / "artifact"
    local.mkdir()

    assert resolve_model_artifact_path(str(local), project_root=tmp_path) == local
    with pytest.raises(
        RuntimeError,
        match="remote_model_artifact_unavailable_in_production_runtime",
    ):
        resolve_model_artifact_path(
            "models:/fx.meta_filter.EURUSD.M5/7",
            project_root=tmp_path,
        )


def test_python_preflight_orders_settings_isolation_then_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    settings = SimpleNamespace(
        validate_for_startup=lambda: events.append("settings") or [],
        start_profile="staged_safe",
        agent_mode="shadow",
        project_root=tmp_path,
        pairs=[PAIR],
        model_activation_manifest=str(tmp_path / "active_models.json"),
    )

    def _find_spec(module_name: str) -> None:
        events.append(f"isolation:{module_name}")
        return None

    def _manifest_preflight(**kwargs: Any) -> dict[str, Any]:
        events.append("manifest")
        assert kwargs["required_pairs"] == [PAIR]
        return {
            "ok": True,
            "read_only": True,
            "manifest_content_sha256": "a" * 64,
        }

    monkeypatch.setattr(
        startup_preflight,
        "preflight_active_model_manifest",
        _manifest_preflight,
    )

    result = startup_preflight.validate_runtime_startup(
        settings,
        find_spec=_find_spec,
    )

    assert events == [
        "settings",
        *(f"isolation:{name}" for name in startup_preflight.FORBIDDEN_RUNTIME_MODULES),
        "manifest",
    ]
    assert result["manifest_content_sha256"] == "a" * 64
    assert result["settings_validated"] is True


def test_direct_runner_rejection_happens_before_bridge_or_service_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    events: list[str] = []
    settings = object()
    monkeypatch.setattr(runner, "get_settings", lambda: settings)

    def _reject(candidate: object) -> dict[str, Any]:
        assert candidate is settings
        events.append("python_preflight")
        raise RuntimeStartupPreflightError("blocked before mutation")

    monkeypatch.setattr(runner, "validate_runtime_startup", _reject)
    monkeypatch.setattr(
        runner,
        "_perform_startup_bridge_checks",
        lambda _settings: events.append("bridge"),
    )

    with pytest.raises(RuntimeStartupPreflightError, match="blocked before mutation"):
        runner.run_loop(equity=10_000.0, sleep_secs=0, feature_root=str(tmp_path))

    assert events == ["python_preflight"]
    source = inspect.getsource(runner.run_loop)
    assert source.index("startup_model_preflight = validate_runtime_startup(s)") < source.index(
        "_perform_startup_bridge_checks(s)"
    )
    assert source.index("_perform_startup_bridge_checks(s)") < source.index(
        "from fxstack.runtime.service import RuntimeService"
    )
    assert source.count(
        'startup_model_preflight.get("manifest_content_sha256")'
    ) == 2
    seed_gate = source.index('stage="manifest_seed"')
    assert seed_gate < source.index('phase="model_load"', seed_gate)
    activation_gate = source.index('stage="activation_consistency"')
    assert activation_gate < source.index('phase="readying_state"', activation_gate)


def _write_activation_manifest(
    path: Path,
    *,
    artifacts: dict[str, dict[str, Any]],
    model_set_id: str = "candidate-v2",
) -> str:
    payload = {
        "schema_version": 1,
        "active_model_sets": {
            PAIR: {
                "enabled": True,
                "model_set_id": model_set_id,
                "registry_path": "mlflow://EURUSD@candidate",
                "artifacts": artifacts,
            }
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_activation_consistency_binds_model_registry_and_artifact_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "regime"
    artifacts = {
        "regime": {
            "path": str(artifact_path),
            "artifact_hash": "a" * 64,
            "content_sha256": "b" * 64,
            "runtime_compatible": True,
        }
    }
    manifest = tmp_path / "active_models.json"
    manifest_sha256 = _write_activation_manifest(manifest, artifacts=artifacts)
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(model_activation_manifest=str(manifest)),
    )
    db_artifacts = json.loads(json.dumps(artifacts))
    db_artifacts["regime"]["artifact_hash"] = "c" * 64
    svc = SimpleNamespace(
        get_active_model_sets=lambda **_kwargs: {
            PAIR: {
                "model_set_id": "different-model-set",
                "registry_path": "mlflow://EURUSD@candidate",
                "artifacts_json": db_artifacts,
            }
        }
    )
    loaded = SimpleNamespace(
        model_set_id="candidate-v2",
        registry_path="mlflow://EURUSD@candidate",
        artifact_identities=runner._artifact_identity_map(
            artifacts,
            project_root=tmp_path,
        ),
    )

    result = runner._activation_consistency(
        svc=svc,
        project_root=tmp_path,
        configured_pairs=[PAIR],
        loaded_model_sets={PAIR: loaded},
        expected_manifest_sha256=manifest_sha256,
    )

    assert result["active_manifest_matches_db"] is False
    assert result["runtime_loaded_matches_db"] is False
    assert result["manifest_db_mismatch_details"][PAIR] == [
        "model_set_id",
        "artifact_identity",
    ]
    assert result["runtime_db_mismatch_details"][PAIR] == [
        "model_set_id",
        "artifact_identity",
    ]
    with pytest.raises(RuntimeError, match="required_model_activation_consistency_failed"):
        runner._require_required_model_startup_consistency(
            settings=SimpleNamespace(require_active_models=True),
            configured_pairs=[PAIR],
            stage="activation_consistency",
            payload=result,
        )


def test_activation_consistency_accepts_exact_required_pair_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    artifacts = {
        "regime": {
            "path": str(tmp_path / "regime"),
            "artifact_hash": "a" * 64,
            "content_sha256": "b" * 64,
            "runtime_compatible": True,
        }
    }
    manifest = tmp_path / "active_models.json"
    manifest_sha256 = _write_activation_manifest(manifest, artifacts=artifacts)
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(model_activation_manifest=str(manifest)),
    )
    artifact_identities = runner._artifact_identity_map(
        artifacts,
        project_root=tmp_path,
    )
    svc = SimpleNamespace(
        get_active_model_sets=lambda **_kwargs: {
            PAIR: {
                "model_set_id": "candidate-v2",
                "registry_path": "mlflow://EURUSD@candidate",
                "artifacts_json": artifacts,
            }
        }
    )
    loaded = SimpleNamespace(
        model_set_id="candidate-v2",
        registry_path="mlflow://EURUSD@candidate",
        artifact_identities=artifact_identities,
    )

    result = runner._activation_consistency(
        svc=svc,
        project_root=tmp_path,
        configured_pairs=[PAIR],
        loaded_model_sets={PAIR: loaded},
        expected_manifest_sha256=manifest_sha256,
    )

    assert result["active_manifest_matches_db"] is True
    assert result["runtime_loaded_matches_db"] is True
    runner._require_required_model_startup_consistency(
        settings=SimpleNamespace(require_active_models=True),
        configured_pairs=[PAIR],
        stage="activation_consistency",
        payload=result,
    )


def test_required_model_seed_failure_is_not_advisory() -> None:
    with pytest.raises(RuntimeError, match="required_model_manifest_seed_failed"):
        runner._require_required_model_startup_consistency(
            settings=SimpleNamespace(require_active_models=True),
            configured_pairs=[PAIR],
            stage="manifest_seed",
            payload={
                "reason": "seeded_partial",
                "pairs": [],
                "failed_pairs": [PAIR],
            },
        )


def test_manifest_seed_rejects_preflight_identity_change_before_service_use(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manifest = tmp_path / "active_models.json"
    _write_activation_manifest(manifest, artifacts={})
    monkeypatch.setattr(
        runner,
        "get_settings",
        lambda: SimpleNamespace(
            pairs=[PAIR],
            model_activation_manifest=str(manifest),
        ),
    )

    class _UntouchedService:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"service must not be used after manifest drift: {name}")

    result = runner._seed_active_model_sets_from_manifest(
        svc=_UntouchedService(),
        project_root=tmp_path,
        expected_manifest_sha256="0" * 64,
    )

    assert result["reason"] == "manifest_identity_changed"
    with pytest.raises(RuntimeError, match="required_model_manifest_seed_failed"):
        runner._require_required_model_startup_consistency(
            settings=SimpleNamespace(require_active_models=True),
            configured_pairs=[PAIR],
            stage="manifest_seed",
            payload=result,
        )
