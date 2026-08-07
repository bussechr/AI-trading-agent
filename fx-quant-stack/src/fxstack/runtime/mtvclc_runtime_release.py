"""Public-only verifier for an MTVCLC IG-DEMO runtime release.

The validation-evidence v3 certificate deliberately carries no runtime
authority.  This module verifies a separate Ed25519 release certificate and
its signed revocation registry before authenticating the embedded evidence.
It contains no issuer, private-key, persistence, activation, broker, or trade
surface.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import get_ig_mt4_instrument
from fxstack.runtime import mtvclc_validation_evidence_v3 as evidence_v3
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_SHA256 as PRODUCTION_MTVCLC_CONFIG_SHA256,
)


# AGENT: ROLE: public-only MTVCLC IG-DEMO runtime-release verifier.
# AGENT: HANDSHAKE: outer-v2 + v5 prereg + registry -> evidence-v3 -> result.
# AGENT: ISOLATION: no signing, secrets, persistence, activation, or broker I/O.

MTVCLC_RUNTIME_RELEASE_CERTIFICATE_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_release_certificate.v2"
)
MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_release_revocations.v1"
)
MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_release_bundle.v2"
)
MTVCLC_VALIDATED_PREREGISTRATION_BINDING_SCHEMA = (
    "fxstack.scalp.mtvclc_validated_preregistration_binding.v1"
)
MTVCLC_V5_PREREGISTRATION_TOOL_REVISION = (
    "fxstack.scalp.mtvclc_runtime_bound_preregistration_tool.v5"
)
MTVCLC_RUNTIME_POLICY_BINDING_SCHEMA = (
    "fxstack.scalp.mtvclc_runtime_policy_binding.v1"
)
MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE = (
    "mtvclc_ig_demo_runtime_release_ed25519.v1"
)
MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE = (
    "mtvclc_ig_demo_runtime_release_eligibility.v1"
)
MTVCLC_COST_CALIBRATION_SCHEMA = "fxstack.strategy.mtvclc.cost_calibration.v1"
MTVCLC_RUNTIME_COST_MAPPING_SCHEMA = (
    "fxstack.runtime.mtvclc_cost_calibration_mapping.v1"
)
MTVCLC_RUNTIME_COST_MAPPING_VERSION = (
    "ig_mt4_ordered22_evidence_cost_rows.v1"
)
MTVCLC_QUALIFICATION_SURFACE_SCHEMA = (
    "fxstack.runtime.mtvclc_qualification_surface.v1"
)
MTVCLC_IMMEDIATE_TRADE_CONTRACT_SCHEMA = (
    "fxstack.runtime.mtvclc_immediate_trade_contract.v1"
)
MTVCLC_IG_DEMO_DEPLOYMENT_SCHEMA = (
    "fxstack.runtime.mtvclc_ig_demo_deployment.v1"
)
PRODUCTION_SCALP_ENGINE_IDENTITY_SCHEMA = (
    "fxstack.production_scalp_engine_identity.v3"
)

CERTIFICATE_SHA256_FIELD = "certificate_body_sha256"
CERTIFICATE_SIGNATURE_FIELD = "certificate_signature_ed25519"
REGISTRY_SHA256_FIELD = "registry_sha256"
REGISTRY_SIGNATURE_FIELD = "revocation_signature_ed25519"
BUNDLE_SHA256_FIELD = "bundle_body_sha256"

MAXIMUM_RELEASE_VALIDITY_SECONDS = evidence_v3.MAX_CERTIFICATE_VALIDITY_SECS
MAXIMUM_REGISTRY_VALIDITY_SECONDS = evidence_v3.MAX_CERTIFICATE_VALIDITY_SECS
MAXIMUM_ACCOUNT_CURRENCY_RISK_PER_TRADE = 1.0
MAXIMUM_ENTRIES_PER_SYMBOL_UTC_DAY = 1
PUBLIC_EVIDENCE_FAMILY_ATTEMPTED_CELLS = 4_874
LEGACY_V2_PUBLIC_EVIDENCE_ACCEPTED = False

if (
    evidence_v3.MTVCLC_VALIDATION_EVIDENCE_SCHEMA
    != "fxstack.scalp.mtvclc_validation_evidence.v3"
    or evidence_v3.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA
    != "fxstack.scalp.mtvclc_validation_certificate.v3"
    or evidence_v3.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA
    != "fxstack.scalp.mtvclc_signed_evidence_bundle.v3"
    or evidence_v3.WILSON_FAMILY_ATTEMPTED_CELLS
    != PUBLIC_EVIDENCE_FAMILY_ATTEMPTED_CELLS
):
    raise RuntimeError("mtvclc_runtime_release_public_evidence_family_invalid")

RUNTIME_RELEASE_AUTHORITY: dict[str, bool] = {
    "activation_authorized": True,
    "runtime_authorized": True,
    "entry_lane_authorized": True,
    "individual_trade_authorized": False,
    "registry_write_authorized": False,
    "broker_access_authorized": False,
    "broker_trade_authorized": False,
    "research_access_authorized": False,
    "real_account_authorized": False,
}

EXPECTED_V5_ABANDONED_PREREGISTRATIONS: tuple[dict[str, Any], ...] = (
    {
        "preregistration_body_sha256": (
            "5ff534bff7013d5836ab0034d2ef13d0739c56046d4d2637a5fd73df917c97d7"
        ),
        "artifact_file_sha256": (
            "fd32199fe13f6989743e7ad1b607ef39c2129bb3a7d20d24661eb13793347f9b"
        ),
        "reason": "atomic_publish_temp_hardlink_retained",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    {
        "preregistration_body_sha256": (
            "0511ee9c98204edc6dfd5166abade98eb47ec4f10c6364c36f504c7151853d96"
        ),
        "artifact_file_sha256": (
            "3cf1b36e4cd311425515ba8fe2268626f0c8e84ed750264e9793dd4e83c283a3"
        ),
        "reason": "first_cycle_refused_opaque_broker_token_misclassified_as_utc",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "attempted_cells_increment": 0,
    },
    {
        "preregistration_body_sha256": (
            "d80e6cc9f05726ff2d2e851890ec06ca1b2df8e08efb4066bc83e31c216f17f3"
        ),
        "artifact_file_sha256": (
            "566c85789fdf1f8639a4c010421d27f3b02e6dd2f9a1912968ca8f9b500c51b3"
        ),
        "reason": "authenticated_mt4_restart_revised_completed_bar_history",
        "eligible_observations_emitted": True,
        "manifest_entries_emitted": 1448,
        "manifest_file_sha256": (
            "86a28670961de97980a09018ea69a9190223459581ba5bfdf00a01aef0b22a6e"
        ),
        "final_manifest_entry_sha256": (
            "ec8f23c7b35a79ccce1cfb269ccd287301800c7e3c845710b775001147f1b163"
        ),
        "attempted_cells_increment": 44,
        "signal_evaluation_performed": False,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "success_claim_evaluated": False,
    },
    {
        "preregistration_body_sha256": (
            "07b78ce6d697a61db308560c325f547e90984143f2b71c1613bb8ee12b9f879c"
        ),
        "artifact_file_sha256": (
            "127d3647ab6378962c8f938e84522b16e3f82dbf6057ef553b7a087e6a4f4cba"
        ),
        "reason": "same_source_restart_late_unseen_completed_bar_backfill",
        "eligible_observations_emitted": True,
        "manifest_entries_emitted": 3,
        "manifest_file_sha256": (
            "7d3e925f819e8e49c071e088a062263dba9354467308c44e536e75b6382d737a"
        ),
        "final_manifest_entry_sha256": (
            "27feebe74d2cb172e68f70278dddd25c6ada8f5b9c1166483f8e8bbc3ccad7a9"
        ),
        "attempted_cells_increment": 44,
        "signal_evaluation_performed": False,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "success_claim_evaluated": False,
    },
    {
        "preregistration_body_sha256": (
            "95306e016d8af3128a26228857e6953691c0ac88e881f619e074db101c95d4a1"
        ),
        "artifact_file_sha256": (
            "21d2f17464067a7e8145c051c557f0c717a2ca123bec0a11d3de26528e0d6dee"
        ),
        "reason": (
            "late_capture_then_upstream_bridge_ea_software_changed_after_"
            "eligible_observations"
        ),
        "eligible_observations_emitted": True,
        "manifest_entries_emitted": 1,
        "manifest_file_size_bytes": 4105,
        "manifest_file_sha256": (
            "ca3ca02d0053096f41991a45d2e10260c5ddd07cdf968a8eaf5467c3ffaa5fa0"
        ),
        "active_journal_records_emitted": 363,
        "active_journal_file_size_bytes": 5857990,
        "active_journal_file_sha256": (
            "30df9f8116655d74e0e367cbc88f0303d821936609b89a3c932653d7521351b3"
        ),
        "capture_finalization_state": (
            "stopped_active_journal_preserved_unfinalized"
        ),
        "start_edge_durable_receipt_emitted": False,
        "upstream_producer_software_changed_after_eligible_observations": True,
        "attempted_cells_increment": 44,
        "signal_evaluation_performed": False,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "success_claim_evaluated": False,
    },
    {
        "preregistration_body_sha256": (
            "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
        ),
        "artifact_file_sha256": (
            "7755d0e5facb7b5f28d6f5a092355e7b33d1d8d790b64bd59d204e220a5c7e55"
        ),
        "attempt_failure_sha256": (
            "56f23f4298f010bf105246ed611ba9cdd47d81e8c55131324d8a0aa6a8e3ac78"
        ),
        "reason": "unresolved_first_cycle_reservation_after_process_interruption",
        "eligible_observations_emitted": False,
        "manifest_entries_emitted": 0,
        "tail_commitment_file_size_bytes": 15303,
        "tail_commitment_file_sha256": (
            "d770d66bd8cb138b6eb142063aa106788cb2e8be9d4479660ed82c26569b9eb8"
        ),
        "tail_commitment_last_write_utc": "2026-08-04T00:59:20.687771Z",
        "immutable_guard_identity_sha256": (
            "f90fea1134d431feb3ab8a0021bf54159bc249a477490284534d9c444b8c5cf3"
        ),
        "attempted_cells_increment": 44,
        "signal_evaluation_performed": False,
        "outcome_evaluation_performed": False,
        "performance_statistics_computed": False,
        "success_claim_evaluated": False,
    },
)
EXPECTED_V5_REPLACEMENT_LINEAGE: dict[str, Any] = {
    "replaces_preregistration_body_sha256": (
        "45f6ca5ccb23195a8253dedfc4aef1b0e95f319d35c55558aa706bfa513952bd"
    ),
    "old_attempt_counted_in_multiplicity_family": True,
    "old_capture_used_for_signal_outcome_or_performance_selection": False,
    "old_window_restart_or_extension": False,
    "new_independent_window_required": True,
    "replacement_reason": (
        "new_independent_window_after_failed_v4_first_cycle_reservation"
    ),
}

EXPECTED_DEPLOYMENT: dict[str, Any] = {
    "schema_version": MTVCLC_IG_DEMO_DEPLOYMENT_SCHEMA,
    "strategy_family": "mtvclc",
    "venue_id": evidence_v3.IG_MT4_VENUE_ID,
    "account_mode": evidence_v3.MTVCLC_ACCOUNT_MODE,
    "broker_account_scope": "IG-DEMO",
    "broker_account_scope_policy": "exact_ig_demo_account_only_no_fallback",
    "account_currency": "USD",
    "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
    "symbol_scope": list(evidence_v3.IG_MT4_SCALP_SYMBOLS),
}

EXPECTED_IMMEDIATE_TRADE_CONTRACT: dict[str, Any] = {
    "schema_version": MTVCLC_IMMEDIATE_TRADE_CONTRACT_SCHEMA,
    "trade_instruction": "immediate_market_trade",
    "execution_type": "market",
    "buy_entry_price_side": "ask",
    "sell_entry_price_side": "bid",
    "pending_orders_forbidden": True,
    "pending_trades_forbidden": True,
    "entry_deadline_policy": "expected_entry_epoch_plus_5_seconds",
    "maximum_entry_delay_seconds": 5,
    "maximum_quote_gap_seconds": 5,
    "target_cost_multiple": 4.0,
    "stop_cost_multiple": 8.0,
    "time_stop_m1_bars": 30,
    "outcome_horizon_m1_bars": 30,
    "maximum_entries_per_symbol_utc_day": MAXIMUM_ENTRIES_PER_SYMBOL_UTC_DAY,
    "bracket_policy": "broker_native_sl_tp",
    "maximum_slippage_points": 20,
    "protection_cushion_points": 5,
    "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
    "rollover_entry_blackout_half_open": True,
    "signals_inside_blackout_reserve": False,
    "maximum_account_currency_risk_per_trade": (
        MAXIMUM_ACCOUNT_CURRENCY_RISK_PER_TRADE
    ),
    "risk_currency_basis": "account_currency",
    "account_currency": "USD",
}

_BUNDLE_FIELDS = {
    "schema_version",
    "evidence_bundle",
    "certificate",
    "revocation_registry",
    BUNDLE_SHA256_FIELD,
}
_CERTIFICATE_FIELDS = {
    "schema_version",
    "generation_id",
    "strategy_id",
    "strategy_version",
    "config_id",
    "config_sha256",
    "evaluator_source_sha256",
    "engine_binding",
    "deployment",
    "execution_contract",
    "validated_preregistration_binding",
    "evidence_binding",
    "cost_binding",
    "qualification_surface",
    "authority_purpose",
    "authority",
    "issued_at_epoch",
    "expires_at_epoch",
    "signing_key_purpose",
    "signing_key_id",
    CERTIFICATE_SHA256_FIELD,
    CERTIFICATE_SIGNATURE_FIELD,
}
_VALIDATED_PREREGISTRATION_BINDING_FIELDS = {
    "schema_version",
    "preregistration",
    "preregistration_tool_revision",
    "preregistration_body_sha256",
    "preregistration_artifact_sha256",
    "runtime_policy_binding",
    "sealed_engine_identity",
}
_RUNTIME_POLICY_BINDING_FIELDS = {
    "schema_version",
    "source_identity_label",
    "source_identity",
    "strategy_id",
    "strategy_version",
    "config_id",
    "config_sha256",
    "evaluator_entrypoint",
    "ordered_symbols",
    "ordered_cell_count",
    "engine_identity_sha256",
    "entry_type",
    "immediate_market_trade",
    "buy_price_basis",
    "sell_price_basis",
    "pending_orders_forbidden",
    "pending_trades_forbidden",
    "prospective_outcome_evaluation_not_before_sealed_end",
    "runtime_or_broker_authority_granted",
}
_REGISTRY_FIELDS = {
    "schema_version",
    "registry_generation_id",
    "registry_revision",
    "previous_registry_sha256",
    "issued_at_epoch",
    "expires_at_epoch",
    "active_certificate_sha256",
    "revoked_certificate_sha256s",
    "signing_key_purpose",
    "signing_key_id",
    REGISTRY_SHA256_FIELD,
    REGISTRY_SIGNATURE_FIELD,
}
_RUNTIME_COST_CONSTRUCTOR_FIELDS = (
    "symbol",
    "calibration_id",
    "source_sha256",
    "p90_spread_bps",
    "commission_bps_per_round_trip",
    "financing_bps_per_trade",
    "account_currency",
    "pnl_currency",
    "convert_on_close_charge_fraction",
    "adverse_execution_debit_bps",
)


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) else None


def _strict_int(value: Any, *, minimum: int = 0) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        return None
    return value


def _parse_utc_second(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.endswith("Z"):
        return None
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return None
    if parsed.tzinfo != UTC or parsed.microsecond != 0:
        return None
    return parsed


def _body_sha256(
    payload: Mapping[str, Any], *, excluded_fields: set[str]
) -> str:
    return evidence_v3.canonical_sha256(
        {
            key: value
            for key, value in dict(payload).items()
            if key not in excluded_fields
        }
    )


def release_certificate_body_sha256(certificate: Mapping[str, Any]) -> str:
    """Hash the exact unsigned outer release-certificate body."""

    return _body_sha256(
        certificate,
        excluded_fields={CERTIFICATE_SHA256_FIELD, CERTIFICATE_SIGNATURE_FIELD},
    )


def release_registry_body_sha256(registry: Mapping[str, Any]) -> str:
    """Hash the exact unsigned revocation-registry body."""

    return _body_sha256(
        registry,
        excluded_fields={REGISTRY_SHA256_FIELD, REGISTRY_SIGNATURE_FIELD},
    )


def release_bundle_body_sha256(bundle: Mapping[str, Any]) -> str:
    """Hash the atomic outer bundle excluding only its claimed body hash."""

    return _body_sha256(bundle, excluded_fields={BUNDLE_SHA256_FIELD})


def _verify_signature(
    *, payload: Mapping[str, Any], signature_field: str, public_key: Any
) -> str:
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PublicKey,
        )
    except ImportError:
        return "signature_backend_unavailable"
    if not isinstance(public_key, Ed25519PublicKey):
        return "public_key_invalid"
    encoded = payload.get(signature_field)
    if not isinstance(encoded, str) or not encoded.strip():
        return "signature_missing"
    try:
        signature = base64.b64decode(encoded, validate=True)
        material = {
            key: value for key, value in payload.items() if key != signature_field
        }
        public_key.verify(signature, evidence_v3.canonical_json_bytes(material))
    except (InvalidSignature, TypeError, ValueError):
        return "signature_invalid"
    return ""


def _engine_sha256(components: Sequence[tuple[str, str]]) -> str:
    payload = {
        "schema_version": PRODUCTION_SCALP_ENGINE_IDENTITY_SCHEMA,
        "components": [
            {"path": path, "sha256": digest} for path, digest in components
        ],
    }
    return hashlib.sha256(evidence_v3.canonical_json_bytes(payload)).hexdigest()


def _expected_engine_binding(
    expectation: "MTVCLCRuntimeReleaseExpectation",
) -> dict[str, Any]:
    return {
        "schema_version": PRODUCTION_SCALP_ENGINE_IDENTITY_SCHEMA,
        "engine_sha256": expectation.engine_sha256,
        "components": [
            {"path": path, "sha256": digest}
            for path, digest in expectation.engine_component_sha256
        ],
    }


def _expected_sealed_engine_identity(
    expectation: "MTVCLCRuntimeReleaseExpectation",
) -> dict[str, Any]:
    """Return the JSON shape emitted by ProductionScalpEngineIdentity.to_dict()."""

    return {
        "engine_sha256": expectation.engine_sha256,
        "component_sha256": [
            [path, digest] for path, digest in expectation.engine_component_sha256
        ],
        "schema_version": PRODUCTION_SCALP_ENGINE_IDENTITY_SCHEMA,
    }


def _preregistration_body_sha256(
    preregistration: Mapping[str, Any],
) -> str:
    body = dict(preregistration)
    body.pop("preregistration_body_sha256", None)
    encoded = json.dumps(
        body,
        allow_nan=False,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _preregistration_artifact_bytes(
    preregistration: Mapping[str, Any],
) -> bytes:
    """Reproduce the exact pretty JSON encoding used by v5/v3-wire publish."""

    return (
        json.dumps(
            dict(preregistration),
            allow_nan=False,
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _preregistration_artifact_sha256(
    preregistration: Mapping[str, Any],
) -> str:
    return hashlib.sha256(_preregistration_artifact_bytes(preregistration)).hexdigest()


def _derived_validated_preregistration_binding(
    preregistration: Mapping[str, Any],
) -> dict[str, Any] | None:
    runtime_policy = preregistration.get("runtime_policy_binding")
    identities = preregistration.get("source_identities")
    runtime_context = (
        identities.get("production_runtime_context")
        if isinstance(identities, Mapping)
        else None
    )
    sealed_engine = (
        runtime_context.get("engine_identity")
        if isinstance(runtime_context, Mapping)
        else None
    )
    body_sha = str(
        preregistration.get("preregistration_body_sha256") or ""
    ).lower()
    if (
        preregistration.get("preregistration_tool_revision")
        != MTVCLC_V5_PREREGISTRATION_TOOL_REVISION
        or not _is_sha256(body_sha)
        or not isinstance(runtime_policy, Mapping)
        or not isinstance(sealed_engine, Mapping)
    ):
        return None
    try:
        artifact_sha = _preregistration_artifact_sha256(preregistration)
    except (TypeError, ValueError):
        return None
    return {
        "schema_version": MTVCLC_VALIDATED_PREREGISTRATION_BINDING_SCHEMA,
        "preregistration": dict(preregistration),
        "preregistration_tool_revision": MTVCLC_V5_PREREGISTRATION_TOOL_REVISION,
        "preregistration_body_sha256": body_sha,
        "preregistration_artifact_sha256": artifact_sha,
        "runtime_policy_binding": dict(runtime_policy),
        "sealed_engine_identity": dict(sealed_engine),
    }


def _validated_preregistration_error(
    *,
    preregistration: Mapping[str, Any],
    binding: Mapping[str, Any],
    evidence: Mapping[str, Any],
    certificate: Mapping[str, Any],
    expectation: "MTVCLCRuntimeReleaseExpectation",
) -> str:
    """Independently verify the exact v5 preregistration and runtime policy."""

    if set(binding) != _VALIDATED_PREREGISTRATION_BINDING_FIELDS:
        return "validated_preregistration_binding_scope_invalid"
    derived = _derived_validated_preregistration_binding(preregistration)
    if derived is None:
        return "validated_preregistration_invalid"
    if dict(binding) != derived:
        return "validated_preregistration_binding_mismatch"
    claimed_body_sha = str(
        binding.get("preregistration_body_sha256") or ""
    ).lower()
    try:
        computed_body_sha = _preregistration_body_sha256(preregistration)
    except (TypeError, ValueError):
        return "validated_preregistration_body_invalid"
    if not hmac.compare_digest(claimed_body_sha, computed_body_sha):
        return "validated_preregistration_body_sha256_mismatch"
    artifacts = evidence.get("artifacts")
    if not isinstance(artifacts, Mapping):
        return "validated_preregistration_evidence_artifacts_invalid"
    evidence_body_sha = str(
        artifacts.get("preregistration_body_sha256") or ""
    ).lower()
    evidence_artifact_sha = str(
        artifacts.get("preregistration_artifact_sha256") or ""
    ).lower()
    if not _is_sha256(evidence_body_sha) or not hmac.compare_digest(
        claimed_body_sha, evidence_body_sha
    ):
        return "validated_preregistration_evidence_body_sha256_mismatch"
    claimed_artifact_sha = str(
        binding.get("preregistration_artifact_sha256") or ""
    ).lower()
    if not _is_sha256(evidence_artifact_sha) or not hmac.compare_digest(
        claimed_artifact_sha, evidence_artifact_sha
    ):
        return "validated_preregistration_evidence_artifact_sha256_mismatch"

    accounting = preregistration.get("attempt_accounting")
    abandoned = preregistration.get("abandoned_preregistrations")
    window = preregistration.get("prospective_window")
    sealed_at = _parse_utc_second(preregistration.get("sealed_at_utc"))
    t0 = (
        _parse_utc_second(window.get("t0_utc_inclusive"))
        if isinstance(window, Mapping)
        else None
    )
    window_end = (
        _parse_utc_second(window.get("end_utc_exclusive"))
        if isinstance(window, Mapping)
        else None
    )
    if (
        preregistration.get("schema_version")
        != "fxstack.scalp.mtvclc_preregistration.v1"
        or preregistration.get("declaration_revision")
        != "fxstack.scalp.mtvclc_gap_v3_preregistration.v1"
        or preregistration.get("capture_profile_id") != "gap_v3_source_pinned"
        or preregistration.get("collector_wire_profile")
        != "gap_v3_source_pinned"
        or preregistration.get("research_only") is not True
    ):
        return "validated_preregistration_successor_identity_invalid"
    if accounting != {
        "prior_attempted_cells_lower_bound": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_874,
    }:
        return "validated_preregistration_attempt_accounting_invalid"
    if abandoned != [
        dict(item) for item in EXPECTED_V5_ABANDONED_PREREGISTRATIONS
    ]:
        return "validated_preregistration_abandoned_lineage_invalid"
    if preregistration.get("replacement_lineage") != EXPECTED_V5_REPLACEMENT_LINEAGE:
        return "validated_preregistration_replacement_lineage_invalid"
    if (
        not isinstance(window, Mapping)
        or sealed_at is None
        or t0 is None
        or window_end is None
        or not sealed_at < t0
        or window_end - t0 != timedelta(days=180)
        or window.get("consecutive_days") != 180
        or window.get("success_evaluation_not_before_utc")
        != window.get("end_utc_exclusive")
        or window.get("fixed_before_any_eligible_observation") is not True
        or window.get("observations_before_t0_forbidden") is not True
        or window.get("observations_at_or_after_end_forbidden") is not True
        or window.get("interim_signal_or_outcome_evaluation_forbidden") is not True
        or window.get("interim_performance_statistics_forbidden") is not True
        or window.get("early_success_forbidden") is not True
        or window.get("no_optional_extension_or_restart_after_failure") is not True
        or window.get("data_quality_monitoring_must_not_compute_performance")
        is not True
    ):
        return "validated_preregistration_window_invalid"

    sealed_engine = binding.get("sealed_engine_identity")
    expected_engine = _expected_sealed_engine_identity(expectation)
    if not isinstance(sealed_engine, Mapping):
        return "validated_preregistration_engine_identity_invalid"
    if dict(sealed_engine) != expected_engine:
        return "validated_preregistration_engine_identity_mismatch"
    if sealed_engine.get("schema_version") != PRODUCTION_SCALP_ENGINE_IDENTITY_SCHEMA:
        return "validated_preregistration_engine_schema_invalid"

    runtime_policy = binding.get("runtime_policy_binding")
    if (
        not isinstance(runtime_policy, Mapping)
        or set(runtime_policy) != _RUNTIME_POLICY_BINDING_FIELDS
        or runtime_policy.get("schema_version")
        != MTVCLC_RUNTIME_POLICY_BINDING_SCHEMA
    ):
        return "validated_preregistration_runtime_policy_scope_invalid"
    try:
        sealed_engine_sha = evidence_v3.canonical_sha256(sealed_engine)
    except (TypeError, ValueError):
        return "validated_preregistration_engine_identity_invalid"
    if not hmac.compare_digest(
        str(runtime_policy.get("engine_identity_sha256") or "").lower(),
        sealed_engine_sha,
    ):
        return "validated_preregistration_runtime_policy_engine_mismatch"

    strategy = preregistration.get("strategy")
    scope = preregistration.get("scope")
    execution = preregistration.get("execution_contract")
    identities = preregistration.get("source_identities")
    authority = preregistration.get("authority")
    runtime_context = (
        identities.get("production_runtime_context")
        if isinstance(identities, Mapping)
        else None
    )
    source_identity = (
        identities.get("production_strategy_policy_source")
        if isinstance(identities, Mapping)
        else None
    )
    if (
        not isinstance(strategy, Mapping)
        or not isinstance(scope, Mapping)
        or not isinstance(execution, Mapping)
        or not isinstance(runtime_context, Mapping)
        or not isinstance(source_identity, Mapping)
        or not isinstance(authority, Mapping)
        or not authority
        or any(value is not False for value in authority.values())
    ):
        return "validated_preregistration_envelope_invalid"

    identity_claims = {
        "strategy_id": certificate.get("strategy_id"),
        "strategy_version": certificate.get("strategy_version"),
        "config_id": certificate.get("config_id"),
        "config_sha256": certificate.get("config_sha256"),
    }
    if any(
        strategy.get(field_name) != expected
        or runtime_policy.get(field_name) != expected
        for field_name, expected in identity_claims.items()
    ):
        return "validated_preregistration_strategy_config_mismatch"
    if (
        runtime_context.get("relationship")
        != "active_runtime_policy_identity_context_only_no_authority"
        or runtime_context.get("active_strategy_family_context")
        != certificate.get("strategy_id")
        or runtime_context.get("active_strategy_version_context")
        != certificate.get("strategy_version")
        or runtime_context.get("active_policy_config_sha256_context")
        != certificate.get("config_sha256")
    ):
        return "validated_preregistration_runtime_context_mismatch"

    ordered_symbols = list(evidence_v3.IG_MT4_SCALP_SYMBOLS)
    expected_cell_order = [
        {
            "config_id": certificate.get("config_id"),
            "symbol": symbol,
            "side": side,
        }
        for symbol in ordered_symbols
        for side in ("BUY", "SELL")
    ]
    deployment = certificate.get("deployment")
    if (
        not isinstance(deployment, Mapping)
        or deployment.get("venue_id") != scope.get("venue_id")
        or deployment.get("scope_version") != scope.get("scope_version")
        or deployment.get("symbol_scope") != ordered_symbols
        or scope.get("ordered_symbols") != ordered_symbols
        or scope.get("sides") != ["BUY", "SELL"]
        or scope.get("cell_order") != expected_cell_order
        or runtime_policy.get("ordered_symbols") != ordered_symbols
        or runtime_policy.get("ordered_cell_count") != len(expected_cell_order)
    ):
        return "validated_preregistration_scope_deployment_mismatch"

    component_rows = sealed_engine.get("component_sha256")
    strategy_component_sha = ""
    if isinstance(component_rows, list):
        matches = [
            row[1]
            for row in component_rows
            if isinstance(row, list)
            and len(row) == 2
            and row[0] == "strategy/mtvclc.py"
            and _is_sha256(row[1])
        ]
        if len(matches) == 1:
            strategy_component_sha = str(matches[0]).lower()
    if (
        not strategy_component_sha
        or runtime_policy.get("source_identity_label")
        != "production_strategy_policy_source"
        or runtime_policy.get("source_identity") != dict(source_identity)
        or source_identity.get("filename") != "mtvclc.py"
        or not hmac.compare_digest(
            str(source_identity.get("sha256") or "").lower(),
            strategy_component_sha,
        )
    ):
        return "validated_preregistration_policy_source_mismatch"

    release_execution = certificate.get("execution_contract")
    if (
        not isinstance(release_execution, Mapping)
        or runtime_policy.get("evaluator_entrypoint") != "evaluate_mtvclc"
        or runtime_policy.get("entry_type") != "immediate_market"
        or runtime_policy.get("immediate_market_trade") is not True
        or runtime_policy.get("buy_price_basis") != "authenticated_ask"
        or runtime_policy.get("sell_price_basis") != "authenticated_bid"
        or runtime_policy.get("pending_orders_forbidden") is not True
        or runtime_policy.get("pending_trades_forbidden") is not True
        or runtime_policy.get(
            "prospective_outcome_evaluation_not_before_sealed_end"
        )
        is not True
        or runtime_policy.get("runtime_or_broker_authority_granted") is not False
        or execution.get("entry_type") != "immediate_market"
        or execution.get("pending_orders_forbidden") is not True
        or release_execution.get("trade_instruction")
        != "immediate_market_trade"
        or release_execution.get("execution_type") != "market"
        or release_execution.get("buy_entry_price_side") != "ask"
        or release_execution.get("sell_entry_price_side") != "bid"
        or release_execution.get("pending_orders_forbidden") is not True
        or release_execution.get("pending_trades_forbidden") is not True
        or execution.get("maximum_entries_per_symbol_utc_day")
        != release_execution.get("maximum_entries_per_symbol_utc_day")
        or execution.get("outcome_horizon_m1_bars")
        != release_execution.get("outcome_horizon_m1_bars")
        or execution.get("rollover_entry_blackout_utc")
        != release_execution.get("rollover_entry_blackout_utc")
        or execution.get("rollover_entry_blackout_half_open")
        != release_execution.get("rollover_entry_blackout_half_open")
        or execution.get("signals_inside_blackout_reserve")
        != release_execution.get("signals_inside_blackout_reserve")
    ):
        return "validated_preregistration_immediate_execution_mismatch"
    return ""


def _runtime_calibration_row_sha256(row: Mapping[str, Any]) -> str:
    return evidence_v3.canonical_sha256(
        {
            "schema_version": MTVCLC_COST_CALIBRATION_SCHEMA,
            "symbol": row["symbol"],
            "calibration_id": row["calibration_id"],
            "source_sha256": row["source_sha256"],
            "p90_spread_bps": row["p90_spread_bps"],
            "commission_bps_per_round_trip": (
                row["commission_bps_per_round_trip"]
            ),
            "financing_bps_per_trade": row["financing_bps_per_trade"],
            "account_currency": row["account_currency"],
            "pnl_currency": row["pnl_currency"],
            "convert_on_close_charge_fraction": (
                row["convert_on_close_charge_fraction"]
            ),
            "adverse_execution_debit_bps": (
                row["adverse_execution_debit_bps"]
            ),
        }
    )


def _cost_mapping_body_sha256(value: Mapping[str, Any]) -> str:
    return _body_sha256(value, excluded_fields={"mapping_sha256"})


def _derived_cost_binding(evidence: Mapping[str, Any]) -> dict[str, Any] | None:
    costs = evidence.get("costs")
    if not isinstance(costs, Mapping):
        return None
    capture = costs.get("capture_bundle")
    rows = costs.get("rows")
    row_hashes = costs.get("cost_row_sha256_by_symbol")
    if (
        not isinstance(capture, Mapping)
        or not isinstance(capture.get("capture_json"), Mapping)
        or not isinstance(rows, Mapping)
        or not isinstance(row_hashes, Mapping)
    ):
        return None
    cost_rows_sha256 = str(costs.get("cost_rows_sha256") or "").lower()
    cost_policy_sha256 = str(costs.get("cost_policy_sha256") or "").lower()
    if not all(_is_sha256(value) for value in (cost_rows_sha256, cost_policy_sha256)):
        return None
    calibration_id = cost_policy_sha256
    calibrations: list[dict[str, Any]] = []
    for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS:
        raw = rows.get(symbol)
        evidence_row_sha = str(row_hashes.get(symbol) or "").lower()
        if not isinstance(raw, Mapping) or not _is_sha256(evidence_row_sha):
            return None
        try:
            computed_evidence_row_sha = evidence_v3.canonical_sha256(raw)
        except (TypeError, ValueError):
            return None
        if not hmac.compare_digest(evidence_row_sha, computed_evidence_row_sha):
            return None
        instrument = get_ig_mt4_instrument(symbol)
        if str(raw.get("profit_loss_currency") or "") != instrument.quote_ccy:
            return None
        try:
            calibration: dict[str, Any] = {
                "schema_version": MTVCLC_COST_CALIBRATION_SCHEMA,
                "symbol": symbol,
                "calibration_id": calibration_id,
                "source_sha256": evidence_row_sha,
                "p90_spread_bps": raw["p90_ig_spread_bps"],
                "commission_bps_per_round_trip": (
                    raw["commission_bps_per_round_trip"]
                ),
                "financing_bps_per_trade": raw["financing_bps_per_trade"],
                "account_currency": raw["account_currency"],
                "pnl_currency": raw["profit_loss_currency"],
                "convert_on_close_charge_fraction": (
                    raw["convert_on_close_charge_fraction_for_screen"]
                ),
                "adverse_execution_debit_bps": (
                    raw["fixed_adverse_execution_debit_bps"]
                ),
                "evidence_cost_row_sha256": evidence_row_sha,
            }
            calibration["runtime_calibration_row_sha256"] = (
                _runtime_calibration_row_sha256(calibration)
            )
        except (KeyError, TypeError, ValueError):
            return None
        calibrations.append(calibration)
    try:
        if not hmac.compare_digest(
            cost_rows_sha256, evidence_v3.canonical_sha256(dict(rows))
        ):
            return None
    except (TypeError, ValueError):
        return None
    binding = {
        "schema_version": MTVCLC_RUNTIME_COST_MAPPING_SCHEMA,
        "mapping_version": MTVCLC_RUNTIME_COST_MAPPING_VERSION,
        "calibration_schema_version": MTVCLC_COST_CALIBRATION_SCHEMA,
        "calibration_id": calibration_id,
        "cost_policy_sha256": cost_policy_sha256,
        "cost_rows_sha256": cost_rows_sha256,
        "calibrations": calibrations,
    }
    binding["mapping_sha256"] = _cost_mapping_body_sha256(binding)
    return binding


def _derived_qualification_surface(
    evidence: Mapping[str, Any], *, cost_binding: Mapping[str, Any]
) -> dict[str, Any] | None:
    cells = evidence.get("cells")
    if not isinstance(cells, list) or len(cells) != 44:
        return None
    calibrations = cost_binding.get("calibrations")
    if not isinstance(calibrations, list) or len(calibrations) != 22:
        return None
    cost_rows = {
        str(row.get("symbol") or ""): row
        for row in calibrations
        if isinstance(row, Mapping)
    }
    if set(cost_rows) != set(evidence_v3.IG_MT4_SCALP_SYMBOLS):
        return None
    derived: list[dict[str, Any]] = []
    expected_order = [
        (symbol, side)
        for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS
        for side in ("BUY", "SELL")
    ]
    for (symbol, side), raw in zip(expected_order, cells, strict=True):
        if (
            not isinstance(raw, Mapping)
            or raw.get("config_id") != evidence_v3.MTVCLC_CONFIG_ID
            or raw.get("symbol") != symbol
            or raw.get("side") != side
        ):
            return None
        bound = _finite(raw.get("win_probability_wilson_lower"))
        break_even = _finite(raw.get("base_break_even_probability"))
        cost_row = cost_rows[symbol]
        if (
            bound is None
            or break_even is None
            or not 0.0 <= bound <= 1.0
            or not 0.0 <= break_even <= 1.0
            or bound <= break_even
            or not _is_sha256(cost_row.get("evidence_cost_row_sha256"))
        ):
            return None
        try:
            cell_sha = evidence_v3.canonical_sha256(raw)
        except (TypeError, ValueError):
            return None
        derived.append(
            {
                "config_id": evidence_v3.MTVCLC_CONFIG_ID,
                "symbol": symbol,
                "side": side,
                "win_probability_wilson_lower": bound,
                "base_break_even_probability": break_even,
                "evidence_cost_row_sha256": cost_row[
                    "evidence_cost_row_sha256"
                ],
                "evidence_cell_sha256": cell_sha,
            }
        )
    surface: dict[str, Any] = {
        "schema_version": MTVCLC_QUALIFICATION_SURFACE_SCHEMA,
        "method": evidence_v3.WILSON_INTERVAL_METHOD,
        "family_confidence": evidence_v3.WIN_PROBABILITY_FAMILY_CONFIDENCE,
        "attempted_cells": evidence_v3.WILSON_FAMILY_ATTEMPTED_CELLS,
        "alpha_allocation": evidence_v3.WILSON_ALPHA_ALLOCATION,
        "cost_mapping_sha256": cost_binding.get("mapping_sha256"),
        "cells": derived,
    }
    surface["surface_sha256"] = _body_sha256(
        surface, excluded_fields={"surface_sha256"}
    )
    return surface


def _derived_evidence_binding(
    *,
    evidence_bundle: Mapping[str, Any],
    evidence_certificate: Mapping[str, Any],
    verification: evidence_v3.MTVCLCValidationVerification,
) -> dict[str, Any]:
    evidence = evidence_certificate.get("evidence")
    evidence_schema = (
        str(evidence.get("schema_version") or "")
        if isinstance(evidence, Mapping)
        else ""
    )
    return {
        "bundle_schema_version": evidence_v3.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "bundle_body_sha256": str(
            evidence_bundle.get(evidence_v3.BUNDLE_SHA256_FIELD) or ""
        ).lower(),
        "certificate_schema_version": (
            evidence_v3.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA
        ),
        "certificate_body_sha256": verification.certificate_sha256,
        "evidence_schema_version": evidence_schema,
        "evidence_sha256": verification.evidence_sha256,
        "signing_key_id": verification.signing_key_id,
        "evaluator_source_sha256": verification.evaluator_source_sha256,
        "expires_at_epoch": verification.expires_at_epoch,
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
    }


@dataclass(frozen=True, slots=True)
class MTVCLCRuntimeReleaseExpectation:
    """Current generation, exact strategy/config, and measured engine identity."""

    generation_id: str
    config_sha256: str
    engine_sha256: str
    engine_component_sha256: tuple[tuple[str, str], ...]
    evaluator_source_sha256: str = ""
    strategy_id: str = evidence_v3.MTVCLC_STRATEGY_ID
    strategy_version: str = evidence_v3.MTVCLC_STRATEGY_VERSION
    config_id: str = evidence_v3.MTVCLC_CONFIG_ID

    def validation_error(self) -> str:
        if not str(self.generation_id or "").strip():
            return "mtvclc_runtime_release_expected_generation_id_missing"
        if self.strategy_id != evidence_v3.MTVCLC_STRATEGY_ID:
            return "mtvclc_runtime_release_expected_strategy_id_invalid"
        if self.strategy_version != evidence_v3.MTVCLC_STRATEGY_VERSION:
            return "mtvclc_runtime_release_expected_strategy_version_invalid"
        if self.config_id != evidence_v3.MTVCLC_CONFIG_ID:
            return "mtvclc_runtime_release_expected_config_id_invalid"
        if self.config_sha256 != PRODUCTION_MTVCLC_CONFIG_SHA256:
            return "mtvclc_runtime_release_expected_config_sha256_invalid"
        if self.evaluator_source_sha256 and not _is_sha256(
            self.evaluator_source_sha256
        ):
            return "mtvclc_runtime_release_expected_evaluator_sha256_invalid"
        if not _is_sha256(self.engine_sha256):
            return "mtvclc_runtime_release_expected_engine_sha256_invalid"
        if not self.engine_component_sha256:
            return "mtvclc_runtime_release_expected_engine_components_missing"
        if any(
            not isinstance(item, tuple)
            or len(item) != 2
            or not str(item[0] or "").strip()
            or not _is_sha256(item[1])
            for item in self.engine_component_sha256
        ):
            return "mtvclc_runtime_release_expected_engine_components_invalid"
        paths = [item[0] for item in self.engine_component_sha256]
        if len(paths) != len(set(paths)):
            return "mtvclc_runtime_release_expected_engine_components_invalid"
        if not hmac.compare_digest(
            self.engine_sha256, _engine_sha256(self.engine_component_sha256)
        ):
            return "mtvclc_runtime_release_expected_engine_identity_mismatch"
        return ""

    def evidence_expectation(
        self, *, evaluator_source_sha256: str
    ) -> evidence_v3.MTVCLCValidationExpectation:
        return evidence_v3.MTVCLCValidationExpectation(
            generation_id=self.generation_id,
            strategy_id=self.strategy_id,
            strategy_version=self.strategy_version,
            config_id=self.config_id,
            config_sha256=self.config_sha256,
            evaluator_source_sha256=evaluator_source_sha256,
        )


@dataclass(frozen=True, slots=True)
class MTVCLCRuntimeReleaseRegistryAnchor:
    """Durable caller-owned last accepted registry identity."""

    generation_id: str
    registry_revision: int
    registry_sha256: str

    def validation_error(self) -> str:
        if not str(self.generation_id or "").strip():
            return "mtvclc_runtime_release_anchor_generation_id_missing"
        if _strict_int(self.registry_revision, minimum=1) is None:
            return "mtvclc_runtime_release_anchor_revision_invalid"
        if not _is_sha256(self.registry_sha256):
            return "mtvclc_runtime_release_anchor_sha256_invalid"
        return ""


@dataclass(frozen=True, slots=True)
class MTVCLCRuntimeReleaseVerification:
    """Authenticated public release data suitable for an admission adapter."""

    valid: bool
    reason: str
    errors: tuple[str, ...]
    authenticated: bool = False
    revocation_verified: bool = False
    admission_mode: str = "signed_validation"
    release_bundle_sha256: str = ""
    certificate_sha256: str = ""
    runtime_release_certificate_sha256: str = ""
    evidence_bundle_sha256: str = ""
    evidence_certificate_sha256: str = ""
    evidence_sha256: str = ""
    signing_key_id: str = ""
    runtime_release_signing_key_id: str = ""
    evidence_signing_key_id: str = ""
    registry_generation_id: str = ""
    generation_id: str = ""
    strategy_id: str = ""
    strategy_version: str = ""
    engine_sha256: str = ""
    engine_component_sha256: tuple[tuple[str, str], ...] = ()
    preregistration_body_sha256: str = ""
    preregistration_artifact_sha256: str = ""
    runtime_policy_binding_sha256: str = ""
    sealed_engine_identity_sha256: str = ""
    config_id: str = ""
    config_sha256: str = ""
    evaluator_source_sha256: str = ""
    venue_id: str = ""
    account_mode: str = ""
    scope_version: str = ""
    symbol_scope: tuple[str, ...] = ()
    max_entries_per_symbol_utc_day: int = 0
    maximum_account_currency_risk_per_trade: float = 0.0
    issued_at_epoch: float = 0.0
    expires_at_epoch: float = 0.0
    release_expires_at_epoch: float = 0.0
    evidence_expires_at_epoch: float = 0.0
    registry_expires_at_epoch: float = 0.0
    registry_revision: int = 0
    registry_sha256: str = ""
    previous_registry_sha256: str = ""
    authority_purpose: str = ""
    authority: dict[str, Any] = field(default_factory=dict)
    deployment_sha256: str = ""
    execution_contract_sha256: str = ""
    qualification_surface_sha256: str = ""
    qualification_surface: dict[str, Any] = field(default_factory=dict)
    win_probability_lower_bounds: dict[str, dict[str, float]] = field(
        default_factory=dict
    )
    base_break_even_probabilities: dict[str, dict[str, float]] = field(
        default_factory=dict
    )
    evidence_cell_sha256: dict[str, dict[str, str]] = field(default_factory=dict)
    evidence_cost_row_sha256: dict[str, str] = field(default_factory=dict)
    cost_mapping_sha256: str = ""
    cost_rows_sha256: str = ""
    cost_calibration_id: str = ""
    cost_calibration_source_sha256: str = ""
    cost_calibration_source_sha256_by_symbol: dict[str, str] = field(
        default_factory=dict
    )
    cost_calibration_row_sha256: dict[str, str] = field(default_factory=dict)
    cost_calibrations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def to_dict(
        self,
        *,
        include_qualification_surfaces: bool = True,
        include_cost_calibrations: bool = True,
    ) -> dict[str, Any]:
        """Return a JSON-ready snapshot, optionally compacted for telemetry."""

        excluded: set[str] = set()
        if not include_qualification_surfaces:
            excluded.update(
                (
                    "qualification_surface",
                    "win_probability_lower_bounds",
                    "base_break_even_probabilities",
                    "evidence_cell_sha256",
                )
            )
        if not include_cost_calibrations:
            excluded.add("cost_calibrations")
        return {
            item.name: deepcopy(getattr(self, item.name))
            for item in fields(self)
            if item.name not in excluded
        }


def verify_mtvclc_runtime_release(
    *,
    bundle: Mapping[str, Any] | None,
    release_public_key: Any | None,
    evidence_public_key: Any | None,
    expectation: MTVCLCRuntimeReleaseExpectation,
    now_epoch: float,
    registry_anchor: MTVCLCRuntimeReleaseRegistryAnchor | None = None,
) -> MTVCLCRuntimeReleaseVerification:
    """Verify release signature/revocation, evidence-v3, and exact v5 prereg."""

    release_key_id = evidence_v3.ed25519_public_key_id(release_public_key)
    evidence_key_id = evidence_v3.ed25519_public_key_id(evidence_public_key)
    authenticated = False
    revocation_verified = False
    release_cert_sha = ""
    registry_sha = ""
    registry_revision = 0
    previous_registry_sha = ""
    registry_generation_id = ""
    authenticated_evaluator_sha = ""
    release_bundle_sha = ""
    authenticated_evidence_bundle_sha = ""
    evidence_verification: evidence_v3.MTVCLCValidationVerification | None = None
    preregistration_body_sha = ""
    preregistration_artifact_sha = ""
    runtime_policy_binding_sha = ""
    sealed_engine_identity_sha = ""

    def result(
        reason: str,
        *,
        issued_at: float = 0.0,
        release_expires_at: float = 0.0,
        registry_expires_at: float = 0.0,
        cost_binding: Mapping[str, Any] | None = None,
        qualification_surface: Mapping[str, Any] | None = None,
    ) -> MTVCLCRuntimeReleaseVerification:
        valid = not reason
        evidence_expires = (
            evidence_verification.expires_at_epoch
            if evidence_verification is not None
            else 0.0
        )
        effective_expires = (
            min(release_expires_at, registry_expires_at, evidence_expires)
            if valid
            else 0.0
        )
        costs = dict(cost_binding or {})
        calibrations = costs.get("calibrations")
        full_calibration_map = (
            {
                str(item["symbol"]): dict(item)
                for item in calibrations
                if isinstance(item, Mapping) and "symbol" in item
            }
            if valid and isinstance(calibrations, list)
            else {}
        )
        calibration_map = {
            symbol: {
                field_name: row[field_name]
                for field_name in _RUNTIME_COST_CONSTRUCTOR_FIELDS
            }
            for symbol, row in full_calibration_map.items()
        }
        bounds: dict[str, dict[str, float]] = {}
        break_even_probabilities: dict[str, dict[str, float]] = {}
        cell_hashes: dict[str, dict[str, str]] = {}
        cost_row_hashes: dict[str, str] = {}
        surface = dict(qualification_surface or {})
        surface_cells = surface.get("cells")
        if valid:
            for item in surface_cells if isinstance(surface_cells, list) else ():
                symbol = str(item["symbol"])
                side = str(item["side"])
                bounds.setdefault(symbol, {})[side] = float(
                    item["win_probability_wilson_lower"]
                )
                break_even_probabilities.setdefault(symbol, {})[side] = float(
                    item["base_break_even_probability"]
                )
                cell_hashes.setdefault(symbol, {})[side] = str(
                    item["evidence_cell_sha256"]
                )
                cost_row_hashes[symbol] = str(
                    item["evidence_cost_row_sha256"]
                )
        source_by_symbol = {
            symbol: str(row.get("source_sha256") or "")
            for symbol, row in full_calibration_map.items()
        }
        calibration_row_hashes = {
            symbol: str(row.get("runtime_calibration_row_sha256") or "")
            for symbol, row in full_calibration_map.items()
        }
        return MTVCLCRuntimeReleaseVerification(
            valid=valid,
            reason=reason,
            errors=() if valid else (reason,),
            authenticated=authenticated,
            revocation_verified=revocation_verified,
            release_bundle_sha256=release_bundle_sha,
            certificate_sha256=release_cert_sha,
            runtime_release_certificate_sha256=release_cert_sha,
            evidence_bundle_sha256=authenticated_evidence_bundle_sha,
            evidence_certificate_sha256=(
                evidence_verification.certificate_sha256
                if evidence_verification is not None
                else ""
            ),
            evidence_sha256=(
                evidence_verification.evidence_sha256
                if evidence_verification is not None
                else ""
            ),
            signing_key_id=release_key_id,
            runtime_release_signing_key_id=release_key_id,
            evidence_signing_key_id=evidence_key_id,
            registry_generation_id=registry_generation_id,
            generation_id=expectation.generation_id,
            strategy_id=expectation.strategy_id,
            strategy_version=expectation.strategy_version,
            engine_sha256=expectation.engine_sha256,
            engine_component_sha256=expectation.engine_component_sha256,
            preregistration_body_sha256=(
                preregistration_body_sha if valid else ""
            ),
            preregistration_artifact_sha256=(
                preregistration_artifact_sha if valid else ""
            ),
            runtime_policy_binding_sha256=(
                runtime_policy_binding_sha if valid else ""
            ),
            sealed_engine_identity_sha256=(
                sealed_engine_identity_sha if valid else ""
            ),
            config_id=expectation.config_id,
            config_sha256=expectation.config_sha256,
            evaluator_source_sha256=authenticated_evaluator_sha if valid else "",
            venue_id=evidence_v3.IG_MT4_VENUE_ID if valid else "",
            account_mode=evidence_v3.MTVCLC_ACCOUNT_MODE if valid else "",
            scope_version=evidence_v3.IG_MT4_SCALP_SCOPE_VERSION if valid else "",
            symbol_scope=evidence_v3.IG_MT4_SCALP_SYMBOLS if valid else (),
            max_entries_per_symbol_utc_day=(
                MAXIMUM_ENTRIES_PER_SYMBOL_UTC_DAY if valid else 0
            ),
            maximum_account_currency_risk_per_trade=(
                MAXIMUM_ACCOUNT_CURRENCY_RISK_PER_TRADE if valid else 0.0
            ),
            issued_at_epoch=issued_at if valid else 0.0,
            expires_at_epoch=effective_expires,
            release_expires_at_epoch=release_expires_at if valid else 0.0,
            evidence_expires_at_epoch=evidence_expires if valid else 0.0,
            registry_expires_at_epoch=registry_expires_at if valid else 0.0,
            registry_revision=registry_revision,
            registry_sha256=registry_sha,
            previous_registry_sha256=previous_registry_sha,
            authority_purpose=(
                MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE if valid else ""
            ),
            authority=dict(RUNTIME_RELEASE_AUTHORITY) if valid else {},
            deployment_sha256=(
                evidence_v3.canonical_sha256(EXPECTED_DEPLOYMENT) if valid else ""
            ),
            execution_contract_sha256=(
                evidence_v3.canonical_sha256(EXPECTED_IMMEDIATE_TRADE_CONTRACT)
                if valid
                else ""
            ),
            qualification_surface_sha256=(
                str(surface.get("surface_sha256") or "") if valid else ""
            ),
            qualification_surface=surface if valid else {},
            win_probability_lower_bounds=bounds,
            base_break_even_probabilities=break_even_probabilities,
            evidence_cell_sha256=cell_hashes,
            evidence_cost_row_sha256=cost_row_hashes,
            cost_mapping_sha256=(
                str(costs.get("mapping_sha256") or "") if valid else ""
            ),
            cost_rows_sha256=(
                str(costs.get("cost_rows_sha256") or "") if valid else ""
            ),
            cost_calibration_id=(
                str(costs.get("calibration_id") or "") if valid else ""
            ),
            cost_calibration_source_sha256=(
                str(costs.get("cost_rows_sha256") or "") if valid else ""
            ),
            cost_calibration_source_sha256_by_symbol=source_by_symbol,
            cost_calibration_row_sha256=calibration_row_hashes,
            cost_calibrations=calibration_map,
        )

    expectation_error = expectation.validation_error()
    if expectation_error:
        return result(expectation_error)
    if registry_anchor is not None:
        anchor_error = registry_anchor.validation_error()
        if anchor_error:
            return result(anchor_error)
        if registry_anchor.generation_id != expectation.generation_id:
            return result("mtvclc_runtime_release_anchor_generation_id_mismatch")
    if not release_key_id:
        return result("mtvclc_runtime_release_public_key_unavailable")
    if not evidence_key_id:
        return result("mtvclc_runtime_release_evidence_public_key_unavailable")
    if not isinstance(bundle, Mapping) or set(bundle) != _BUNDLE_FIELDS:
        return result("mtvclc_runtime_release_bundle_scope_invalid")
    if bundle.get("schema_version") != MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA:
        return result("mtvclc_runtime_release_bundle_schema_invalid")
    claimed_bundle_sha = str(bundle.get(BUNDLE_SHA256_FIELD) or "").lower()
    try:
        computed_bundle_sha = release_bundle_body_sha256(bundle)
    except (TypeError, ValueError):
        return result("mtvclc_runtime_release_bundle_body_invalid")
    if not _is_sha256(claimed_bundle_sha) or not hmac.compare_digest(
        claimed_bundle_sha, computed_bundle_sha
    ):
        return result("mtvclc_runtime_release_bundle_hash_invalid")
    release_bundle_sha = claimed_bundle_sha

    raw_certificate = bundle.get("certificate")
    if not isinstance(raw_certificate, Mapping):
        return result("mtvclc_runtime_release_certificate_missing")
    certificate = dict(raw_certificate)
    if set(certificate) != _CERTIFICATE_FIELDS:
        return result("mtvclc_runtime_release_certificate_scope_invalid")
    if certificate.get("signing_key_id") != release_key_id:
        return result("mtvclc_runtime_release_certificate_signing_key_mismatch")
    claimed_cert_sha = str(certificate.get(CERTIFICATE_SHA256_FIELD) or "").lower()
    try:
        computed_cert_sha = release_certificate_body_sha256(certificate)
    except (TypeError, ValueError):
        return result("mtvclc_runtime_release_certificate_body_invalid")
    if not _is_sha256(claimed_cert_sha) or not hmac.compare_digest(
        claimed_cert_sha, computed_cert_sha
    ):
        return result("mtvclc_runtime_release_certificate_hash_invalid")
    signature_error = _verify_signature(
        payload=certificate,
        signature_field=CERTIFICATE_SIGNATURE_FIELD,
        public_key=release_public_key,
    )
    if signature_error:
        return result(f"mtvclc_runtime_release_certificate_{signature_error}")
    authenticated = True
    release_cert_sha = claimed_cert_sha
    if certificate.get("schema_version") != MTVCLC_RUNTIME_RELEASE_CERTIFICATE_SCHEMA:
        return result("mtvclc_runtime_release_certificate_schema_invalid")
    if certificate.get("signing_key_purpose") != MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE:
        return result("mtvclc_runtime_release_certificate_key_purpose_invalid")
    expected_claims = {
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "engine_binding": _expected_engine_binding(expectation),
        "deployment": EXPECTED_DEPLOYMENT,
        "execution_contract": EXPECTED_IMMEDIATE_TRADE_CONTRACT,
        "authority_purpose": MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE,
        "authority": RUNTIME_RELEASE_AUTHORITY,
    }
    for field_name, expected in expected_claims.items():
        if certificate.get(field_name) != expected:
            return result(
                f"mtvclc_runtime_release_certificate_{field_name}_mismatch"
            )
    now = _finite(now_epoch)
    issued_at = _finite(certificate.get("issued_at_epoch"))
    release_expires_at = _finite(certificate.get("expires_at_epoch"))
    if now is None or now <= 0.0:
        return result("mtvclc_runtime_release_clock_invalid")
    if (
        issued_at is None
        or release_expires_at is None
        or issued_at <= 0.0
        or release_expires_at <= issued_at
        or release_expires_at - issued_at > MAXIMUM_RELEASE_VALIDITY_SECONDS
    ):
        return result("mtvclc_runtime_release_certificate_time_window_invalid")
    if issued_at > now + 5.0:
        return result("mtvclc_runtime_release_certificate_future_dated")
    if release_expires_at <= now:
        return result("mtvclc_runtime_release_certificate_expired")

    raw_registry = bundle.get("revocation_registry")
    if not isinstance(raw_registry, Mapping):
        return result("mtvclc_runtime_release_registry_missing")
    registry = dict(raw_registry)
    if set(registry) != _REGISTRY_FIELDS:
        return result("mtvclc_runtime_release_registry_scope_invalid")
    if registry.get("signing_key_id") != release_key_id:
        return result("mtvclc_runtime_release_registry_signing_key_mismatch")
    claimed_registry_sha = str(registry.get(REGISTRY_SHA256_FIELD) or "").lower()
    try:
        computed_registry_sha = release_registry_body_sha256(registry)
    except (TypeError, ValueError):
        return result("mtvclc_runtime_release_registry_body_invalid")
    if not _is_sha256(claimed_registry_sha) or not hmac.compare_digest(
        claimed_registry_sha, computed_registry_sha
    ):
        return result("mtvclc_runtime_release_registry_hash_invalid")
    signature_error = _verify_signature(
        payload=registry,
        signature_field=REGISTRY_SIGNATURE_FIELD,
        public_key=release_public_key,
    )
    if signature_error:
        return result(f"mtvclc_runtime_release_registry_{signature_error}")
    registry_sha = claimed_registry_sha
    if registry.get("schema_version") != MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA:
        return result("mtvclc_runtime_release_registry_schema_invalid")
    if registry.get("signing_key_purpose") != MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE:
        return result("mtvclc_runtime_release_registry_key_purpose_invalid")
    registry_generation_id = str(
        registry.get("registry_generation_id") or ""
    )
    if registry_generation_id != expectation.generation_id:
        return result("mtvclc_runtime_release_registry_generation_id_mismatch")
    revision = _strict_int(registry.get("registry_revision"), minimum=1)
    registry_issued_at = _finite(registry.get("issued_at_epoch"))
    registry_expires_at = _finite(registry.get("expires_at_epoch"))
    if revision is None:
        return result("mtvclc_runtime_release_registry_revision_invalid")
    registry_revision = revision
    previous_registry_sha = str(
        registry.get("previous_registry_sha256") or ""
    ).lower()
    if not _is_sha256(previous_registry_sha) or (
        revision == 1 and previous_registry_sha != "0" * 64
    ):
        return result("mtvclc_runtime_release_registry_previous_sha256_invalid")
    if (
        registry_issued_at is None
        or registry_expires_at is None
        or registry_issued_at <= 0.0
        or registry_expires_at <= registry_issued_at
        or registry_expires_at - registry_issued_at
        > MAXIMUM_REGISTRY_VALIDITY_SECONDS
    ):
        return result("mtvclc_runtime_release_registry_time_window_invalid")
    if registry_issued_at > now + 5.0:
        return result("mtvclc_runtime_release_registry_future_dated")
    if registry_expires_at <= now:
        return result("mtvclc_runtime_release_registry_expired")
    if registry_expires_at > release_expires_at:
        return result("mtvclc_runtime_release_registry_expiry_exceeds_release")
    revoked = registry.get("revoked_certificate_sha256s")
    if (
        not isinstance(revoked, list)
        or any(not _is_sha256(item) for item in revoked)
        or revoked != sorted(revoked)
        or len(revoked) != len(set(revoked))
    ):
        return result("mtvclc_runtime_release_registry_revoked_scope_invalid")
    if registry.get("active_certificate_sha256") != release_cert_sha:
        return result("mtvclc_runtime_release_registry_active_certificate_mismatch")
    if release_cert_sha in revoked:
        return result("mtvclc_runtime_release_certificate_revoked")
    if registry_anchor is not None:
        if revision < registry_anchor.registry_revision:
            return result("mtvclc_runtime_release_registry_rollback_detected")
        if revision == registry_anchor.registry_revision:
            if not hmac.compare_digest(registry_sha, registry_anchor.registry_sha256):
                return result("mtvclc_runtime_release_registry_revision_fork")
        elif revision != registry_anchor.registry_revision + 1:
            return result("mtvclc_runtime_release_registry_revision_gap")
        elif not hmac.compare_digest(
            previous_registry_sha, registry_anchor.registry_sha256
        ):
            return result("mtvclc_runtime_release_registry_chain_mismatch")
    revocation_verified = True

    authenticated_evidence_binding = certificate.get("evidence_binding")
    if not isinstance(authenticated_evidence_binding, Mapping):
        return result("mtvclc_runtime_release_evidence_binding_invalid")
    authenticated_evaluator_sha = str(
        authenticated_evidence_binding.get("evaluator_source_sha256") or ""
    ).lower()
    if not _is_sha256(authenticated_evaluator_sha):
        return result("mtvclc_runtime_release_evaluator_sha256_invalid")
    if certificate.get("evaluator_source_sha256") != authenticated_evaluator_sha:
        return result("mtvclc_runtime_release_evaluator_binding_mismatch")
    if expectation.evaluator_source_sha256 and not hmac.compare_digest(
        expectation.evaluator_source_sha256, authenticated_evaluator_sha
    ):
        return result("mtvclc_runtime_release_evaluator_expectation_mismatch")

    raw_evidence_bundle = bundle.get("evidence_bundle")
    if not isinstance(raw_evidence_bundle, Mapping):
        return result("mtvclc_runtime_release_evidence_bundle_missing")
    evidence_verification = evidence_v3.verify_mtvclc_validation_evidence(
        bundle=raw_evidence_bundle,
        public_key=evidence_public_key,
        expectation=expectation.evidence_expectation(
            evaluator_source_sha256=authenticated_evaluator_sha
        ),
        now_epoch=now,
    )
    if not evidence_verification.valid or not evidence_verification.authenticated:
        return result(
            "mtvclc_runtime_release_evidence_invalid:"
            f"{evidence_verification.reason or 'unauthenticated'}"
        )
    authenticated_evidence_bundle_sha = str(
        raw_evidence_bundle.get(evidence_v3.BUNDLE_SHA256_FIELD) or ""
    ).lower()
    if hmac.compare_digest(release_key_id, evidence_verification.signing_key_id):
        return result("mtvclc_runtime_release_signing_keys_not_distinct")
    if evidence_verification.signing_key_id != evidence_key_id:
        return result("mtvclc_runtime_release_evidence_signing_key_mismatch")
    expected_evidence_result = {
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_source_sha256": authenticated_evaluator_sha,
        "venue_id": evidence_v3.IG_MT4_VENUE_ID,
        "account_mode": evidence_v3.MTVCLC_ACCOUNT_MODE,
        "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
        "symbol_scope": evidence_v3.IG_MT4_SCALP_SYMBOLS,
    }
    for field_name, expected in expected_evidence_result.items():
        if getattr(evidence_verification, field_name) != expected:
            return result(
                f"mtvclc_runtime_release_evidence_{field_name}_mismatch"
            )
    if release_expires_at > evidence_verification.expires_at_epoch:
        return result("mtvclc_runtime_release_expiry_exceeds_evidence")
    raw_evidence_certificate = raw_evidence_bundle.get("certificate")
    if not isinstance(raw_evidence_certificate, Mapping):
        return result("mtvclc_runtime_release_evidence_certificate_missing")
    raw_evidence = raw_evidence_certificate.get("evidence")
    if not isinstance(raw_evidence, Mapping):
        return result("mtvclc_runtime_release_evidence_missing")
    if (
        raw_evidence_certificate.get("authority") != evidence_v3.NO_RUNTIME_AUTHORITY
        or raw_evidence.get("authority") != evidence_v3.NO_RUNTIME_AUTHORITY
    ):
        return result("mtvclc_runtime_release_evidence_authority_invalid")
    authenticated_preregistration_binding = certificate.get(
        "validated_preregistration_binding"
    )
    if not isinstance(authenticated_preregistration_binding, Mapping):
        return result(
            "mtvclc_runtime_release_validated_preregistration_binding_invalid"
        )
    raw_preregistration = authenticated_preregistration_binding.get(
        "preregistration"
    )
    if not isinstance(raw_preregistration, Mapping):
        return result("mtvclc_runtime_release_preregistration_missing")
    preregistration_error = _validated_preregistration_error(
        preregistration=raw_preregistration,
        binding=authenticated_preregistration_binding,
        evidence=raw_evidence,
        certificate=certificate,
        expectation=expectation,
    )
    if preregistration_error:
        return result(f"mtvclc_runtime_release_{preregistration_error}")
    preregistration_body_sha = str(
        authenticated_preregistration_binding["preregistration_body_sha256"]
    )
    preregistration_artifact_sha = str(
        authenticated_preregistration_binding[
            "preregistration_artifact_sha256"
        ]
    )
    runtime_policy_binding_sha = evidence_v3.canonical_sha256(
        authenticated_preregistration_binding["runtime_policy_binding"]
    )
    sealed_engine_identity_sha = evidence_v3.canonical_sha256(
        authenticated_preregistration_binding["sealed_engine_identity"]
    )
    derived_evidence_binding = _derived_evidence_binding(
        evidence_bundle=raw_evidence_bundle,
        evidence_certificate=raw_evidence_certificate,
        verification=evidence_verification,
    )
    if certificate.get("evidence_binding") != derived_evidence_binding:
        return result("mtvclc_runtime_release_evidence_binding_mismatch")
    cost_binding = _derived_cost_binding(raw_evidence)
    if cost_binding is None:
        return result("mtvclc_runtime_release_cost_binding_invalid")
    if certificate.get("cost_binding") != cost_binding:
        return result("mtvclc_runtime_release_cost_binding_mismatch")
    qualification_surface = _derived_qualification_surface(
        raw_evidence, cost_binding=cost_binding
    )
    if qualification_surface is None:
        return result("mtvclc_runtime_release_qualification_surface_invalid")
    if certificate.get("qualification_surface") != qualification_surface:
        return result("mtvclc_runtime_release_qualification_surface_mismatch")
    return result(
        "",
        issued_at=issued_at,
        release_expires_at=release_expires_at,
        registry_expires_at=registry_expires_at,
        cost_binding=cost_binding,
        qualification_surface=qualification_surface,
    )


__all__ = [
    "BUNDLE_SHA256_FIELD",
    "CERTIFICATE_SHA256_FIELD",
    "CERTIFICATE_SIGNATURE_FIELD",
    "EXPECTED_DEPLOYMENT",
    "EXPECTED_IMMEDIATE_TRADE_CONTRACT",
    "EXPECTED_V5_ABANDONED_PREREGISTRATIONS",
    "EXPECTED_V5_REPLACEMENT_LINEAGE",
    "LEGACY_V2_PUBLIC_EVIDENCE_ACCEPTED",
    "MAXIMUM_ACCOUNT_CURRENCY_RISK_PER_TRADE",
    "MAXIMUM_ENTRIES_PER_SYMBOL_UTC_DAY",
    "MTVCLC_IG_DEMO_DEPLOYMENT_SCHEMA",
    "MTVCLC_IMMEDIATE_TRADE_CONTRACT_SCHEMA",
    "MTVCLC_QUALIFICATION_SURFACE_SCHEMA",
    "MTVCLC_RUNTIME_COST_MAPPING_SCHEMA",
    "MTVCLC_RUNTIME_COST_MAPPING_VERSION",
    "MTVCLC_RUNTIME_POLICY_BINDING_SCHEMA",
    "MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE",
    "MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA",
    "MTVCLC_RUNTIME_RELEASE_CERTIFICATE_SCHEMA",
    "MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE",
    "MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA",
    "MTVCLC_V5_PREREGISTRATION_TOOL_REVISION",
    "MTVCLC_VALIDATED_PREREGISTRATION_BINDING_SCHEMA",
    "MTVCLCRuntimeReleaseExpectation",
    "MTVCLCRuntimeReleaseRegistryAnchor",
    "MTVCLCRuntimeReleaseVerification",
    "REGISTRY_SHA256_FIELD",
    "REGISTRY_SIGNATURE_FIELD",
    "PRODUCTION_MTVCLC_CONFIG_SHA256",
    "PUBLIC_EVIDENCE_FAMILY_ATTEMPTED_CELLS",
    "RUNTIME_RELEASE_AUTHORITY",
    "release_bundle_body_sha256",
    "release_certificate_body_sha256",
    "release_registry_body_sha256",
    "verify_mtvclc_runtime_release",
]
