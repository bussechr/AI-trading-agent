from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from fxstack.features.session_contract import current_feature_schema
from fxstack.rl.proposal import (
    _load_checkpoint,
    _load_checkpoint_cached,
    build_portfolio_rl_proposal_bundle,
)
from fxstack.rl.trainer import RLLinearCheckpoint


def _checkpoint(*, weight: float = 1.0, bias: float = 0.0) -> RLLinearCheckpoint:
    return RLLinearCheckpoint(
        target_name="reward",
        feature_names=["allocator_score"],
        feature_means=[0.0],
        feature_scales=[1.0],
        weights=[weight],
        bias=bias,
        train_rows=2,
        val_rows=1,
        metrics={"rl.train.mse": 0.01},
        metadata={"run_name": "integrity-test"},
    )


def test_checkpoint_rejects_tamper_and_invalid_runtime_shape(tmp_path: Path) -> None:
    from fxstack.mlops.model_uri import normalize_artifact_ref
    from fxstack.training.activation import _validate_portfolio_rl_checkpoint

    checkpoint_path = _checkpoint().save(tmp_path / "checkpoint.json")
    checkpoint_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    checkpoint_ref = normalize_artifact_ref(
        {
            "path": str(checkpoint_path),
            "content_sha256": checkpoint_sha256,
        }
    )
    assert checkpoint_ref["content_sha256"] == checkpoint_sha256
    assert _validate_portfolio_rl_checkpoint(
        path_value=checkpoint_ref,
        registry_label="integrity-test",
    )
    payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    payload["weights"][0] = 99.0
    checkpoint_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        RLLinearCheckpoint.load(checkpoint_path)
    with pytest.raises(ValueError, match="portfolio_rl_content_sha256_mismatch"):
        _validate_portfolio_rl_checkpoint(
            path_value=checkpoint_ref,
            registry_label="integrity-test",
        )

    invalid = _checkpoint()
    invalid.weights = []
    with pytest.raises(ValueError, match="vector shape mismatch"):
        invalid.predict_frame(pd.DataFrame([{"allocator_score": 0.5}]))


def test_same_path_checkpoint_replacement_reloads_new_content(tmp_path: Path) -> None:
    checkpoint_path = tmp_path / "checkpoint.json"
    _load_checkpoint_cached.cache_clear()
    _checkpoint(weight=1.0).save(checkpoint_path)
    activated_sha256 = hashlib.sha256(checkpoint_path.read_bytes()).hexdigest()
    first = _load_checkpoint(checkpoint_path)

    _checkpoint(weight=-2.0).save(checkpoint_path)
    second = _load_checkpoint(checkpoint_path)

    assert first is not None
    assert second is not None
    assert first.checksum != second.checksum
    assert first.weights == [1.0]
    assert second.weights == [-2.0]
    assert second is not first
    assert (
        _load_checkpoint(
            checkpoint_path,
            expected_content_sha256=activated_sha256,
        )
        is None
    )


