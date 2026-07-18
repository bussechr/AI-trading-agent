from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.belief.engine import load_directional_belief_model_set
from fxstack.features.session_contract import feature_contract_metadata
from fxstack.models._xgb_base import XGBBinaryModel
from fxstack.models._xgb_multiclass import XGBMulticlassModel
from fxstack.models.artifact_contract import (
    ARTIFACT_PAYLOAD_DIGEST_KEY,
    artifact_lock,
    artifact_payload_digest,
    stamp_artifact_payload_digest,
    validate_artifact_contract,
)
from fxstack.models.belief_horizon_xgb import BeliefHorizonXGB
from fxstack.models.belief_scenario_xgb import BeliefScenarioXGB
from fxstack.models.intraday_tcn import IntradayTCN
from fxstack.models.patchtst import SwingPatchTST
from fxstack.models.swing_transformer import SwingTransformer
from fxstack.mlops.model_uri import resolve_model_artifact_path
from fxstack.runtime import runner as runner_module
from fxstack.training.activation import latest_registry_for_pair, write_manifest
from fxstack.training.registry import ArtifactRegistry


def _write_artifact(path: Path, *, payload: bytes = b"weights-v1") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    (path / "weights.bin").write_bytes(payload)
    (path / "reports").mkdir()
    (path / "reports" / "validation.json").write_text(
        json.dumps({"status": "eligible"}, sort_keys=True),
        encoding="utf-8",
    )
    (path / "meta.json").write_text(
        json.dumps({"name": path.name, **feature_contract_metadata()}),
        encoding="utf-8",
    )
    stamp_artifact_payload_digest(path)
    return path


def _artifact_ref(path: Path) -> dict[str, str]:
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    return {
        "path": str(path),
        "artifact_hash": str(meta[ARTIFACT_PAYLOAD_DIGEST_KEY]),
    }


