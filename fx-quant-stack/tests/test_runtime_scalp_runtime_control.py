from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS, IG_MT4_VENUE_ID
from fxstack.runtime.scalp_engine_identity import ProductionScalpEngineIdentity
from fxstack.runtime.scalp_execution_authority import (
    BROKER_NATIVE_BRACKET_POLICY,
    SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
    ScalpAuthorityExpectation,
    authority_binding_sha256,
    build_active_authority,
)
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_runtime_admission import ScalpRuntimeAdmission
from fxstack.runtime.scalp_runtime_control import (
    build_scalp_runtime_attestation,
    ensure_production_scalp_authority,
    ensure_production_scalp_protective_management_egress,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
)
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


NOW = 1_800_000_000.0


def _admission(
    *,
    valid: bool = True,
    direct_demo: bool = False,
) -> ScalpRuntimeAdmission:
    reason = "" if valid else "validation_certificate_revoked"
    generation_id = "direct-demo-v1" if direct_demo else "scalp-generation-1"
    verification = MTVCLCRuntimeReleaseVerification(
        valid=valid,
        reason=reason,
        errors=() if valid else (reason,),
        authenticated=bool(valid and not direct_demo),
        revocation_verified=bool(valid and not direct_demo),
        certificate_sha256="a" * 64 if valid else "",
        runtime_release_certificate_sha256="a" * 64 if valid else "",
        evidence_sha256=("a" if direct_demo else "b") * 64 if valid else "",
        signing_key_id="c" * 64 if valid and not direct_demo else "",
        runtime_release_signing_key_id=(
            "c" * 64 if valid and not direct_demo else ""
        ),
        evidence_signing_key_id="f" * 64 if valid and not direct_demo else "",
        registry_generation_id=generation_id if valid else "",
        registry_revision=1 if valid else 0,
        registry_sha256="1" * 64 if valid and not direct_demo else "",
        generation_id=generation_id,
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        engine_sha256="d" * 64,
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=MTVCLC_CONFIG_SHA256,
        venue_id=IG_MT4_VENUE_ID if valid else "",
        account_mode="demo" if valid else "",
        scope_version=(
            "fxstack.ig_mt4.scalp_scope.v3" if valid else ""
        ),
        symbol_scope=IG_MT4_SCALP_SYMBOLS if valid else (),
        max_entries_per_symbol_utc_day=1 if valid else 0,
        issued_at_epoch=NOW - 60.0,
        expires_at_epoch=NOW + 86_400.0,
        qualification_surface_sha256=(
            "2" * 64 if valid and not direct_demo else ""
        ),
        cost_mapping_sha256="3" * 64 if valid and not direct_demo else "",
        execution_contract_sha256=(
            "4" * 64 if valid and not direct_demo else ""
        ),
        win_probability_lower_bounds=(
            {
                symbol: {"BUY": 0.60, "SELL": 0.60}
                for symbol in IG_MT4_SCALP_SYMBOLS
            }
            if valid and not direct_demo
            else {}
        ),
        admission_mode=(
            SCALP_ADMISSION_MODE_DIRECT_DEMO
            if direct_demo
            else "signed_validation"
        ),
    )
    return ScalpRuntimeAdmission(
        valid=valid,
        reason=reason,
        errors=() if valid else (reason,),
        verification=verification,
        engine_identity=ProductionScalpEngineIdentity(
            engine_sha256="d" * 64,
            component_sha256=(("strategy/scalp_dislocation.py", "f" * 64),),
        ),
    )


def _settings() -> SimpleNamespace:
    return SimpleNamespace(
        agent_mode="live",
        live_armed=True,
        entry_strategy_family="scalp_dislocation",
        pairs=list(IG_MT4_SCALP_SYMBOLS),
        agent_live_pair_allowlist=list(IG_MT4_SCALP_SYMBOLS),
        agent_live_sleeve_allowlist=["scalp"],
        agent_live_intent_allowlist=["enter", "exit", "reduce", "tighten_stop"],
        live_expected_account_mode="demo",
        capital_rollout_budget_scale_full_risk=1.0,
        enable_lifecycle_actions=True,
        enable_adjust_actions=True,
    )


