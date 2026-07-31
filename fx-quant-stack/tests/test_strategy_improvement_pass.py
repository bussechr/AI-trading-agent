from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fxstack.features.session_contract import current_feature_schema, feature_contract_metadata
from fxstack.models.artifact_contract import (
    ARTIFACT_PAYLOAD_DIGEST_KEY,
    stamp_artifact_payload_digest,
)
from fxstack.settings import get_settings
from fxstack.tasks import artifact_retrain_decision
from fxstack.training.activation import parse_registry_entry


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_meta(path: Path, **extra: object) -> str:
    """Write a contract-valid artifact and return its stamped payload digest.

    Registry entries must carry that digest as ``artifact_hash`` -- resolution
    fails closed on unregistered local artifacts.
    """
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        **feature_contract_metadata(),
        "trained_at": 1_700_000_000.0,
        "data_window_end": "2026-03-20T00:00:00+00:00",
    }
    payload.update(extra)
    (path / "model.bin").write_bytes(b"test-model")
    (path / "meta.json").write_text(json.dumps(payload), encoding="utf-8")
    meta = stamp_artifact_payload_digest(path)
    return str(meta[ARTIFACT_PAYLOAD_DIGEST_KEY])


def _write_stale_contract_meta(path: Path, *, contract_key: str, stale_value: str, **extra: object) -> None:
    """Stamp a valid artifact, then downgrade one contract field on disk.

    The stamp itself validates the contract, so a superseded version has to be
    written *after* stamping to simulate a model trained on an old contract.
    """
    _write_meta(path, **extra)
    payload = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    payload[contract_key] = stale_value
    (path / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def test_tier1_activation_requires_lifecycle_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_STRICT_ACTIVATION", "1")
    monkeypatch.setenv("FXSTACK_REQUIRE_LIFECYCLE_ARTIFACTS", "1")
    monkeypatch.setenv("FXSTACK_TIER1_PAIRS", "EURUSD")
    # This test pins the lifecycle-artifact gate; certificate coverage is
    # pinned separately by tests/test_certificate_coverage.py.
    monkeypatch.setenv("FXSTACK_REQUIRE_VALIDATION_CERTIFICATE", "0")

    base = tmp_path / "artifacts"
    digests = {
        rel: _write_meta(base / rel)
        for rel in [
            "eurusd/regime_hmm",
            "eurusd/meta_filter",
            "eurusd/swing_transformer",
            "eurusd/swing_xgb",
            "eurusd/intraday_tcn",
            "eurusd/intraday_xgb",
        ]
    }

    def _ref(rel: str) -> dict[str, str]:
        return {"path": str(base / rel), "artifact_hash": digests[rel]}

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "run_id": "run1",
                "pair": "EURUSD",
                "tier": "tier1",
                "artifacts": {
                    "regime": _ref("eurusd/regime_hmm"),
                    "meta": _ref("eurusd/meta_filter"),
                    "swing_transformer": _ref("eurusd/swing_transformer"),
                    "swing_xgb": _ref("eurusd/swing_xgb"),
                    "intraday_tcn": _ref("eurusd/intraday_tcn"),
                    "intraday_xgb": _ref("eurusd/intraday_xgb"),
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": current_feature_schema(),
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="missing required lifecycle artifacts"):
        parse_registry_entry(registry)


def test_tier2_activation_records_soft_lifecycle_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_STRICT_ACTIVATION", "1")
    monkeypatch.setenv("FXSTACK_REQUIRE_LIFECYCLE_ARTIFACTS", "1")
    monkeypatch.setenv("FXSTACK_TIER1_PAIRS", "EURUSD,GBPUSD")
    monkeypatch.setenv("FXSTACK_REQUIRE_VALIDATION_CERTIFICATE", "0")

    base = tmp_path / "artifacts"
    digests = {
        rel: _write_meta(base / rel)
        for rel in [
            "usdcad/regime_hmm",
            "usdcad/meta_filter",
            "usdcad/swing_xgb",
            "usdcad/intraday_xgb",
        ]
    }

    def _ref(rel: str) -> dict[str, str]:
        return {"path": str(base / rel), "artifact_hash": digests[rel]}

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "run_id": "run2",
                "pair": "USDCAD",
                "artifacts": {
                    "regime": _ref("usdcad/regime_hmm"),
                    "meta": _ref("usdcad/meta_filter"),
                    "swing_xgb": _ref("usdcad/swing_xgb"),
                    "intraday_xgb": _ref("usdcad/intraday_xgb"),
                },
                "policies": {
                    "swing": "xgb_only",
                    "intraday": "xgb_only",
                },
                "feature_schema": current_feature_schema(),
            }
        ),
        encoding="utf-8",
    )

    item = parse_registry_entry(registry)
    assert str(item["tier"]) == "tier2"
    assert bool(item["metadata"]["lifecycle_complete"]) is False
    assert "exit_policy_missing" in list(item["metadata"]["activation_warnings"])


def test_artifact_retrain_decision_respects_new_rows_threshold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_FORCE_WEEKLY_RETRAIN_DAY", "")
    get_settings.cache_clear()
    artifact = tmp_path / "intraday_xgb"
    _write_meta(artifact, data_window_end="2026-03-20T00:00:00+00:00")
    dataset = pd.DataFrame(
        {
            "ts": pd.to_datetime(
                [
                    "2026-03-19T00:00:00Z",
                    "2026-03-20T00:00:00Z",
                    "2026-03-20T00:05:00Z",
                    "2026-03-20T00:10:00Z",
                ],
                utc=True,
            )
        }
    )

    decision = artifact_retrain_decision(
        dataset=dataset,
        artifact_path=artifact,
        min_new_rows=3,
    )

    assert bool(decision["should_retrain"]) is False
    assert int(decision["new_rows"]) == 2
    assert str(decision["reason"]) == "up_to_date"


def test_artifact_retrain_decision_invalidates_old_feature_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FXSTACK_FORCE_WEEKLY_RETRAIN_DAY", "")
    get_settings.cache_clear()
    artifact = tmp_path / "intraday_xgb"
    _write_stale_contract_meta(
        artifact,
        contract_key="session_contract_version",
        stale_value="utc_session_buckets_v1",
        data_window_end="2026-03-20T00:00:00+00:00",
    )
    dataset = pd.DataFrame(
        {"ts": pd.to_datetime(["2026-03-20T00:00:00Z"], utc=True)}
    )

    decision = artifact_retrain_decision(
        dataset=dataset,
        artifact_path=artifact,
        min_new_rows=999,
        weekly_only=True,
    )

    assert decision["should_retrain"] is True
    assert decision["reason"] == "feature_contract_mismatch"
    assert "session_contract_version" in decision["feature_contract_mismatches"]