def test_runtime_preflight_rejects_legacy_rl_before_scoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fxstack.runtime import service as service_module
    from fxstack.runtime.runner import _load_model_sets

    checkpoint_path = _checkpoint().save(tmp_path / "checkpoint.json")
    legacy = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    legacy["schema_version"] = "rl_linear_checkpoint_v1"
    legacy.pop("checksum")
    legacy.pop("checksum_contract")
    checkpoint_path.write_text(json.dumps(legacy), encoding="utf-8")
    scoring_called = False

    def _unexpected_score(
        self: RLLinearCheckpoint,
        frame: pd.DataFrame,
    ) -> object:
        nonlocal scoring_called
        scoring_called = True
        raise AssertionError("runtime preflight must reject before RL scoring")

    active_rows: dict[str, object] = {
        "EURUSD": {
            "model_set_id": "legacy-rl-active-row",
            "artifacts_json": {
                "portfolio_rl": {"path": str(checkpoint_path)},
            },
            "metadata_json": {
                "feature_schema": current_feature_schema(),
                "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
            },
        }
    }

    class FakeRuntimeService:
        def __init__(self, **_: object) -> None:
            pass

        def get_active_model_sets(
            self,
            *,
            enabled_only: bool = True,
        ) -> dict[str, object]:
            assert enabled_only is True
            return active_rows

    monkeypatch.setattr(service_module, "RuntimeService", FakeRuntimeService)
    monkeypatch.setattr(RLLinearCheckpoint, "predict_frame", _unexpected_score)

    loaded, diagnostics = _load_model_sets(
        pairs=["EURUSD"],
        require_all=False,
        project_root=tmp_path,
    )

    assert loaded == {}
    assert scoring_called is False
    assert diagnostics["failed_pairs"] == ["EURUSD"]
    assert diagnostics["pairs"]["EURUSD"]["failure_component"] == "portfolio_rl"
    assert "publisher identity is required" in diagnostics["pairs"]["EURUSD"][
        "failure_reason"
    ]

    first_path = _checkpoint(weight=1.0).save(tmp_path / "first.json")
    second_path = _checkpoint(weight=-1.0).save(tmp_path / "second.json")
    active_rows = {
        pair: {
            "model_set_id": f"{pair.lower()}-different-rl",
            "artifacts_json": {
                "portfolio_rl": {
                    "path": str(path),
                    "content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                },
            },
            "metadata_json": {
                "feature_schema": current_feature_schema(),
                "policies": {"swing": "xgb_only", "intraday": "xgb_only"},
            },
        }
        for pair, path in (("EURUSD", first_path), ("GBPUSD", second_path))
    }

    loaded, diagnostics = _load_model_sets(
        pairs=["EURUSD", "GBPUSD"],
        require_all=False,
        project_root=tmp_path,
    )

    assert loaded == {}
    assert diagnostics["failed_pairs"] == ["EURUSD", "GBPUSD"]
    assert all(
        "rl_checkpoint_identity_disagreement"
        in diagnostics["pairs"][pair]["failure_reason"]
        for pair in ("EURUSD", "GBPUSD")
    )


def test_rl_identity_failure_blocks_baseline_entry(tmp_path: Path) -> None:
    from fxstack.runtime.runner import (
        _finalize_entry_submissions,
        _resolve_runtime_rl_checkpoint,
    )

    class Settings:
        agent_mode = "paper"
        agent_paper_pair_allowlist = ["EURUSD"]
        agent_paper_sleeve_allowlist: list[str] = []
        agent_paper_intent_allowlist: list[str] = []
        adaptive_execution_enabled = False
        adaptive_shadow_enabled = True
        strategy_engine_mode = "rl_primary"
        rl_supervised_fallback_required = False
        min_order_lots = 0.01
        order_lot_step = 0.01
        max_order_lots = 0.0

    class DummyService:
        def __init__(self) -> None:
            self.payloads: list[dict[str, object]] = []

        def submit_command(
            self,
            payload: dict[str, object],
            proto: str = "v2",
        ) -> tuple[dict[str, object], None]:
            self.payloads.append(dict(payload))
            return {"status": "queued", "proto": proto}, None

    decisions = [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "execution_ready": True,
            "reasons": [],
            "metadata": {
                "pair": "EURUSD",
                "strict_entry_ready": True,
                "strict_entry_blocking_reasons": [],
                "strict_rejection_reason": "none",
                "entry_ready": True,
                "entry_blocking_reasons": [],
                "rejection_reason": "none",
                "lifecycle_action": "entry",
            },
        }
    ]
    service = DummyService()
    reason = "checkpoint_integrity_or_identity_invalid"
    missing_checkpoint = tmp_path / "removed-after-start.json"
    activated_sha256 = "a" * 64
    resolved_path, resolved_sha256 = _resolve_runtime_rl_checkpoint(
        model_sets={
            "EURUSD": SimpleNamespace(
                rl_checkpoint_path=str(missing_checkpoint),
                rl_checkpoint_content_sha256=activated_sha256,
            )
        },
        project_root=tmp_path,
    )
    assert resolved_path == missing_checkpoint.resolve(strict=False)
    assert resolved_sha256 == activated_sha256
    proposal_bundle = build_portfolio_rl_proposal_bundle(
        ts="2026-03-25T10:00:00Z",
        decisions=decisions,
        checkpoint_path=resolved_path,
        checkpoint_content_sha256=resolved_sha256,
    ).to_dict()
    assert proposal_bundle["source"] == "checkpoint_identity_failure"
    assert proposal_bundle["checkpoint_identity_failure"] is True

    diagnostics = _finalize_entry_submissions(
        decisions=decisions,
        pending_entries=[
            {
                "index": 0,
                "pair": "EURUSD",
                "ts_value": "2026-03-25T10:00:00Z",
                "action_key": "entry:2026-03-25T10:00:00Z",
                "payload": {
                    "command_id": "would-have-entered",
                    "action": "entry",
                    "symbol": "EURUSD",
                    "lots": 0.5,
                },
                "approved_order": {
                    "command_id": "would-have-entered",
                    "action": "entry",
                    "symbol": "EURUSD",
                    "cmd": "BUY",
                    "side": "BUY",
                    "lots": 0.5,
                },
                "orchestration": {},
            }
        ],
        svc=service,
        last_action_key={},
        settings=Settings(),
        rl_portfolio_proposal=proposal_bundle,
    )

    assert service.payloads == []
    assert diagnostics["rl_blocked_entry_count"] == 1
    assert diagnostics["rl_fallback_entry_count"] == 0
    assert decisions[0]["execution_ready"] is False
    assert decisions[0]["reasons"] == [reason]
    assert decisions[0]["metadata"]["rl_router_reason"] == reason
    assert decisions[0]["metadata"]["rl_checkpoint_identity_failure"] is True
