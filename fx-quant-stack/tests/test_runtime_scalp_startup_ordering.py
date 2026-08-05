from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime import scalp_live_loop as loop
from fxstack.runtime import startup as startup_module


class _ContractUniverse:
    account_currency = "GBP"

    @staticmethod
    def contract_for(_symbol: str) -> None:
        return None


class _GovernanceService:
    @staticmethod
    def get_metrics() -> dict[str, Any]:
        return {"feature_parity": {"breaches": 0}}


def _governance_settings() -> SimpleNamespace:
    return SimpleNamespace(
        capital_governance_enabled=True,
        capital_band_mode="full_risk_live",
        capital_entries_only=False,
        provider_shadow_only=False,
        max_total_positions=4,
        max_pair_positions=1,
        portfolio_corr_mode="heuristic",
        portfolio_realized_corr_window_bars=0,
        portfolio_realized_corr_min_obs=0,
    )


def test_scalp_binding_governance_bootstraps_then_admits_fresh_predecessor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        loop,
        "evaluate_portfolio_allocation",
        lambda **_kwargs: SimpleNamespace(
            telemetry={"numeric_inputs_valid": True}
        ),
    )
    state: dict[str, Any] = {
        "positions": [],
        "runtime_diag": {
            "loop_latency_ms": 1.0,
            "risk_cycle_summary": {},
        },
        "equity": 10_000.0,
    }
    common = {
        "service": _GovernanceService(),
        "settings": _governance_settings(),
        "contract_universe": _ContractUniverse(),
        "quote_rates": {},
        "ready": {"ready": True},
        "max_source_age_secs": 60.0,
    }

    bootstrap = loop._binding_governance_policy(
        state=state,
        computed_at=100.0,
        **common,
    )

    assert bootstrap["paused"] is True
    assert bootstrap["entries_only"] is True
    assert bootstrap["budget_scale"] == 0.0
    assert "governance_bootstrap" in bootstrap["reasons"]

    state["governance"] = deepcopy(bootstrap)
    state["runtime_last_cycle_ts"] = 100.0
    admitted = loop._binding_governance_policy(
        state=state,
        computed_at=101.0,
        **common,
    )

    assert admitted["source_cycle_ts"] == pytest.approx(100.0)
    assert admitted["source_age_secs"] == pytest.approx(1.0)
    assert admitted["paused"] is False
    assert admitted["entries_only"] is False
    assert admitted["shadow_only"] is False
    assert admitted["budget_scale"] == pytest.approx(1.0)
    assert "governance_bootstrap" not in admitted["reasons"]


class _Clock:
    def __init__(self, start: float = 1_900_000_000.0) -> None:
        self.current = float(start)
        self.values: list[float] = []

    def time(self) -> float:
        self.current += 1.0
        self.values.append(self.current)
        return self.current


class _StartupService:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {
            "runtime_diag": {
                "orchestration_live": {"authority_revision": 0}
            }
        }
        self.patch_events: list[tuple[dict[str, Any], bool]] = []
        self.failure_events: list[dict[str, Any]] = []
        self.activation_succeeded = False

    def record_runtime_boot_state(
        self,
        *,
        boot: dict[str, Any],
        patch: dict[str, Any],
        prune_state: bool,
        preserve_queued_exposure_reducing: bool,
    ) -> None:
        del boot, prune_state, preserve_queued_exposure_reducing
        self.state.update(deepcopy(patch))

    @staticmethod
    def purge_pending_commands(**_kwargs: Any) -> int:
        return 0

    @staticmethod
    def quarantine_stale_delivered(**_kwargs: Any) -> int:
        return 0

    def get_state(self) -> dict[str, Any]:
        return deepcopy(self.state)

    def patch_state(self, patch: dict[str, Any]) -> None:
        self.patch_events.append(
            (deepcopy(patch), bool(self.activation_succeeded))
        )
        self.state.update(deepcopy(patch))

    def record_runtime_boot_failure(
        self,
        *,
        boot: dict[str, Any],
        failure_reason: str,
        patch: dict[str, Any],
        prune_state: bool,
        preserve_queued_exposure_reducing: bool,
    ) -> None:
        self.failure_events.append(
            {
                "boot": deepcopy(boot),
                "failure_reason": failure_reason,
                "patch": deepcopy(patch),
                "prune_state": prune_state,
                "preserve_queued_exposure_reducing": (
                    preserve_queued_exposure_reducing
                ),
            }
        )
        self.state.update(deepcopy(patch))


def _startup_settings() -> SimpleNamespace:
    return SimpleNamespace(
        entry_strategy_family="mtvclc",
        pairs=list(IG_MT4_SCALP_SYMBOLS),
        policy_version="scalp-dislocation-test",
        startup_requeue_age_secs=30.0,
    )