class _Service:
    def __init__(self) -> None:
        self.state = {
            "runtime_status": "running",
            "runtime_startup": {"boot_id": "boot-1"},
            "runtime_attestation": {"runtime_boot_id": "boot-1"},
            "runtime_diag": {"orchestration_live": {}},
            "execution_egress_enabled": False,
            "production_scalp_authority": {},
            "broker_venue_id": IG_MT4_VENUE_ID,
            "broker_account_mode": "demo",
            "broker_account_scope": "ig-demo-scope",
            "broker_account_magic": 246_810,
        }
        self.activation_verification = None
        self.egress_disable_called = False
        self.preserve_queued_exposure_reducing = False

    def get_state(self):
        return deepcopy(self.state)

    def patch_state(self, patch):
        incoming = dict(patch)
        incoming.pop("__expected_orchestration_live_authority__", None)
        self.state.update(incoming)

    def patch_orchestration_live_state(
        self,
        *,
        updates,
        expected_live_authority,
        allow_reenable,
    ):
        del allow_reenable
        assert expected_live_authority == self.state["runtime_diag"].get(
            "orchestration_live", {}
        )
        live = {**dict(updates), "authority_revision": 1}
        self.state["runtime_diag"] = {
            **self.state["runtime_diag"],
            "orchestration_live": live,
        }
        return deepcopy(live)

    def enable_production_execution_egress(self, *, runtime_boot_id):
        assert runtime_boot_id == "boot-1"
        self.state["execution_egress_enabled"] = True
        self.state["execution_egress_authority"] = {
            "runtime_boot_id": runtime_boot_id,
            "authority_revision": 1,
        }
        return deepcopy(self.state["execution_egress_authority"])

    def disable_execution_egress(
        self,
        *,
        reason,
        revoke_release,
        preserve_queued_exposure_reducing,
    ):
        del revoke_release
        self.egress_disable_called = True
        self.preserve_queued_exposure_reducing = bool(
            preserve_queued_exposure_reducing
        )
        self.state["execution_egress_enabled"] = False
        current = dict(self.state.get("production_scalp_authority") or {})
        if current:
            self.state["production_scalp_authority"] = {
                **current,
                "status": "revoked",
                "reason": str(reason),
            }
        live = dict(self.state["runtime_diag"].get("orchestration_live") or {})
        self.state["runtime_diag"]["orchestration_live"] = {
            **live,
            "runtime_enabled": False,
            "queue_kill_active": True,
        }
        return {"execution_egress_enabled": False, "reason": str(reason)}

    def compare_and_set_production_scalp_authority(
        self,
        *,
        next_authority,
        validation_verification=None,
        expected_generation_id="",
        safety_dominant=False,
        **_kwargs,
    ):
        del expected_generation_id
        if safety_dominant:
            current = dict(self.state.get("production_scalp_authority") or {})
            revoked = {
                **current,
                "status": "revoked",
                "reason": next_authority.get("reason"),
            }
            self.state["production_scalp_authority"] = revoked
            return {"updated": True, "reason": "updated", "authority": revoked}
        self.activation_verification = validation_verification
        self.state["production_scalp_authority"] = dict(next_authority)
        return {
            "updated": True,
            "reason": "updated",
            "authority": dict(next_authority),
        }


def test_runtime_attestation_binds_engine_certificate_and_boot() -> None:
    attestation = build_scalp_runtime_attestation(
        runtime_boot_id="boot-1",
        runtime_pid=123,
        runtime_config_sha256="1" * 64,
        admission=_admission(),
        attested_at=NOW,
    )

    assert attestation["valid"] is True
    assert attestation["runtime_boot_id"] == "boot-1"
    assert attestation["engine_sha256"] == "d" * 64
    assert attestation["validation_certificate_sha256"] == "a" * 64
    assert attestation["symbol_scope"] == list(IG_MT4_SCALP_SYMBOLS)


