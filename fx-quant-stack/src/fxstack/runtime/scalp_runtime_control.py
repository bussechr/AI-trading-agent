"""Canonical control-plane activation for the production IG MT4 scalper."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from types import SimpleNamespace
from typing import Any

from fxstack._serialization import copy_json_payload, flat_dataclass_dict
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime.orchestration_bridge import (
    live_command_admission_diagnostics,
    live_mode_enabled,
)
from fxstack.runtime.mtvclc_runtime_release import (
    MTVCLCRuntimeReleaseVerification,
)
from fxstack.runtime.scalp_execution_authority import (
    authority_error,
    build_active_authority,
    expectation_from_mtvclc_runtime_release,
    protective_authority_structure_error,
)
from fxstack.runtime.scalp_runtime_admission import ScalpRuntimeAdmission
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_SIGNED,
)


SCALP_RUNTIME_ATTESTATION_SCHEMA = "fxstack.production_scalp_runtime_attestation.v1"


def _signed_admission_error(admission: ScalpRuntimeAdmission) -> str:
    verification = admission.verification
    if not isinstance(verification, MTVCLCRuntimeReleaseVerification):
        return "scalp_mtvclc_runtime_release_verification_required"
    if (
        str(verification.admission_mode or SCALP_ADMISSION_MODE_SIGNED)
        .strip()
        .lower()
        != SCALP_ADMISSION_MODE_SIGNED
    ):
        return "scalp_signed_validation_required"
    if not admission.valid or not verification.valid:
        return str(admission.reason or verification.reason or "scalp_validation_invalid")
    if verification.authenticated is not True:
        return "scalp_validation_unauthenticated"
    if verification.revocation_verified is not True:
        return "scalp_validation_revocation_unverified"
    if str(verification.account_mode or "").strip().lower() not in {
        "demo",
        "real",
    }:
        return "scalp_mtvclc_account_mode_invalid"
    if str(verification.engine_sha256 or "").lower() != str(
        admission.engine_identity.engine_sha256 or ""
    ).lower():
        return "scalp_mtvclc_engine_identity_changed"
    try:
        expectation_from_mtvclc_runtime_release(
            verification,
            runtime_boot_id="admission-structure-check",
            authority_revision=1,
        )
    except ValueError as exc:
        return str(exc)
    return ""


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def build_scalp_runtime_attestation(
    *,
    runtime_boot_id: str,
    runtime_pid: int,
    runtime_config_sha256: str,
    admission: ScalpRuntimeAdmission,
    attested_at: float,
) -> dict[str, Any]:
    """Bind the boot to the installed engine and selected admission mode."""

    verification = admission.verification
    timestamp = _finite(attested_at)
    admission_error = _signed_admission_error(admission)
    valid = bool(
        str(runtime_boot_id or "").strip()
        and int(runtime_pid) > 0
        and len(str(runtime_config_sha256 or "").strip()) == 64
        and admission.valid
        and verification.valid
        and not admission_error
        and timestamp > 0.0
    )
    errors: list[str] = []
    if not valid:
        if not admission.valid:
            errors.extend(admission.errors or (admission.reason,))
        if admission_error:
            errors.append(admission_error)
        if not str(runtime_boot_id or "").strip():
            errors.append("runtime_boot_id_missing")
        if int(runtime_pid) <= 0:
            errors.append("runtime_pid_invalid")
        if len(str(runtime_config_sha256 or "").strip()) != 64:
            errors.append("runtime_config_sha256_invalid")
        if timestamp <= 0.0:
            errors.append("runtime_attestation_time_invalid")
    return {
        "schema_version": SCALP_RUNTIME_ATTESTATION_SCHEMA,
        "runtime_boot_id": str(runtime_boot_id),
        "runtime_pid": int(runtime_pid),
        "attested_at": float(timestamp),
        "runtime_config_sha256": str(runtime_config_sha256).strip().lower(),
        "entry_strategy_family": "mtvclc",
        "engine_sha256": str(admission.engine_identity.engine_sha256),
        "validation_certificate_sha256": str(
            getattr(verification, "runtime_release_certificate_sha256", "")
        ),
        "runtime_release_certificate_sha256": str(
            getattr(verification, "runtime_release_certificate_sha256", "")
        ),
        "runtime_release_signing_key_id": str(
            getattr(verification, "runtime_release_signing_key_id", "")
        ),
        "research_evidence_sha256": str(
            getattr(verification, "evidence_sha256", "")
        ),
        "research_evidence_signing_key_id": str(
            getattr(verification, "evidence_signing_key_id", "")
        ),
        "registry_generation_id": str(
            getattr(verification, "registry_generation_id", "")
        ),
        "registry_revision": int(
            getattr(verification, "registry_revision", 0) or 0
        ),
        "registry_sha256": str(
            getattr(verification, "registry_sha256", "")
        ),
        "qualification_surface_sha256": str(
            getattr(verification, "qualification_surface_sha256", "")
        ),
        "cost_mapping_sha256": str(
            getattr(verification, "cost_mapping_sha256", "")
        ),
        "execution_contract_sha256": str(
            getattr(verification, "execution_contract_sha256", "")
        ),
        "validation_evidence_sha256": str(
            getattr(verification, "evidence_sha256", "")
        ),
        "validation_signing_key_id": str(
            getattr(verification, "runtime_release_signing_key_id", "")
        ),
        "admission_mode": str(verification.admission_mode),
        "validation_expires_at_epoch": float(verification.expires_at_epoch),
        "generation_id": str(verification.generation_id),
        "strategy_id": str(verification.strategy_id),
        "strategy_version": str(verification.strategy_version),
        "config_sha256": str(verification.config_sha256),
        "symbol_scope": list(verification.symbol_scope),
        "valid": bool(valid),
        "errors": list(dict.fromkeys(str(item) for item in errors if str(item))),
    }


def _rollout_model_sets(settings: Any, admission: ScalpRuntimeAdmission) -> dict[str, Any]:
    signed_admission_valid = not _signed_admission_error(admission)
    live_enabled = bool(
        live_mode_enabled(settings)
        and bool(getattr(settings, "live_armed", False))
        and signed_admission_valid
    )
    pair_scope = {
        str(item).strip().upper()
        for item in list(getattr(settings, "agent_live_pair_allowlist", []) or [])
        if str(item).strip()
    }
    budget_scale = max(
        0.0,
        min(
            1.0,
            _finite(
                getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0),
                1.0,
            ),
        ),
    )
    model_set_id = str(
        getattr(
            admission.verification,
            "runtime_release_certificate_sha256",
            "",
        )
        or admission.verification.generation_id
    )
    out: dict[str, Any] = {}
    for symbol in IG_MT4_SCALP_SYMBOLS:
        active = bool(
            live_enabled
            and admission.valid
            and symbol in pair_scope
            and budget_scale > 0.0
        )
        out[symbol] = SimpleNamespace(
            model_set_id=model_set_id,
            rollout_policy={
                "configured": active,
                "source": "production_operator_scope",
                "mode": "live" if active else "off",
                "enabled": active,
                "active": active,
                "pair": symbol,
                "pair_allowlisted": active,
                "allowlisted_pairs": [symbol] if active else [],
                "budget_scale": budget_scale if active else 0.0,
                "budget_reason": (
                    "operator_armed_production_runtime"
                    if active
                    else "production_live_scope_inactive"
                ),
            },
        )
    return out


def scalp_live_command_admission(
    *,
    settings: Any,
    admission: ScalpRuntimeAdmission,
) -> dict[str, Any]:
    return live_command_admission_diagnostics(
        settings=settings,
        model_sets=_rollout_model_sets(settings, admission),
    )


def revoke_production_scalp_authority(
    *,
    service: Any,
    reason: str,
) -> dict[str, Any]:
    """Safety-dominant revocation that leaves protective egress available."""

    state = dict(service.get_state() or {})
    current = dict(state.get("production_scalp_authority") or {})
    if not current or str(current.get("status") or "").lower() == "revoked":
        return {
            "updated": False,
            "reason": "already_revoked_or_absent",
            "authority": current,
        }
    return service.compare_and_set_production_scalp_authority(
        next_authority={
            "status": "revoked",
            "generation_id": str(current.get("generation_id") or ""),
            "reason": str(reason or "scalp_validation_invalid"),
        },
        expected_generation_id=str(current.get("generation_id") or ""),
        safety_dominant=True,
    )


@dataclass(frozen=True, slots=True)
class ScalpAuthorityActivationResult:
    active: bool
    reason: str
    errors: tuple[str, ...]
    authority: dict[str, Any]
    live_command_admission: dict[str, Any]
    authority_revision: int = 0
    egress: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = flat_dataclass_dict(self)
        payload["authority"] = copy_json_payload(self.authority)
        payload["live_command_admission"] = copy_json_payload(
            self.live_command_admission
        )
        payload["egress"] = copy_json_payload(self.egress)
        return payload


@dataclass(frozen=True, slots=True)
class ScalpProtectiveManagementActivationResult:
    active: bool
    reason: str
    errors: tuple[str, ...]
    authority: dict[str, Any]
    live_command_admission: dict[str, Any]
    authority_revision: int = 0
    egress: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = flat_dataclass_dict(self)
        payload["authority"] = copy_json_payload(self.authority)
        payload["live_command_admission"] = copy_json_payload(
            self.live_command_admission
        )
        payload["egress"] = copy_json_payload(self.egress)
        return payload


def ensure_production_scalp_protective_management_egress(
    *,
    service: Any,
    settings: Any,
    runtime_boot_id: str,
    admission: ScalpRuntimeAdmission,
) -> ScalpProtectiveManagementActivationResult:
    """Arm exact-owner EXIT egress when entry evidence is invalid.

    The durable prior authority is revoked first and retained only as a
    tamper-evident ownership identity.  No validation expiry, prior boot, or
    prior revision is reused as entry authority.
    """

    command_admission = scalp_live_command_admission(
        settings=settings,
        admission=admission,
    )

    def _result(
        *,
        active: bool,
        reason: str,
        errors: tuple[str, ...],
        authority: dict[str, Any] | None = None,
        revision: int = 0,
        egress: dict[str, Any] | None = None,
    ) -> ScalpProtectiveManagementActivationResult:
        return ScalpProtectiveManagementActivationResult(
            active=active,
            reason=reason,
            errors=errors,
            authority=dict(authority or {}),
            live_command_admission=dict(command_admission),
            authority_revision=int(revision),
            egress=dict(egress or {}),
        )

    if not live_mode_enabled(settings):
        return _result(active=False, reason="not_live_mode", errors=())
    expected_mode = str(
        getattr(settings, "live_expected_account_mode", "") or ""
    ).strip().lower()
    if not bool(getattr(settings, "live_armed", False)) and expected_mode != "demo":
        reason = "scalp_protective_management_live_not_armed"
        return _result(active=False, reason=reason, errors=(reason,))
    if admission.valid:
        reason = "scalp_protective_management_requires_invalid_admission"
        return _result(active=False, reason=reason, errors=(reason,))
    if bool(command_admission.get("allowed", False)):
        reason = "scalp_protective_management_entry_admission_not_blocked"
        return _result(active=False, reason=reason, errors=(reason,))

    disable_reason = str(
        admission.reason or "scalp_validation_invalid_protective_management"
    )
    try:
        service.disable_execution_egress(
            reason=disable_reason,
            revoke_release=True,
            preserve_queued_exposure_reducing=True,
        )
    except Exception as exc:
        reason = (
            "scalp_protective_management_disable_failed:"
            f"{type(exc).__name__}:{exc}"
        )
        return _result(active=False, reason=reason, errors=(reason,))

    state = dict(service.get_state() or {})
    authority = dict(state.get("production_scalp_authority") or {})
    authority_failure = protective_authority_structure_error(authority)
    if authority_failure:
        return _result(
            active=False,
            reason=authority_failure,
            errors=(authority_failure,),
            authority=authority,
        )

    configured_pairs = tuple(
        str(item or "").strip().upper()
        for item in list(getattr(settings, "pairs", []) or [])
    )
    live_pairs = tuple(
        str(item or "").strip().upper()
        for item in list(
            getattr(settings, "agent_live_pair_allowlist", []) or []
        )
    )
    state_errors: list[str] = []
    if configured_pairs != tuple(IG_MT4_SCALP_SYMBOLS):
        state_errors.append("scalp_protective_configured_pair_scope_invalid")
    if live_pairs != tuple(IG_MT4_SCALP_SYMBOLS):
        state_errors.append("scalp_protective_live_pair_scope_invalid")
    if str(state.get("broker_venue_id") or "").strip().lower() != IG_MT4_VENUE_ID:
        state_errors.append("scalp_protective_broker_venue_unattested")
    account_mode = str(state.get("broker_account_mode") or "").strip().lower()
    expected_mode = str(
        getattr(settings, "live_expected_account_mode", "") or ""
    ).strip().lower()
    if account_mode not in {"demo", "real"} or account_mode != expected_mode:
        state_errors.append("scalp_protective_broker_account_mode_mismatch")
    if not str(state.get("broker_account_scope") or "").strip():
        state_errors.append("scalp_protective_broker_account_scope_unattested")
    try:
        broker_magic = int(state.get("broker_account_magic") or 0)
    except (TypeError, ValueError, OverflowError):
        broker_magic = 0
    if broker_magic <= 0:
        state_errors.append("scalp_protective_broker_magic_unattested")
    if state_errors:
        return _result(
            active=False,
            reason=state_errors[0],
            errors=tuple(state_errors),
            authority=authority,
        )

    runtime_diag = dict(state.get("runtime_diag") or {})
    current_live = dict(runtime_diag.get("orchestration_live") or {})
    runtime_diag["live_command_admission"] = dict(command_admission)
    try:
        service.patch_state(
            {
                "__expected_orchestration_live_authority__": current_live,
                "runtime_diag": runtime_diag,
            }
        )
        state = dict(service.get_state() or {})
        current_live = dict(
            dict(state.get("runtime_diag") or {}).get(
                "orchestration_live"
            )
            or {}
        )
        live = service.patch_orchestration_live_state(
            updates={
                "enabled": True,
                "mode": "live",
                "runtime_enabled": True,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "active_pair_scope": list(IG_MT4_SCALP_SYMBOLS),
                "active_sleeve_scope": ["scalp"],
                "active_intent_scope": ["exit"],
                "active_pair_scope_configured": True,
                "active_sleeve_scope_configured": True,
                "active_intent_scope_configured": True,
                "current_stage_index": 0,
                "current_stage_pct": 0,
                "budget_scale": 0.0,
                "bundle_run_id": str(
                    authority.get("runtime_release_certificate_sha256")
                    or authority.get("validation_evidence_sha256")
                    or ""
                ),
                "release_status": "protective_management_only",
                "signoff_records": [],
            },
            expected_live_authority=current_live,
            allow_reenable=True,
        )
        revision = int(_finite(live.get("authority_revision")))
        if revision <= 0:
            raise RuntimeError("protective_management_authority_revision_invalid")
        egress = service.enable_production_execution_egress(
            runtime_boot_id=str(runtime_boot_id),
        )
    except Exception as exc:
        reason = (
            "scalp_protective_management_activation_failed:"
            f"{type(exc).__name__}:{exc}"
        )
        try:
            service.disable_execution_egress(
                reason=reason,
                revoke_release=True,
                preserve_queued_exposure_reducing=True,
            )
        except Exception:
            pass
        return _result(
            active=False,
            reason=reason,
            errors=(reason,),
            authority=dict(
                service.get_state().get("production_scalp_authority") or {}
            ),
        )

    return _result(
        active=True,
        reason="protective_management_only",
        errors=(),
        authority=authority,
        revision=revision,
        egress=egress,
    )


def ensure_production_scalp_authority(
    *,
    service: Any,
    settings: Any,
    runtime_boot_id: str,
    admission: ScalpRuntimeAdmission,
    now_epoch: float | None = None,
) -> ScalpAuthorityActivationResult:
    """Activate one verified generation through the canonical DB control plane."""

    now = _finite(time.time() if now_epoch is None else now_epoch)
    command_admission = scalp_live_command_admission(
        settings=settings,
        admission=admission,
    )
    if not live_mode_enabled(settings):
        return ScalpAuthorityActivationResult(
            active=False,
            reason="not_live_mode",
            errors=(),
            authority={},
            live_command_admission=command_admission,
        )
    if now <= 0.0 or not admission.valid:
        reason = (
            "validation_clock_invalid" if now <= 0.0 else admission.reason
        )
        revoke_production_scalp_authority(service=service, reason=reason)
        return ScalpAuthorityActivationResult(
            active=False,
            reason=str(reason),
            errors=(str(reason),),
            authority=dict(
                service.get_state().get("production_scalp_authority") or {}
            ),
            live_command_admission=command_admission,
        )
    signed_admission_failure = _signed_admission_error(admission)
    if signed_admission_failure:
        revoke_production_scalp_authority(
            service=service,
            reason=signed_admission_failure,
        )
        return ScalpAuthorityActivationResult(
            active=False,
            reason=signed_admission_failure,
            errors=(signed_admission_failure,),
            authority=dict(
                service.get_state().get("production_scalp_authority") or {}
            ),
            live_command_admission=command_admission,
        )
    if not bool(command_admission.get("allowed", False)):
        errors = tuple(str(item) for item in command_admission.get("blockers") or [])
        revoke_production_scalp_authority(
            service=service,
            reason=(errors[0] if errors else "scalp_live_command_admission_blocked"),
        )
        return ScalpAuthorityActivationResult(
            active=False,
            reason="scalp_live_command_admission_blocked",
            errors=errors or ("scalp_live_command_admission_blocked",),
            authority=dict(
                service.get_state().get("production_scalp_authority") or {}
            ),
            live_command_admission=command_admission,
        )

    state = dict(service.get_state() or {})
    runtime_diag = dict(state.get("runtime_diag") or {})
    current_live = dict(runtime_diag.get("orchestration_live") or {})
    current_revision = int(_finite(current_live.get("authority_revision")))
    verification = admission.verification
    if current_revision > 0:
        current_expectation = expectation_from_mtvclc_runtime_release(
            verification,
            runtime_boot_id=str(runtime_boot_id),
            authority_revision=current_revision,
        )
        current_authority = dict(state.get("production_scalp_authority") or {})
        if (
            state.get("execution_egress_enabled") is True
            and not authority_error(
                current_authority,
                expectation=current_expectation,
                now_epoch=now,
            )
        ):
            return ScalpAuthorityActivationResult(
                active=True,
                reason="already_active",
                errors=(),
                authority=current_authority,
                live_command_admission=command_admission,
                authority_revision=current_revision,
                egress=dict(state.get("execution_egress_authority") or {}),
            )

    current_authority = dict(state.get("production_scalp_authority") or {})
    if (
        str(current_authority.get("status") or "").lower() == "active"
        and str(current_authority.get("generation_id") or "")
        != str(verification.generation_id)
    ):
        revoke_production_scalp_authority(
            service=service,
            reason="scalp_generation_replaced",
        )
        state = dict(service.get_state() or {})
        runtime_diag = dict(state.get("runtime_diag") or {})
        current_live = dict(runtime_diag.get("orchestration_live") or {})

    runtime_diag["live_command_admission"] = dict(command_admission)
    service.patch_state(
        {
            "__expected_orchestration_live_authority__": current_live,
            "runtime_diag": runtime_diag,
        }
    )
    state = dict(service.get_state() or {})
    current_live = dict(
        dict(state.get("runtime_diag") or {}).get("orchestration_live") or {}
    )
    pair_scope = list(getattr(settings, "agent_live_pair_allowlist", []) or [])
    sleeve_scope = list(getattr(settings, "agent_live_sleeve_allowlist", []) or [])
    intent_scope = list(getattr(settings, "agent_live_intent_allowlist", []) or [])
    budget_scale = max(
        0.0,
        min(
            1.0,
            _finite(
                getattr(settings, "capital_rollout_budget_scale_full_risk", 1.0),
                1.0,
            ),
        ),
    )
    try:
        live = service.patch_orchestration_live_state(
            updates={
                "enabled": True,
                "mode": "live",
                "runtime_enabled": True,
                "queue_kill_active": False,
                "queue_kill_reason": "",
                "queue_killed_at": 0.0,
                "active_pair_scope": pair_scope,
                "active_sleeve_scope": sleeve_scope,
                "active_intent_scope": intent_scope,
                "active_pair_scope_configured": True,
                "active_sleeve_scope_configured": True,
                "active_intent_scope_configured": True,
                "current_stage_index": 0,
                "current_stage_pct": 100,
                "budget_scale": budget_scale,
                "bundle_run_id": str(
                    verification.runtime_release_certificate_sha256
                ),
                "release_status": "mtvclc_runtime_release_active",
                "signoff_records": [],
            },
            expected_live_authority=current_live,
            allow_reenable=True,
        )
        revision = int(_finite(live.get("authority_revision")))
        egress = service.enable_production_execution_egress(
            runtime_boot_id=str(runtime_boot_id),
        )
        expectation = expectation_from_mtvclc_runtime_release(
            verification,
            runtime_boot_id=str(runtime_boot_id),
            authority_revision=revision,
        )
        authority = build_active_authority(expectation, activated_at=now)
        activated = service.compare_and_set_production_scalp_authority(
            next_authority=authority,
            validation_verification=verification,
        )
    except Exception as exc:
        reason = f"scalp_authority_activation_failed:{type(exc).__name__}:{exc}"
        revoke_production_scalp_authority(service=service, reason=reason)
        return ScalpAuthorityActivationResult(
            active=False,
            reason=reason,
            errors=(reason,),
            authority=dict(
                service.get_state().get("production_scalp_authority") or {}
            ),
            live_command_admission=command_admission,
        )
    if not bool(activated.get("updated", False)):
        reason = str(activated.get("reason") or "scalp_authority_activation_refused")
        revoke_production_scalp_authority(service=service, reason=reason)
        return ScalpAuthorityActivationResult(
            active=False,
            reason=reason,
            errors=(reason,),
            authority=dict(activated.get("authority") or {}),
            live_command_admission=command_admission,
            authority_revision=revision,
            egress=dict(egress or {}),
        )
    return ScalpAuthorityActivationResult(
        active=True,
        reason="active",
        errors=(),
        authority=dict(activated.get("authority") or authority),
        live_command_admission=command_admission,
        authority_revision=revision,
        egress=dict(egress or {}),
    )


__all__ = [
    "SCALP_RUNTIME_ATTESTATION_SCHEMA",
    "ScalpAuthorityActivationResult",
    "ScalpProtectiveManagementActivationResult",
    "build_scalp_runtime_attestation",
    "ensure_production_scalp_authority",
    "ensure_production_scalp_protective_management_egress",
    "revoke_production_scalp_authority",
    "scalp_live_command_admission",
]