def _install_startup_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    service: _StartupService,
    clock: _Clock,
    activation_active: bool,
    admission_valid: bool = True,
    protective_active: bool = True,
) -> None:
    admission = SimpleNamespace(
        valid=admission_valid,
        reason="" if admission_valid else "validation_revoked",
        to_dict=lambda: {"valid": admission_valid},
    )
    monkeypatch.setattr(startup_module, "perform_startup_bridge_checks", lambda _settings: None)
    monkeypatch.setattr(loop, "_runtime_service", lambda _settings: service)
    monkeypatch.setattr(loop.uuid, "uuid4", lambda: "scalp-test-boot")
    monkeypatch.setattr(loop.time, "time", clock.time)
    monkeypatch.setattr(loop, "runtime_config_sha256", lambda _settings: "a" * 64)
    monkeypatch.setattr(
        loop,
        "verify_configured_scalp_runtime_admission",
        lambda *_args, **_kwargs: admission,
    )
    monkeypatch.setattr(
        loop,
        "build_scalp_runtime_attestation",
        lambda **_kwargs: {"attested": True},
    )
    monkeypatch.setattr(
        loop,
        "scalp_live_command_admission",
        lambda **_kwargs: {"admitted": True},
    )
    monkeypatch.setattr(loop, "_is_live", lambda _settings: True)
    monkeypatch.setattr(loop, "_startup_log", lambda _message: None)

    def _activate(**_kwargs: Any) -> SimpleNamespace:
        activation_patch, was_active = service.patch_events[-1]
        assert activation_patch["runtime_status"] == "running"
        assert activation_patch["runtime_last_cycle_ts"] == 0.0
        assert was_active is False
        if activation_active:
            service.activation_succeeded = True
            return SimpleNamespace(active=True, reason="active")
        return SimpleNamespace(active=False, reason="forced_test_failure")

    monkeypatch.setattr(loop, "ensure_production_scalp_authority", _activate)

    def _activate_protective(**_kwargs: Any) -> SimpleNamespace:
        activation_patch, was_active = service.patch_events[-1]
        assert activation_patch["runtime_status"] == "running"
        assert activation_patch["runtime_last_cycle_ts"] == 0.0
        assert was_active is False
        if protective_active:
            service.activation_succeeded = True
            return SimpleNamespace(active=True, reason="protective_management_only")
        return SimpleNamespace(active=False, reason="protective_test_failure")

    monkeypatch.setattr(
        loop,
        "ensure_production_scalp_protective_management_egress",
        _activate_protective,
    )


def test_live_scalp_startup_publishes_fresh_timestamp_only_after_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StartupService()
    clock = _Clock()
    _install_startup_fakes(
        monkeypatch,
        service=service,
        clock=clock,
        activation_active=True,
    )

    loop.run_production_scalp_loop(
        settings=_startup_settings(),
        startup_preflight={"settings_validated": True},
        equity=10_000.0,
        sleep_secs=10,
        feature_root="unused",
        max_cycles=0,
    )

    assert [event[0]["runtime_last_cycle_ts"] for event in service.patch_events] == [
        0.0,
        clock.values[-1],
    ]
    assert service.patch_events[0][1] is False
    assert service.patch_events[1][1] is True
    assert service.patch_events[1][0]["runtime_last_cycle_ts"] > 0.0
    assert service.patch_events[1][0]["runtime_startup"]["phase"] == "main_loop"
    assert service.failure_events == []


def test_live_scalp_activation_failure_records_failed_zero_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StartupService()
    clock = _Clock()
    _install_startup_fakes(
        monkeypatch,
        service=service,
        clock=clock,
        activation_active=False,
    )

    with pytest.raises(
        RuntimeError,
        match="production_scalp_authority_startup_failed:forced_test_failure",
    ):
        loop.run_production_scalp_loop(
            settings=_startup_settings(),
            startup_preflight={"settings_validated": True},
            equity=10_000.0,
            sleep_secs=10,
            feature_root="unused",
            max_cycles=0,
        )

    assert [event[0]["runtime_last_cycle_ts"] for event in service.patch_events] == [
        0.0
    ]
    assert len(service.failure_events) == 1
    failure = service.failure_events[0]
    assert failure["boot"]["phase"] == "authority_activation"
    assert failure["failure_reason"].endswith(
        "authority_activation:RuntimeError"
    )
    assert failure["patch"]["runtime_status"] == "failed"
    assert failure["patch"]["runtime_last_cycle_ts"] == 0.0
    assert service.state["runtime_status"] == "failed"
    assert service.state["runtime_last_cycle_ts"] == 0.0


def test_invalid_entry_evidence_starts_ready_in_protective_management_only_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _StartupService()
    clock = _Clock()
    _install_startup_fakes(
        monkeypatch,
        service=service,
        clock=clock,
        activation_active=False,
        admission_valid=False,
        protective_active=True,
    )

    loop.run_production_scalp_loop(
        settings=_startup_settings(),
        startup_preflight={"settings_validated": True},
        equity=10_000.0,
        sleep_secs=10,
        feature_root="unused",
        max_cycles=0,
    )

    assert [event[0]["runtime_last_cycle_ts"] for event in service.patch_events] == [
        0.0,
        clock.values[-1],
    ]
    ready_diag = service.patch_events[-1][0]["runtime_diag"]
    assert ready_diag["production_scalp_startup"][
        "protective_management_only"
    ] is True
    assert ready_diag["production_scalp_startup"]["authority_activation"][
        "reason"
    ] == "protective_management_only"
    assert service.failure_events == []