def _write_belief_v1_artifact(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    for component_name in (
        "scenario_xgb",
        "horizon_short_xgb",
        "horizon_trade_xgb",
        "horizon_structural_xgb",
    ):
        component = path / component_name
        component.mkdir()
        (component / "model.bin").write_bytes(component_name.encode("utf-8"))
        (component / "meta.json").write_text(
            json.dumps({"name": component_name, **feature_contract_metadata()}),
            encoding="utf-8",
        )
        stamp_artifact_payload_digest(component)
    (path / "meta.json").write_text(
        json.dumps(
            {
                "model_version": "belief-test-v1",
                "belief_contract": "directional_belief_v1",
                **feature_contract_metadata(),
            }
        ),
        encoding="utf-8",
    )
    stamp_artifact_payload_digest(path)
    return path


def test_payload_digest_is_portable_and_binds_reports(tmp_path: Path) -> None:
    left = _write_artifact(tmp_path / "left" / "artifact")
    right = _write_artifact(tmp_path / "right" / "artifact")

    assert artifact_payload_digest(left) == artifact_payload_digest(right)
    assert validate_artifact_contract(left, label="left")
    assert validate_artifact_contract(right, label="right")

    (right / "reports" / "validation.json").write_text(
        json.dumps({"status": "rejected"}, sort_keys=True),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="artifact_payload_digest_mismatch:right"):
        validate_artifact_contract(right, label="right")


def test_legacy_unbound_artifact_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "legacy"
    path.mkdir()
    (path / "weights.bin").write_bytes(b"legacy")
    (path / "meta.json").write_text(
        json.dumps(feature_contract_metadata()),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="artifact_payload_contract_mismatch:legacy"):
        validate_artifact_contract(path, label="legacy")


def test_semantic_and_nested_metadata_are_bound_to_model_identity(
    tmp_path: Path,
) -> None:
    artifact = _write_artifact(tmp_path / "semantic")
    meta_path = artifact / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["params"] = {"max_depth": 99}
    meta_path.write_text(json.dumps(meta), encoding="utf-8")

    with pytest.raises(ValueError, match="artifact_payload_digest_mismatch:semantic"):
        validate_artifact_contract(artifact, label="semantic")

    belief = _write_belief_v1_artifact(tmp_path / "belief-semantic")
    child_meta_path = belief / "scenario_xgb" / "meta.json"
    child_meta = json.loads(child_meta_path.read_text(encoding="utf-8"))
    child_meta["scenario_labels"] = ["tampered"]
    child_meta_path.write_text(json.dumps(child_meta), encoding="utf-8")
    with pytest.raises(
        ValueError,
        match="artifact_payload_digest_mismatch:belief-semantic",
    ):
        validate_artifact_contract(belief, label="belief-semantic")

    wrong_family = _write_artifact(tmp_path / "wrong_family")
    with pytest.raises(ValueError, match="artifact_model_name_mismatch"):
        XGBBinaryModel.load(wrong_family)


def test_registered_hash_rejects_mutable_path_and_uses_exact_uri(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.mlops import model_uri as model_uri_module
    from fxstack.mlops.registry import _bind_ref_to_registered_version
    from fxstack.mlops.types import ModelVersionRef

    local = _write_artifact(tmp_path / "local" / "model", payload=b"local")
    registered = _write_artifact(
        tmp_path / "registered" / "model",
        payload=b"registered",
    )
    expected_digest = _artifact_ref(registered)["artifact_hash"]

    fake_mlflow = SimpleNamespace(
        artifacts=SimpleNamespace(
            download_artifacts=lambda **_: str(registered),
        ),
        set_tracking_uri=lambda _value: None,
        set_registry_uri=lambda _value: None,
    )
    monkeypatch.setattr(
        model_uri_module,
        "get_settings",
        lambda: SimpleNamespace(
            project_root=tmp_path,
            mlflow_enabled=True,
            mlflow_tracking_uri="sqlite:///tracking.db",
            mlflow_registry_uri="sqlite:///tracking.db",
            mlflow_cache_root=tmp_path / "cache",
        ),
    )
    monkeypatch.setattr(model_uri_module, "_get_mlflow", lambda: fake_mlflow)

    resolved = resolve_model_artifact_path(
        {
            "path": str(local),
            "model_uri": "models:/fx.swing_xgb.EURUSD.D/7",
            "model_name": "fx.swing_xgb.EURUSD.D",
            "model_version": "7",
            "artifact_hash": expected_digest,
        },
        project_root=tmp_path,
    )

    assert resolved == registered.resolve()
    with pytest.raises(ValueError, match="artifact_registry_uri_moving_alias"):
        resolve_model_artifact_path(
            {
                "path": str(registered),
                "model_uri": "models:/fx.swing_xgb.EURUSD.D@champion",
                "model_name": "fx.swing_xgb.EURUSD.D",
                "model_version": "7",
                "artifact_hash": expected_digest,
            },
            project_root=tmp_path,
        )
    with pytest.raises(RuntimeError, match="missing_registered_tag.*component_key"):
        _bind_ref_to_registered_version(
            ref=ModelVersionRef(
                component_key="swing_xgb",
                pair="EURUSD",
                timeframe="D",
                model_family="swing_xgb",
                model_name="fx.swing_xgb.EURUSD.D",
                model_version="7",
                bundle_run_id="bundle-7",
                artifact_hash=expected_digest,
            ),
            component_key="swing_xgb",
            alias="champion",
            current=SimpleNamespace(
                version="7",
                run_id="run-7",
                tags={
                    "fxstack.bundle_run_id": "bundle-7",
                    "fxstack.artifact_hash": expected_digest,
                },
            ),
            bundle_run_id="bundle-7",
            trusted_pair="EURUSD",
            trusted_timeframe="D",
        )
    with pytest.raises(
        RuntimeError,
        match="component_ref_identity_mismatch:swing_xgb:pair",
    ):
        _bind_ref_to_registered_version(
            ref=ModelVersionRef(
                component_key="swing_xgb",
                pair="GBPUSD",
                timeframe="D",
                model_family="swing_xgb",
                model_name="fx.swing_xgb.GBPUSD.D",
                model_version="7",
                bundle_run_id="bundle-7",
                artifact_hash=expected_digest,
            ),
            component_key="swing_xgb",
            alias="champion",
            current=SimpleNamespace(
                name="fx.swing_xgb.GBPUSD.D",
                version="7",
                run_id="run-7",
                tags={
                    "fxstack.bundle_run_id": "bundle-7",
                    "fxstack.artifact_hash": expected_digest,
                    "fxstack.component_key": "swing_xgb",
                    "fxstack.pair": "GBPUSD",
                    "fxstack.model_family": "swing_xgb",
                    "fxstack.timeframe": "D",
                },
            ),
            bundle_run_id="bundle-7",
            trusted_pair="EURUSD",
            trusted_timeframe="D",
        )


def test_locked_load_rejects_valid_identity_replacement_before_deserialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_artifact(tmp_path / "fake_model")
    trusted_ref = _artifact_ref(path)
    resolved = threading.Event()
    load_calls: list[Path] = []
    result: list[tuple[Any | None, str]] = []

    class FakeModel:
        name = "fake_model"

        @classmethod
        def load(cls, artifact_path: Path) -> object:
            load_calls.append(artifact_path)
            return object()

    def _resolved_path(*_args: Any, **_kwargs: Any) -> Path:
        resolved.set()
        return path

    monkeypatch.setattr(runner_module, "resolve_model_artifact_path", _resolved_path)
    monkeypatch.setattr(
        runner_module,
        "get_settings",
        lambda: SimpleNamespace(model_load_timeout_secs=0.0),
    )

    worker = threading.Thread(
        target=lambda: result.append(
            runner_module._safe_load(FakeModel, trusted_ref, tmp_path)
        )
    )
    with artifact_lock(path):
        worker.start()
        assert resolved.wait(timeout=2.0)
        time.sleep(0.05)
        assert worker.is_alive()
        (path / "weights.bin").write_bytes(b"replacement")
        stamp_artifact_payload_digest(path)
    worker.join(timeout=5.0)

    assert not worker.is_alive()
    assert result == [(None, "load_error:ValueError")]
    assert load_calls == []


def test_newest_invalid_registry_fails_closed_and_publishers_are_atomic(
    tmp_path: Path,
) -> None:
    artifact_root = tmp_path / "artifacts"
    artifacts = {
        "regime": _artifact_ref(_write_artifact(artifact_root / "regime")),
        "meta": _artifact_ref(_write_artifact(artifact_root / "meta")),
        "swing_xgb": _artifact_ref(_write_artifact(artifact_root / "swing_xgb")),
        "intraday_xgb": _artifact_ref(
            _write_artifact(artifact_root / "intraday_xgb")
        ),
    }
    payload = {
        "run_id": "old-valid",
        "pair": "EURUSD",
        "tier": "tier2",
        "artifacts": artifacts,
        "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
        "feature_schema": feature_contract_metadata(),
    }
    registry = ArtifactRegistry(tmp_path / "registry")
    older = registry.register("EURUSD_old", payload)
    invalid = json.loads(json.dumps(payload))
    invalid["run_id"] = "new-invalid"
    invalid["artifacts"]["regime"].pop("artifact_hash")
    newest = registry.register("EURUSD_new", invalid)
    now = time.time()
    os.utime(older, (now - 10.0, now - 10.0))
    os.utime(newest, (now, now))

    active_manifest = tmp_path / "active_models.json"
    write_manifest(active_manifest, {"schema_version": 1, "active_model_sets": {}})
    assert json.loads(active_manifest.read_text(encoding="utf-8"))["schema_version"] == 1
    assert not list(registry.root.glob(".*.tmp"))
    assert not list(active_manifest.parent.glob(f".{active_manifest.name}.*.tmp"))

    with pytest.raises(ValueError, match="artifact_registry_hash_missing"):
        latest_registry_for_pair(registry_root=registry.root, pair="EURUSD")


def test_payload_tamper_is_rejected_before_runtime_deserialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_artifact(tmp_path / "runtime-model")
    (path / "weights.bin").write_bytes(b"tampered")
    load_calls: list[Path] = []

    class FakeModel:
        name = "fake_model"

        @classmethod
        def load(cls, artifact_path: Path) -> object:
            load_calls.append(artifact_path)
            return object()

    monkeypatch.setattr(
        runner_module,
        "get_settings",
        lambda: SimpleNamespace(model_load_timeout_secs=0.0),
    )

    loaded, error = runner_module._safe_load(FakeModel, str(path), tmp_path)

    assert loaded is None
    assert error == "load_error:ValueError"
    assert load_calls == []


def test_sequence_shadow_tamper_is_rejected_before_patchtst_deserialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_artifact(tmp_path / "shadow-patchtst")
    (path / "weights.bin").write_bytes(b"tampered")
    load_calls: list[Path] = []

    def _fake_load(_cls: type[Any], artifact_path: Path) -> object:
        load_calls.append(artifact_path)
        return object()

    monkeypatch.setattr(SwingPatchTST, "load", classmethod(_fake_load))
    monkeypatch.setattr(
        runner_module,
        "get_settings",
        lambda: SimpleNamespace(
            sequence_shadow_enabled=True,
            mlflow_enabled=True,
            model_load_timeout_secs=0.0,
        ),
    )
    monkeypatch.setattr(
        runner_module,
        "resolve_bundle_manifest_by_alias",
        lambda **_: SimpleNamespace(
            bundle_run_id="shadow-bundle",
            components={"swing_patchtst": {"path": str(path)}},
        ),
    )

    models, bundle_run_id, refs, errors = runner_module._load_sequence_shadow_bundle(
        pair="EURUSD",
        timeframes={"swing": "D"},
        project_root=tmp_path,
    )

    assert models == {}
    assert bundle_run_id == "shadow-bundle"
    assert refs == {}
    assert errors == ["swing_patchtst_load_error:ValueError"]
    assert load_calls == []


def test_nested_belief_payload_tamper_preflights_all_components_before_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_belief_v1_artifact(tmp_path / "belief")
    nested_payload = path / "horizon_short_xgb" / "model.bin"
    nested_payload.write_bytes(b"tampered")
    # Keep the root binding current while deliberately leaving the nested
    # component binding stale. This proves every child is checked up front.
    stamp_artifact_payload_digest(path)
    load_calls: list[Path] = []

    def _fake_load(_cls: type[Any], artifact_path: Path) -> object:
        load_calls.append(artifact_path)
        return object()

    monkeypatch.setattr(BeliefScenarioXGB, "load", classmethod(_fake_load))
    monkeypatch.setattr(BeliefHorizonXGB, "load", classmethod(_fake_load))

    with pytest.raises(
        ValueError,
        match="artifact_payload_digest_mismatch:directional_belief:horizon_short_xgb",
    ):
        load_directional_belief_model_set(path)
    assert load_calls == []


def test_unknown_belief_contract_is_rejected_before_load(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _write_belief_v1_artifact(tmp_path / "belief")
    meta_path = path / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["belief_contract"] = "directional_belief_v3"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    load_calls: list[Path] = []

    def _fake_load(_cls: type[Any], artifact_path: Path) -> object:
        load_calls.append(artifact_path)
        return object()

    monkeypatch.setattr(BeliefScenarioXGB, "load", classmethod(_fake_load))
    monkeypatch.setattr(BeliefHorizonXGB, "load", classmethod(_fake_load))

    with pytest.raises(ValueError, match="belief_contract_invalid:directional_belief"):
        load_directional_belief_model_set(path)
    assert load_calls == []


class _FakeXGBWeights:
    def save_model(self, path: str) -> None:
        Path(path).write_bytes(b"xgb-weights")


class _FakeStateModule:
    def state_dict(self) -> dict[str, object]:
        return {}


class _FakePretrainedModel:
    def save_pretrained(self, path: str) -> None:
        Path(path, "model.safetensors").write_bytes(b"patchtst-weights")


class _FakeConfig:
    def to_dict(self) -> dict[str, object]:
        return {"model_type": "test"}


def _bare_binary_xgb() -> tuple[XGBBinaryModel, str]:
    model = object.__new__(XGBBinaryModel)
    model.model = _FakeXGBWeights()
    model.params = {}
    model.runtime = {}
    model.use_calibration = False
    model.calibrator = None
    model.feature_columns = []
    return model, "calibrator.joblib"


def _bare_multiclass_xgb() -> tuple[XGBMulticlassModel, str]:
    model = object.__new__(XGBMulticlassModel)
    model.model = _FakeXGBWeights()
    model.params = {}
    model.runtime = {}
    model.use_calibration = False
    model.calibrators = {}
    model.classes_ = [0, 1]
    model.feature_columns = []
    return model, "calibrators.joblib"


def _bare_intraday_tcn() -> tuple[IntradayTCN, str]:
    model = object.__new__(IntradayTCN)
    model.backbone = _FakeStateModule()
    model.head = _FakeStateModule()
    model.backbone_kind = "test"
    model.params = SimpleNamespace(
        window_size=8,
        hidden_channels=4,
        lr=0.001,
        epochs=1,
        batch_size=2,
        require_cuda=False,
    )
    model.n_features = 1
    model.feature_columns = ["feature"]
    model.device = "cpu"
    model.calibrator = None
    return model, "calibrator.joblib"


def _bare_patchtst() -> tuple[SwingPatchTST, str]:
    model = object.__new__(SwingPatchTST)
    model.model = _FakePretrainedModel()
    model.params = SimpleNamespace(
        window_size=8,
        patch_length=4,
        stride=2,
        d_model=8,
        num_layers=1,
        num_heads=1,
        dropout=0.1,
        lr=0.001,
        epochs=1,
        batch_size=2,
        require_cuda=False,
    )
    model.n_features = 1
    model.feature_columns = ["feature"]
    model.device = "cpu"
    model.calibrator = None
    return model, "calibrator.joblib"


def _bare_swing_transformer() -> tuple[SwingTransformer, str]:
    model = object.__new__(SwingTransformer)
    model.model = _FakeStateModule()
    model.hf_config = _FakeConfig()
    model.params = SimpleNamespace(
        window_size=8,
        d_model=8,
        n_heads=1,
        n_layers=1,
        lr=0.001,
        epochs=1,
        batch_size=2,
        require_cuda=False,
    )
    model.n_features = 1
    model.feature_columns = ["feature"]
    model.device = "cpu"
    model.calibrator = None
    return model, "calibrator.joblib"


@pytest.mark.parametrize(
    "model_factory",
    [
        _bare_binary_xgb,
        _bare_multiclass_xgb,
        _bare_intraday_tcn,
        _bare_patchtst,
        _bare_swing_transformer,
    ],
)
def test_resave_removes_stale_optional_calibrator(
    tmp_path: Path,
    model_factory,
) -> None:
    model, stale_filename = model_factory()
    path = tmp_path / model_factory.__name__
    path.mkdir()
    stale_calibrator = path / stale_filename
    stale_calibrator.write_bytes(b"stale-calibrator")

    model.save(path)

    assert not stale_calibrator.exists()
    meta = validate_artifact_contract(path, label=model_factory.__name__)
    assert meta[ARTIFACT_PAYLOAD_DIGEST_KEY]
