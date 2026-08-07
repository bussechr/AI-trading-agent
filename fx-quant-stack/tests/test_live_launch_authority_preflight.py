from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.live_launch_authority_preflight import (
    validate_live_launch_authority,
)
from fxstack.runtime.scalp_engine_identity import ProductionScalpEngineIdentity
from fxstack.runtime.scalp_execution_authority import (
    build_active_authority,
    expectation_from_mtvclc_runtime_release,
)
from fxstack.runtime.scalp_runtime_admission import ScalpRuntimeAdmission
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
    SCALP_ADMISSION_MODE_SIGNED,
)
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


NOW = 1_800_000_000.0
EXPIRY = NOW + 3_600.0


def _settings(*, account_mode: str = "demo") -> SimpleNamespace:
    return SimpleNamespace(
        entry_strategy_family="mtvclc",
        live_expected_account_mode=account_mode,
    )


def _signed_admission(
    *,
    certificate: str = "c" * 64,
    account_mode: str = "demo",
) -> ScalpRuntimeAdmission:
    verification = MTVCLCRuntimeReleaseVerification(
        valid=True,
        reason="",
        errors=(),
        authenticated=True,
        revocation_verified=True,
        release_bundle_sha256="1" * 64,
        certificate_sha256=certificate,
        runtime_release_certificate_sha256=certificate,
        evidence_bundle_sha256="2" * 64,
        evidence_certificate_sha256="3" * 64,
        evidence_sha256="4" * 64,
        signing_key_id="5" * 64,
        runtime_release_signing_key_id="5" * 64,
        evidence_signing_key_id="6" * 64,
        registry_generation_id="signed-generation-1",
        generation_id="signed-generation-1",
        strategy_id=MTVCLC_STRATEGY_ID,
        strategy_version=MTVCLC_STRATEGY_VERSION,
        engine_sha256="a" * 64,
        config_id=MTVCLC_CONFIG_ID,
        config_sha256=MTVCLC_CONFIG_SHA256,
        venue_id=IG_MT4_VENUE_ID,
        account_mode=account_mode,
        scope_version=IG_MT4_SCALP_SCOPE_VERSION,
        symbol_scope=IG_MT4_SCALP_SYMBOLS,
        max_entries_per_symbol_utc_day=1,
        maximum_account_currency_risk_per_trade=1.0,
        issued_at_epoch=NOW - 60.0,
        expires_at_epoch=EXPIRY,
        release_expires_at_epoch=EXPIRY,
        evidence_expires_at_epoch=EXPIRY,
        registry_expires_at_epoch=EXPIRY,
        registry_revision=11,
        registry_sha256="7" * 64,
        previous_registry_sha256="8" * 64,
        deployment_sha256="9" * 64,
        execution_contract_sha256="b" * 64,
        qualification_surface_sha256="c" * 64,
        cost_mapping_sha256="d" * 64,
        cost_rows_sha256="e" * 64,
        win_probability_lower_bounds={
            symbol: {"BUY": 0.8, "SELL": 0.8}
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
        admission_mode=SCALP_ADMISSION_MODE_SIGNED,
    )
    return ScalpRuntimeAdmission(
        valid=True,
        reason="",
        errors=(),
        verification=verification,
        engine_identity=ProductionScalpEngineIdentity(
            engine_sha256="a" * 64,
            component_sha256=(),
        ),
        bundle_file_sha256="f" * 64,
        release_public_key_file_sha256="1" * 64,
        evidence_public_key_file_sha256="2" * 64,
    )


def _active_state(admission: ScalpRuntimeAdmission) -> dict[str, object]:
    verification = admission.verification
    authority = build_active_authority(
        expectation_from_mtvclc_runtime_release(
            verification,
            runtime_boot_id="boot-active-1",
            authority_revision=7,
        ),
        activated_at=NOW - 30.0,
    )
    return {
        "runtime_boot_id": "boot-active-1",
        "production_scalp_authority": authority,
    }


def test_initial_live_launch_requires_exact_active_signed_service_authority() -> None:
    admission = _signed_admission()

    result = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=_active_state(admission),
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert result.valid is True
    assert len(result.binding_sha256) == 64
    assert result.generation_id == "signed-generation-1"
    assert result.strategy_id == MTVCLC_STRATEGY_ID


def test_runtime_native_cold_start_requires_inert_authenticated_service() -> None:
    admission = replace(_signed_admission(), bundle_path="runtime-native")
    inert_state = {
        "database_ok": True,
        "execution_egress_enabled": False,
        "runtime_status": "stale",
    }

    allowed = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=inert_state,
        admission_verifier=lambda *_args, **_kwargs: admission,
    )
    refused = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state={**inert_state, "execution_egress_enabled": True},
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert allowed.valid is True
    assert len(allowed.binding_sha256) == 64
    assert refused.valid is False
    assert refused.errors == ("live_launch_runtime_native_egress_not_disabled",)


