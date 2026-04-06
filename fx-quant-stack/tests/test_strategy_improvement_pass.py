from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from fxstack.settings import get_settings
from fxstack.tasks import artifact_retrain_decision
from fxstack.training.activation import parse_registry_entry


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _write_meta(path: Path, **extra: object) -> None:
    path.mkdir(parents=True, exist_ok=True)
    payload = {
        "trained_at": 1_700_000_000.0,
        "data_window_end": "2026-03-20T00:00:00+00:00",
    }
    payload.update(extra)
    (path / "meta.json").write_text(json.dumps(payload), encoding="utf-8")


def test_tier1_activation_requires_lifecycle_when_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FXSTACK_STRICT_ACTIVATION", "1")
    monkeypatch.setenv("FXSTACK_REQUIRE_LIFECYCLE_ARTIFACTS", "1")
    monkeypatch.setenv("FXSTACK_TIER1_PAIRS", "EURUSD")

    base = tmp_path / "artifacts"
    for rel in [
        "eurusd/regime_hmm",
        "eurusd/meta_filter",
        "eurusd/swing_transformer",
        "eurusd/swing_xgb",
        "eurusd/intraday_tcn",
        "eurusd/intraday_xgb",
    ]:
        _write_meta(base / rel)

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "run_id": "run1",
                "pair": "EURUSD",
                "tier": "tier1",
                "artifacts": {
                    "regime": {"path": str(base / "eurusd/regime_hmm")},
                    "meta": {"path": str(base / "eurusd/meta_filter")},
                    "swing_transformer": {"path": str(base / "eurusd/swing_transformer")},
                    "swing_xgb": {"path": str(base / "eurusd/swing_xgb")},
                    "intraday_tcn": {"path": str(base / "eurusd/intraday_tcn")},
                    "intraday_xgb": {"path": str(base / "eurusd/intraday_xgb")},
                },
                "policies": {
                    "swing": "transformer_primary_xgb_fallback",
                    "intraday": "tcn_primary_xgb_fallback",
                },
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
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

    base = tmp_path / "artifacts"
    for rel in [
        "usdcad/regime_hmm",
        "usdcad/meta_filter",
        "usdcad/swing_xgb",
        "usdcad/intraday_xgb",
    ]:
        _write_meta(base / rel)

    registry = tmp_path / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "run_id": "run2",
                "pair": "USDCAD",
                "artifacts": {
                    "regime": {"path": str(base / "usdcad/regime_hmm")},
                    "meta": {"path": str(base / "usdcad/meta_filter")},
                    "swing_xgb": {"path": str(base / "usdcad/swing_xgb")},
                    "intraday_xgb": {"path": str(base / "usdcad/intraday_xgb")},
                },
                "policies": {
                    "swing": "xgb_only",
                    "intraday": "xgb_only",
                },
                "feature_schema": {"intraday_contract": "hierarchical_v1"},
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
