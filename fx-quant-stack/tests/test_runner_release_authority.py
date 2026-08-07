from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.runtime import runner


def _request() -> dict[str, Any]:
    return {
        "generation_id": "generation-1",
        "request_sha256": "a" * 64,
        "pair": "EURUSD",
        "source_sha256": "b" * 64,
        "package_merkle_sha256": "1" * 64,
        "config_sha256": "c" * 64,
        "manifest_file_sha256": "d" * 64,
        "model_identity_sha256": "e" * 64,
        "artifact_set_sha256": "f" * 64,
        "model_set_id": "model-1",
        "authorized_execution": {
            "pair_scope": ["EURUSD"],
        },
    }


def _attestation() -> dict[str, Any]:
    request = _request()
    return {
        "runtime_boot_id": "boot-1",
        "source_sha256": request["source_sha256"],
        "package_merkle_sha256": request["package_merkle_sha256"],
        "config_sha256": request["config_sha256"],
        "source_clean": True,
        "valid": True,
        "errors": [],
        "pairs": {
            "EURUSD": {
                key: request[key]
                for key in (
                    "manifest_file_sha256",
                    "model_identity_sha256",
                    "artifact_set_sha256",
                    "model_set_id",
                )
            }
        },
    }


class _Service:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = deepcopy(state)
        self.cas_calls: list[dict[str, Any]] = []
        self.disabled: list[str] = []

    def get_active_model_set(self, pair: str) -> dict[str, Any]:
        return {"pair": pair, "model_set_id": "model-1"}

    def compare_and_set_release_authority(self, **kwargs: Any) -> dict[str, Any]:
        self.cas_calls.append(deepcopy(kwargs))
        authority = deepcopy(kwargs["next_authority"])
        self.state["release_authority"] = authority
        self.state["execution_egress_enabled"] = False
        return {"updated": True, "authority": authority}

    def get_state(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def disable_execution_egress(
        self,
        *,
        reason: str,
        revoke_release: bool,
    ) -> dict[str, Any]:
        self.disabled.append(reason)
        self.state["execution_egress_enabled"] = False
        self.state["release_authority"]["status"] = "revoked"
        return {"execution_egress_enabled": False}


def test_runner_ack_binds_pending_request_to_loaded_boot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    state = {
        "execution_egress_enabled": False,
        "release_authority": {
            "schema_version": "fxstack_live_release_authority_state_v1",
            "status": "pending",
            "request": request,
            "ack": {},
        },
        "runtime_diag": {"orchestration_live": {}},
    }
    service = _Service(state)
    loaded = SimpleNamespace(model_set_id="model-1", rollout_policy={})
    monkeypatch.setattr(runner, "authority_request_errors", lambda *args, **kwargs: [])

    result = runner._synchronize_release_authority(
        svc=service,
        state=state,
        runtime_boot_id="boot-1",
        runtime_attestation=_attestation(),
        model_sets={"EURUSD": loaded},
    )

    assert result["status"] == "acknowledged"
    assert result["valid"] is False
    assert len(service.cas_calls) == 1
    acknowledged = service.cas_calls[0]["next_authority"]
    assert acknowledged["status"] == "acknowledged"
    assert acknowledged["ack"] == {
        "schema_version": "fxstack_live_release_authority_ack_v1",
        "generation_id": request["generation_id"],
        "request_sha256": request["request_sha256"],
        "runtime_boot_id": "boot-1",
        "runtime_pid": acknowledged["ack"]["runtime_pid"],
            "source_sha256": request["source_sha256"],
            "package_merkle_sha256": request["package_merkle_sha256"],
            "config_sha256": request["config_sha256"],
        "manifest_file_sha256": request["manifest_file_sha256"],
        "model_identity_sha256": request["model_identity_sha256"],
        "artifact_set_sha256": request["artifact_set_sha256"],
        "model_set_id": request["model_set_id"],
        "acked_at": acknowledged["ack"]["acked_at"],
    }
    assert loaded.rollout_policy["active"] is False


def test_runner_revokes_active_authority_on_any_revalidation_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = _request()
    state = {
        "execution_egress_enabled": True,
        "release_authority": {
            "schema_version": "fxstack_live_release_authority_state_v1",
            "status": "active",
            "request": request,
            "ack": {"runtime_boot_id": "boot-1"},
        },
        "runtime_diag": {"orchestration_live": {}},
    }
    service = _Service(state)
    loaded = SimpleNamespace(model_set_id="model-1", rollout_policy={})
    monkeypatch.setattr(
        runner,
        "active_authority_errors",
        lambda *args, **kwargs: ["release_authority_manifest_file_sha256_mismatch"],
    )

    result = runner._synchronize_release_authority(
        svc=service,
        state=state,
        runtime_boot_id="boot-1",
        runtime_attestation=_attestation(),
        model_sets={"EURUSD": loaded},
    )

    assert result["status"] == "revoked"
    assert result["valid"] is False
    assert service.disabled == [
        "release_authority_drift:release_authority_manifest_file_sha256_mismatch"
    ]
    assert loaded.rollout_policy["active"] is False


def test_runner_stamps_evaluated_sleeve_and_exact_release_generation() -> None:
    request = _request()
    release = {
        "status": "active",
        "request": request,
        "ack": {"runtime_boot_id": "boot-1"},
    }
    payload = runner._stamp_orchestration_payload(
        payload={"cmd": "BUY", "symbol": "EURUSD", "lots": 0.01},
        orchestration={
            "enabled": True,
            "agent_mode": "live",
            "cycle_id": "cycle-1",
            "run_id": "run-1",
            "trace_id": "trace-1",
            "thread_id": "thread-1",
            "correlation_id": "correlation-1",
            "pair": "EURUSD",
        },
        release_authority=release,
        sleeve="trend",
    )

    meta = payload["orchestration_meta_json"]
    assert meta["adaptive_sleeve"] == "trend"
    assert meta["release_generation_id"] == request["generation_id"]
    assert meta["release_request_sha256"] == request["request_sha256"]
    assert meta["release_model_identity_sha256"] == request[
        "model_identity_sha256"
    ]
    assert meta["release_manifest_file_sha256"] == request[
        "manifest_file_sha256"
    ]
    assert meta["release_runtime_boot_id"] == "boot-1"


def _production_settings() -> SimpleNamespace:
    return SimpleNamespace(
        start_profile="live",
        agent_mode="live",
        live_armed=True,
        pairs=["EURUSD"],
        agent_live_pair_allowlist=["EURUSD"],
        agent_live_sleeve_allowlist=["trend_pullback"],
        agent_live_intent_allowlist=["enter", "exit", "reduce"],
        enable_lifecycle_actions=True,
        enable_adjust_actions=False,
        capital_rollout_budget_scale_full_risk=1.0,
        max_pair_positions=1,
        max_total_positions=6,
        risk_max_gross_exposure=0.3,
        risk_max_net_exposure=0.2,
    )


class _ProductionService:
    def __init__(self) -> None:
        self.live_updates: dict[str, Any] = {}
        self.egress_boot_id = ""

    def patch_orchestration_live_state(self, **kwargs: Any) -> dict[str, Any]:
        self.live_updates = deepcopy(kwargs["updates"])
        return {**self.live_updates, "authority_revision": 7}

    def enable_production_execution_egress(
        self,
        *,
        runtime_boot_id: str,
    ) -> dict[str, Any]:
        self.egress_boot_id = runtime_boot_id
        return {
            "enabled": True,
            "source": "production_runtime",
            "runtime_boot_id": runtime_boot_id,
        }


def test_production_operator_scope_owns_rollout_without_twin_release() -> None:
    settings = _production_settings()
    loaded = SimpleNamespace(model_set_id="model-1", rollout_policy={})

    runner._apply_production_operator_rollout(
        settings=settings,
        model_sets={"EURUSD": loaded},
    )

    assert loaded.rollout_policy["source"] == "production_operator_scope"
    assert loaded.rollout_policy["mode"] == "live"
    assert loaded.rollout_policy["active"] is True
    assert loaded.rollout_policy["pair_allowlisted"] is True
    assert runner._live_command_admission_diagnostics(
        settings=settings,
        model_sets={"EURUSD": loaded},
    )["allowed"] is True


def test_production_runtime_arms_when_twin_release_is_rejected() -> None:
    settings = _production_settings()
    loaded = SimpleNamespace(model_set_id="model-1", rollout_policy={})
    runner._apply_production_operator_rollout(
        settings=settings,
        model_sets={"EURUSD": loaded},
    )
    service = _ProductionService()

    result = runner._arm_production_runtime_authority(
        svc=service,
        state={
            "release_authority": {
                "status": "rejected",
                "errors": ["twin_evidence_unavailable"],
            },
            "runtime_diag": {"orchestration_live": {"authority_revision": 6}},
        },
        settings=settings,
        model_sets={"EURUSD": loaded},
        runtime_boot_id="boot-production-1",
    )

    assert result["status"] == "active"
    assert result["binding"] == "production_runtime"
    assert result["errors"] == []
    assert service.egress_boot_id == "boot-production-1"
    assert service.live_updates["release_status"] == "advisory_only"