def test_real_account_uses_same_launch_path_with_matching_signed_authority() -> None:
    admission = _signed_admission(account_mode="real")

    result = validate_live_launch_authority(
        _settings(account_mode="real"),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=_active_state(admission),
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert result.valid is True
    assert len(result.binding_sha256) == 64


def test_launch_refuses_cross_account_signed_authority() -> None:
    admission = _signed_admission(account_mode="real")

    result = validate_live_launch_authority(
        _settings(account_mode="demo"),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=_active_state(admission),
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert result.valid is False
    assert "live_launch_signed_release_account_mode_changed" in result.errors


def test_direct_demo_cannot_satisfy_live_launch_release_gate() -> None:
    signed = _signed_admission()
    direct = ScalpRuntimeAdmission(
        valid=True,
        reason="",
        errors=(),
        verification=replace(
            signed.verification,
            authenticated=False,
            revocation_verified=False,
            signing_key_id="",
            admission_mode=SCALP_ADMISSION_MODE_DIRECT_DEMO,
        ),
        engine_identity=signed.engine_identity,
    )

    result = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state={},
        admission_verifier=lambda *_args, **_kwargs: direct,
    )

    assert result.valid is False
    assert "live_launch_signed_release_required" in result.errors
    assert "live_launch_signed_release_not_authenticated" in result.errors


def test_inactive_or_boot_detached_authority_fails_before_runtime_mutation() -> None:
    admission = _signed_admission()
    state = _active_state(admission)
    authority = dict(state["production_scalp_authority"])  # type: ignore[arg-type]
    authority["status"] = "revoked"
    state["production_scalp_authority"] = authority
    state["runtime_boot_id"] = "other-boot"

    result = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=state,
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert result.valid is False
    assert "scalp_authority_inactive" in result.errors
    assert "live_launch_authority_runtime_boot_changed" in result.errors


def test_controlled_replacement_reverifies_the_exact_pre_mutation_binding() -> None:
    admission = _signed_admission()
    initial = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW,
        current_state=_active_state(admission),
        admission_verifier=lambda *_args, **_kwargs: admission,
    )
    assert initial.valid is True

    continued = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW + 1.0,
        expected_binding_sha256=initial.binding_sha256,
        current_state=None,
        admission_verifier=lambda *_args, **_kwargs: admission,
    )
    changed = _signed_admission(certificate="9" * 64)
    refused = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="mtvclc",
        now_epoch=NOW + 1.0,
        expected_binding_sha256=initial.binding_sha256,
        current_state=None,
        admission_verifier=lambda *_args, **_kwargs: changed,
    )

    assert continued.valid is True
    assert continued.binding_sha256 == initial.binding_sha256
    assert refused.valid is False
    assert refused.errors == ("live_launch_release_binding_changed",)


def test_strategy_family_cannot_change_around_the_signed_gate() -> None:
    admission = _signed_admission()

    result = validate_live_launch_authority(
        _settings(),
        selected_strategy_family="model_stack",
        now_epoch=NOW,
        current_state=_active_state(admission),
        admission_verifier=lambda *_args, **_kwargs: admission,
    )

    assert result.valid is False
    assert result.errors == (
        "live_launch_strategy_family_changed",
        "live_launch_signed_authority_strategy_unsupported",
    )