def test_control_plane_activates_exact_scope_with_verifier_object() -> None:
    service = _Service()
    admission = _admission()

    result = ensure_production_scalp_authority(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=admission,
        now_epoch=NOW,
    )

    assert result.active is True
    assert result.authority_revision == 1
    assert result.authority["symbol_scope"] == list(IG_MT4_SCALP_SYMBOLS)
    assert result.authority["runtime_release_certificate_sha256"] == "a" * 64
    assert result.authority["research_evidence_sha256"] == "b" * 64
    assert result.authority["registry_sha256"] == "1" * 64
    assert result.authority["qualification_surface_sha256"] == "2" * 64
    assert result.authority["cost_mapping_sha256"] == "3" * 64
    assert result.authority["execution_contract_sha256"] == "4" * 64
    assert result.authority["validation_expires_at_epoch"] == NOW + 86_400.0
    assert service.activation_verification is admission.verification
    assert service.state["execution_egress_enabled"] is True

    payload = result.to_dict()
    assert payload == asdict(result)
    payload["authority"]["status"] = "mutated"
    payload["live_command_admission"]["allowed"] = False
    if payload["egress"] is not None:
        payload["egress"]["enabled"] = False
    assert result.authority["status"] == "active"
    assert result.live_command_admission["allowed"] is True
    assert result.to_dict() == asdict(result)


def test_control_plane_uses_the_same_runtime_contract_for_real_account() -> None:
    service = _Service()
    service.state["broker_account_mode"] = "real"
    settings = _settings()
    settings.live_expected_account_mode = "real"
    demo_admission = _admission()
    admission = replace(
        demo_admission,
        verification=replace(demo_admission.verification, account_mode="real"),
    )

    result = ensure_production_scalp_authority(
        service=service,
        settings=settings,
        runtime_boot_id="boot-1",
        admission=admission,
        now_epoch=NOW,
    )

    assert result.active is True
    assert result.authority["account_mode"] == "real"
    assert service.activation_verification is admission.verification


def test_direct_demo_control_plane_cannot_activate_demo_egress() -> None:
    service = _Service()
    admission = _admission(direct_demo=True)

    attestation = build_scalp_runtime_attestation(
        runtime_boot_id="boot-1",
        runtime_pid=123,
        runtime_config_sha256="1" * 64,
        admission=admission,
        attested_at=NOW,
    )

    result = ensure_production_scalp_authority(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=admission,
        now_epoch=NOW,
    )

    assert attestation["valid"] is False
    assert "scalp_signed_validation_required" in attestation["errors"]
    assert result.active is False
    assert result.reason == "scalp_signed_validation_required"
    assert result.live_command_admission["allowed"] is False
    assert service.activation_verification is None
    assert service.state["execution_egress_enabled"] is False


def test_invalid_cycle_validation_revokes_entry_authority_without_killing_egress() -> None:
    service = _Service()
    service.state["execution_egress_enabled"] = True
    service.state["production_scalp_authority"] = {
        "status": "active",
        "generation_id": "scalp-generation-1",
    }

    result = ensure_production_scalp_authority(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=_admission(valid=False),
        now_epoch=NOW,
    )

    assert result.active is False
    assert result.reason == "validation_certificate_revoked"
    assert service.state["production_scalp_authority"]["status"] == "revoked"
    assert service.state["execution_egress_enabled"] is True
    assert service.egress_disable_called is False


def _prior_authority() -> dict:
    return build_active_authority(
        ScalpAuthorityExpectation(
            generation_id="prior-scalp-generation",
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256="1" * 64,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="2" * 64,
            runtime_release_signing_key_id="3" * 64,
            research_evidence_sha256="4" * 64,
            research_evidence_signing_key_id="5" * 64,
            registry_generation_id="prior-scalp-generation",
            registry_revision=2,
            registry_sha256="6" * 64,
            qualification_surface_sha256="7" * 64,
            cost_mapping_sha256="8" * 64,
            execution_contract_sha256="9" * 64,
            validation_expires_at_epoch=NOW - 1.0,
            runtime_boot_id="prior-boot",
            authority_revision=7,
        ),
        activated_at=NOW - 3_600.0,
    )


def test_invalid_admission_arms_exact_exit_only_protective_egress() -> None:
    service = _Service()
    prior = _prior_authority()
    service.state["production_scalp_authority"] = deepcopy(prior)

    result = ensure_production_scalp_protective_management_egress(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=_admission(valid=False),
    )

    assert result.active is True
    assert result.reason == "protective_management_only"
    assert result.live_command_admission["allowed"] is False
    assert result.authority["status"] == "revoked"
    assert result.authority["binding_sha256"] == prior["binding_sha256"]
    assert service.egress_disable_called is True
    assert service.preserve_queued_exposure_reducing is True
    live = service.state["runtime_diag"]["orchestration_live"]
    assert live["active_pair_scope"] == list(IG_MT4_SCALP_SYMBOLS)
    assert live["active_sleeve_scope"] == ["scalp"]
    assert live["active_intent_scope"] == ["exit"]
    assert live["budget_scale"] == 0.0
    assert service.state["execution_egress_enabled"] is True


def test_invalid_admission_preserves_legacy_direct_demo_protective_egress() -> None:
    service = _Service()
    prior = {
        "schema_version": SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
        "status": "active",
        "source": "production_runtime",
        "admission_mode": SCALP_ADMISSION_MODE_DIRECT_DEMO,
        "account_mode": "demo",
        "generation_id": "direct-demo-v1",
        "strategy_id": "legacy-scalp-strategy",
        "engine_sha256": "1" * 64,
        "config_sha256": "2" * 64,
        "validation_evidence_sha256": "3" * 64,
        "validation_expires_at_epoch": NOW - 1.0,
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "bracket_policy": BROKER_NATIVE_BRACKET_POLICY,
        "max_entries_per_symbol_utc_day": 1,
        "runtime_boot_id": "prior-boot",
        "authority_revision": 7,
    }
    prior["binding_sha256"] = authority_binding_sha256(prior)
    service.state["production_scalp_authority"] = deepcopy(prior)

    result = ensure_production_scalp_protective_management_egress(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=_admission(valid=False),
    )

    assert result.active is True
    assert result.reason == "protective_management_only"
    assert result.authority["admission_mode"] == SCALP_ADMISSION_MODE_DIRECT_DEMO
    assert result.authority["status"] == "revoked"
    assert service.state["execution_egress_enabled"] is True


def test_protective_egress_refuses_tampered_prior_authority() -> None:
    service = _Service()
    prior = _prior_authority()
    prior["engine_sha256"] = "9" * 64
    service.state["production_scalp_authority"] = prior

    result = ensure_production_scalp_protective_management_egress(
        service=service,
        settings=_settings(),
        runtime_boot_id="boot-1",
        admission=_admission(valid=False),
    )

    assert result.active is False
    assert result.reason == "scalp_protective_authority_binding_invalid"
    assert service.state["execution_egress_enabled"] is False


def test_demo_protective_egress_does_not_depend_on_removed_arming_toggle() -> None:
    service = _Service()
    service.state["production_scalp_authority"] = _prior_authority()
    settings = _settings()
    settings.live_armed = False

    result = ensure_production_scalp_protective_management_egress(
        service=service,
        settings=settings,
        runtime_boot_id="boot-1",
        admission=_admission(valid=False),
    )

    assert result.active is True
    assert result.reason == "protective_management_only"
    assert service.egress_disable_called is True
    assert service.state["execution_egress_enabled"] is True
