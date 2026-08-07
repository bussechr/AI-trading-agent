from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import replace
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from fxstack.runtime import mtvclc_runtime_release as release
from fxstack.runtime import mtvclc_validation_evidence_v3 as evidence_v3
from fxstack.strategy.mtvclc import MTVCLCCostCalibration


NOW = 1_800_000_000.0
ISSUED = NOW - 60.0
RELEASE_EXPIRES = NOW + 1_800.0
EVIDENCE_EXPIRES = NOW + 3_600.0
REGISTRY_EXPIRES = NOW + 900.0


def _sign(
    payload: dict[str, Any],
    *,
    private_key: Ed25519PrivateKey,
    hash_field: str,
    signature_field: str,
    hasher: Any,
) -> None:
    payload.pop(hash_field, None)
    payload.pop(signature_field, None)
    payload[hash_field] = hasher(payload)
    material = {key: value for key, value in payload.items() if key != signature_field}
    payload[signature_field] = base64.b64encode(
        private_key.sign(evidence_v3.canonical_json_bytes(material))
    ).decode("ascii")


def _preregistration(
    expectation: release.MTVCLCRuntimeReleaseExpectation,
) -> dict[str, Any]:
    source_sha = dict(expectation.engine_component_sha256)["strategy/mtvclc.py"]
    source_identity = {
        "filename": "mtvclc.py",
        "sha256": source_sha,
        "size_bytes": 10,
    }
    sealed_engine = release._expected_sealed_engine_identity(expectation)
    policy = {
        "schema_version": release.MTVCLC_RUNTIME_POLICY_BINDING_SCHEMA,
        "source_identity_label": "production_strategy_policy_source",
        "source_identity": source_identity,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_entrypoint": "evaluate_mtvclc",
        "ordered_symbols": list(evidence_v3.IG_MT4_SCALP_SYMBOLS),
        "ordered_cell_count": 44,
        "engine_identity_sha256": evidence_v3.canonical_sha256(sealed_engine),
        "entry_type": "immediate_market",
        "immediate_market_trade": True,
        "buy_price_basis": "authenticated_ask",
        "sell_price_basis": "authenticated_bid",
        "pending_orders_forbidden": True,
        "pending_trades_forbidden": True,
        "prospective_outcome_evaluation_not_before_sealed_end": True,
        "runtime_or_broker_authority_granted": False,
    }
    preregistration = {
        "schema_version": "fxstack.scalp.mtvclc_preregistration.v1",
        "declaration_revision": (
            "fxstack.scalp.mtvclc_gap_v3_preregistration.v1"
        ),
        "capture_profile_id": "gap_v3_source_pinned",
        "collector_wire_profile": "gap_v3_source_pinned",
        "preregistration_tool_revision": (
            release.MTVCLC_V5_PREREGISTRATION_TOOL_REVISION
        ),
        "sealed_at_utc": "2026-08-04T00:00:00Z",
        "research_only": True,
        "attempt_accounting": {
            "prior_attempted_cells_lower_bound": 4_830,
            "current_attempted_cells": 44,
            "cumulative_attempted_cells_lower_bound": 4_874,
        },
        "abandoned_preregistrations": [
            dict(item) for item in release.EXPECTED_V5_ABANDONED_PREREGISTRATIONS
        ],
        "replacement_lineage": dict(release.EXPECTED_V5_REPLACEMENT_LINEAGE),
        "prospective_window": {
            "t0_utc_inclusive": "2026-08-04T01:00:00Z",
            "end_utc_exclusive": "2027-01-31T01:00:00Z",
            "consecutive_days": 180,
            "fixed_before_any_eligible_observation": True,
            "observations_before_t0_forbidden": True,
            "observations_at_or_after_end_forbidden": True,
            "interim_signal_or_outcome_evaluation_forbidden": True,
            "interim_performance_statistics_forbidden": True,
            "early_success_forbidden": True,
            "success_evaluation_not_before_utc": "2027-01-31T01:00:00Z",
            "no_optional_extension_or_restart_after_failure": True,
            "data_quality_monitoring_must_not_compute_performance": True,
        },
        "strategy": {
            "strategy_id": expectation.strategy_id,
            "strategy_version": expectation.strategy_version,
            "config_id": expectation.config_id,
            "config_sha256": expectation.config_sha256,
        },
        "scope": {
            "venue_id": evidence_v3.IG_MT4_VENUE_ID,
            "scope_version": evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
            "ordered_symbols": list(evidence_v3.IG_MT4_SCALP_SYMBOLS),
            "sides": ["BUY", "SELL"],
            "cell_order": [
                {
                    "config_id": expectation.config_id,
                    "symbol": symbol,
                    "side": side,
                }
                for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS
                for side in ("BUY", "SELL")
            ],
        },
        "execution_contract": {
            "entry_type": "immediate_market",
            "pending_orders_forbidden": True,
            "maximum_entries_per_symbol_utc_day": 1,
            "outcome_horizon_m1_bars": 30,
            "rollover_entry_blackout_utc": "[20:20:00,22:10:00)",
            "rollover_entry_blackout_half_open": True,
            "signals_inside_blackout_reserve": False,
        },
        "source_identities": {
            "production_strategy_policy_source": source_identity,
            "production_runtime_context": {
                "relationship": (
                    "active_runtime_policy_identity_context_only_no_authority"
                ),
                "active_strategy_family_context": expectation.strategy_id,
                "active_strategy_version_context": expectation.strategy_version,
                "active_policy_config_sha256_context": expectation.config_sha256,
                "engine_identity": sealed_engine,
            },
        },
        "runtime_policy_binding": policy,
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
    }
    preregistration["preregistration_body_sha256"] = (
        release._preregistration_body_sha256(preregistration)
    )
    return preregistration


def _evidence(preregistration: dict[str, Any]) -> dict[str, Any]:
    rows: dict[str, dict[str, Any]] = {}
    row_hashes: dict[str, str] = {}
    for index, symbol in enumerate(evidence_v3.IG_MT4_SCALP_SYMBOLS):
        row = {
            "p90_ig_spread_bps": 1.0 + index / 100.0,
            "commission_bps_per_round_trip": 0.2,
            "financing_bps_per_trade": 0.0,
            "fixed_adverse_execution_debit_bps": 1.0,
            "pre_conversion_geometry_cost_bps": 2.2 + index / 100.0,
            "profit_loss_currency": symbol[3:],
            "account_currency": "USD",
            "conversion_rate_of_absolute_profit_or_loss": 0.005,
            "convert_on_close_charge_fraction_for_screen": (
                0.0 if symbol[3:] == "USD" else 0.005
            ),
            "conversion_applies": symbol[3:] != "USD",
            "conversion_adjusted_break_even_win_probability": 0.75,
            "commission_status": "explicit_source_attested",
            "financing_status": "structurally_avoided_by_fixed_rollover_guard",
            "conversion_status": (
                "debit_absolute_profit_or_loss_when_account_currency_differs"
            ),
        }
        rows[symbol] = row
        row_hashes[symbol] = evidence_v3.canonical_sha256(row)
    cells = [
        {
            "config_id": evidence_v3.MTVCLC_CONFIG_ID,
            "symbol": symbol,
            "side": side,
            "win_probability_wilson_lower": 0.751 + index / 100_000.0,
            "base_break_even_probability": 0.75,
        }
        for index, (symbol, side) in enumerate(
            (pair for symbol in evidence_v3.IG_MT4_SCALP_SYMBOLS for pair in ((symbol, "BUY"), (symbol, "SELL")))
        )
    ]
    return {
        "schema_version": evidence_v3.MTVCLC_VALIDATION_EVIDENCE_SCHEMA,
        "artifacts": {
            "preregistration_body_sha256": preregistration[
                "preregistration_body_sha256"
            ],
            "preregistration_artifact_sha256": (
                release._preregistration_artifact_sha256(preregistration)
            ),
        },
        "costs": {
            "capture_bundle": {
                "capture_json": {
                    "filename": "sealed-costs.json",
                    "sha256": "2" * 64,
                    "size_bytes": 10,
                }
            },
            "cost_policy_sha256": "3" * 64,
            "cost_rows_sha256": evidence_v3.canonical_sha256(rows),
            "cost_row_sha256_by_symbol": row_hashes,
            "rows": rows,
        },
        "cells": cells,
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
    }


def _fixture() -> dict[str, Any]:
    release_private_key = Ed25519PrivateKey.generate()
    evidence_private_key = Ed25519PrivateKey.generate()
    release_public_key = release_private_key.public_key()
    evidence_public_key = evidence_private_key.public_key()
    release_key_id = evidence_v3.ed25519_public_key_id(release_public_key)
    evidence_key_id = evidence_v3.ed25519_public_key_id(evidence_public_key)

    components = (
        ("runtime/scalp_live_loop.py", "a" * 64),
        ("strategy/mtvclc.py", "b" * 64),
    )
    expectation = release.MTVCLCRuntimeReleaseExpectation(
        generation_id="generation-2026-08-03",
        config_sha256=release.PRODUCTION_MTVCLC_CONFIG_SHA256,
        evaluator_source_sha256="d" * 64,
        engine_sha256=release._engine_sha256(components),
        engine_component_sha256=components,
    )
    preregistration = _preregistration(expectation)
    evidence = _evidence(preregistration)
    evidence_sha = evidence_v3.canonical_sha256(evidence)
    evidence_certificate = {
        "schema_version": evidence_v3.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA,
        "generation_id": expectation.generation_id,
        "signing_key_id": evidence_key_id,
        "expires_at_epoch": EVIDENCE_EXPIRES,
        "authority": dict(evidence_v3.NO_RUNTIME_AUTHORITY),
        "evidence": evidence,
        "evidence_sha256": evidence_sha,
        evidence_v3.CERTIFICATE_SHA256_FIELD: "e" * 64,
    }
    evidence_bundle = {
        "schema_version": evidence_v3.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "certificate": evidence_certificate,
    }
    evidence_bundle[evidence_v3.BUNDLE_SHA256_FIELD] = (
        evidence_v3.bundle_body_sha256(evidence_bundle)
    )
    evidence_verification = evidence_v3.MTVCLCValidationVerification(
        valid=True,
        reason="",
        errors=(),
        authenticated=True,
        certificate_sha256="e" * 64,
        evidence_sha256=evidence_sha,
        signing_key_id=evidence_key_id,
        generation_id=expectation.generation_id,
        strategy_id=expectation.strategy_id,
        strategy_version=expectation.strategy_version,
        config_id=expectation.config_id,
        config_sha256=expectation.config_sha256,
        evaluator_source_sha256=expectation.evaluator_source_sha256,
        venue_id=evidence_v3.IG_MT4_VENUE_ID,
        account_mode=evidence_v3.MTVCLC_ACCOUNT_MODE,
        scope_version=evidence_v3.IG_MT4_SCALP_SCOPE_VERSION,
        symbol_scope=evidence_v3.IG_MT4_SCALP_SYMBOLS,
        issued_at_epoch=ISSUED - 60.0,
        expires_at_epoch=EVIDENCE_EXPIRES,
    )
    evidence_binding = release._derived_evidence_binding(
        evidence_bundle=evidence_bundle,
        evidence_certificate=evidence_certificate,
        verification=evidence_verification,
    )
    cost_binding = release._derived_cost_binding(evidence)
    assert cost_binding is not None
    qualification_surface = release._derived_qualification_surface(
        evidence, cost_binding=cost_binding
    )
    assert qualification_surface is not None
    certificate = {
        "schema_version": release.MTVCLC_RUNTIME_RELEASE_CERTIFICATE_SCHEMA,
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "config_id": expectation.config_id,
        "config_sha256": expectation.config_sha256,
        "evaluator_source_sha256": expectation.evaluator_source_sha256,
        "engine_binding": release._expected_engine_binding(expectation),
        "deployment": deepcopy(release.EXPECTED_DEPLOYMENT),
        "execution_contract": deepcopy(release.EXPECTED_IMMEDIATE_TRADE_CONTRACT),
        "validated_preregistration_binding": (
            release._derived_validated_preregistration_binding(preregistration)
        ),
        "evidence_binding": evidence_binding,
        "cost_binding": cost_binding,
        "qualification_surface": qualification_surface,
        "authority_purpose": release.MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE,
        "authority": deepcopy(release.RUNTIME_RELEASE_AUTHORITY),
        "issued_at_epoch": ISSUED,
        "expires_at_epoch": RELEASE_EXPIRES,
        "signing_key_purpose": release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE,
        "signing_key_id": release_key_id,
    }
    _sign(
        certificate,
        private_key=release_private_key,
        hash_field=release.CERTIFICATE_SHA256_FIELD,
        signature_field=release.CERTIFICATE_SIGNATURE_FIELD,
        hasher=release.release_certificate_body_sha256,
    )
    registry = {
        "schema_version": release.MTVCLC_RUNTIME_RELEASE_REVOCATIONS_SCHEMA,
        "registry_generation_id": expectation.generation_id,
        "registry_revision": 1,
        "previous_registry_sha256": "0" * 64,
        "issued_at_epoch": ISSUED,
        "expires_at_epoch": REGISTRY_EXPIRES,
        "active_certificate_sha256": certificate[
            release.CERTIFICATE_SHA256_FIELD
        ],
        "revoked_certificate_sha256s": [],
        "signing_key_purpose": release.MTVCLC_RUNTIME_RELEASE_KEY_PURPOSE,
        "signing_key_id": release_key_id,
    }
    _sign(
        registry,
        private_key=release_private_key,
        hash_field=release.REGISTRY_SHA256_FIELD,
        signature_field=release.REGISTRY_SIGNATURE_FIELD,
        hasher=release.release_registry_body_sha256,
    )
    outer_bundle = {
        "schema_version": release.MTVCLC_RUNTIME_RELEASE_BUNDLE_SCHEMA,
        "evidence_bundle": evidence_bundle,
        "certificate": certificate,
        "revocation_registry": registry,
    }
    outer_bundle[release.BUNDLE_SHA256_FIELD] = release.release_bundle_body_sha256(
        outer_bundle
    )
    return {
        "bundle": outer_bundle,
        "expectation": expectation,
        "release_private_key": release_private_key,
        "release_public_key": release_public_key,
        "evidence_public_key": evidence_public_key,
        "evidence_verification": evidence_verification,
    }


def _install_evidence_verifier(monkeypatch: Any, case: dict[str, Any]) -> list[str]:
    calls: list[str] = []

    def verify(**kwargs: Any) -> evidence_v3.MTVCLCValidationVerification:
        calls.append(kwargs["expectation"].evaluator_source_sha256)
        key_id = evidence_v3.ed25519_public_key_id(kwargs["public_key"])
        original = case["evidence_verification"]
        return evidence_v3.MTVCLCValidationVerification(
            **{
                **{
                    field: getattr(original, field)
                    for field in original.__dataclass_fields__
                },
                "signing_key_id": key_id,
            }
        )

    monkeypatch.setattr(
        release.evidence_v3, "verify_mtvclc_validation_evidence", verify
    )
    return calls


def _resign_outer(case: dict[str, Any], *, certificate_changed: bool = False) -> None:
    bundle = case["bundle"]
    certificate = bundle["certificate"]
    registry = bundle["revocation_registry"]
    private_key = case["release_private_key"]
    if certificate_changed:
        _sign(
            certificate,
            private_key=private_key,
            hash_field=release.CERTIFICATE_SHA256_FIELD,
            signature_field=release.CERTIFICATE_SIGNATURE_FIELD,
            hasher=release.release_certificate_body_sha256,
        )
        registry["active_certificate_sha256"] = certificate[
            release.CERTIFICATE_SHA256_FIELD
        ]
    _sign(
        registry,
        private_key=private_key,
        hash_field=release.REGISTRY_SHA256_FIELD,
        signature_field=release.REGISTRY_SIGNATURE_FIELD,
        hasher=release.release_registry_body_sha256,
    )
    bundle[release.BUNDLE_SHA256_FIELD] = release.release_bundle_body_sha256(bundle)


def _replace_preregistration(
    case: dict[str, Any],
    preregistration: dict[str, Any],
    *,
    update_evidence_hashes: bool = True,
) -> None:
    preregistration.pop("preregistration_body_sha256", None)
    preregistration["preregistration_body_sha256"] = (
        release._preregistration_body_sha256(preregistration)
    )
    bundle = case["bundle"]
    evidence_bundle = bundle["evidence_bundle"]
    evidence_certificate = evidence_bundle["certificate"]
    evidence = evidence_certificate["evidence"]
    if update_evidence_hashes:
        evidence["artifacts"]["preregistration_body_sha256"] = (
            preregistration["preregistration_body_sha256"]
        )
        evidence["artifacts"]["preregistration_artifact_sha256"] = (
            release._preregistration_artifact_sha256(preregistration)
        )
    evidence_bundle[evidence_v3.BUNDLE_SHA256_FIELD] = (
        evidence_v3.bundle_body_sha256(evidence_bundle)
    )
    certificate = bundle["certificate"]
    certificate["validated_preregistration_binding"] = (
        release._derived_validated_preregistration_binding(preregistration)
    )
    certificate["evidence_binding"] = release._derived_evidence_binding(
        evidence_bundle=evidence_bundle,
        evidence_certificate=evidence_certificate,
        verification=case["evidence_verification"],
    )
    _resign_outer(case, certificate_changed=True)


def _verify(case: dict[str, Any], **changes: Any) -> release.MTVCLCRuntimeReleaseVerification:
    arguments = {
        "bundle": case["bundle"],
        "release_public_key": case["release_public_key"],
        "evidence_public_key": case["evidence_public_key"],
        "expectation": case["expectation"],
        "now_epoch": NOW,
    }
    arguments.update(changes)
    return release.verify_mtvclc_runtime_release(**arguments)


def test_valid_release_exposes_exact_admission_and_runtime_calibrations(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)

    verified = _verify(case)

    assert verified.valid
    assert verified.authenticated
    assert verified.revocation_verified
    assert verified.admission_mode == "signed_validation"
    assert len(calls) == 1
    assert verified.expires_at_epoch == REGISTRY_EXPIRES
    assert verified.release_expires_at_epoch == RELEASE_EXPIRES
    assert verified.evidence_expires_at_epoch == EVIDENCE_EXPIRES
    assert verified.symbol_scope == evidence_v3.IG_MT4_SCALP_SYMBOLS
    assert verified.max_entries_per_symbol_utc_day == 1
    assert verified.maximum_account_currency_risk_per_trade == 1.0
    assert len(verified.cost_calibrations) == 22
    eurusd_cost = MTVCLCCostCalibration(**verified.cost_calibrations["EURUSD"])
    assert eurusd_cost.source_sha256 == verified.evidence_cost_row_sha256["EURUSD"]
    assert eurusd_cost.row_sha256() == verified.cost_calibration_row_sha256["EURUSD"]
    assert len(verified.win_probability_lower_bounds) == 22
    assert sum(len(sides) for sides in verified.win_probability_lower_bounds.values()) == 44
    assert verified.authority == release.RUNTIME_RELEASE_AUTHORITY
    assert verified.authority_purpose == (
        release.MTVCLC_RUNTIME_RELEASE_AUTHORITY_PURPOSE
    )
    assert len(verified.qualification_surface_sha256) == 64
    assert len(verified.cost_mapping_sha256) == 64
    assert len(verified.execution_contract_sha256) == 64
    assert len(verified.preregistration_body_sha256) == 64
    assert len(verified.preregistration_artifact_sha256) == 64
    assert len(verified.runtime_policy_binding_sha256) == 64
    assert len(verified.sealed_engine_identity_sha256) == 64
    assert verified.registry_generation_id == case["expectation"].generation_id
    assert not verified.authority["individual_trade_authorized"]
    assert not verified.authority["broker_trade_authorized"]
    full_payload = verified.to_dict()
    compact_payload = verified.to_dict(include_qualification_surfaces=False)
    minimal_payload = verified.to_dict(
        include_qualification_surfaces=False,
        include_cost_calibrations=False,
    )
    assert full_payload["qualification_surface"]["surface_sha256"] == (
        verified.qualification_surface_sha256
    )
    assert "qualification_surface" not in compact_payload
    assert "win_probability_lower_bounds" not in compact_payload
    assert compact_payload["qualification_surface_sha256"] == (
        verified.qualification_surface_sha256
    )
    assert len(compact_payload["cost_calibrations"]) == 22
    assert "cost_calibrations" not in minimal_payload
    assert minimal_payload["cost_calibration_id"] == verified.cost_calibration_id


def test_revoked_outer_certificate_is_rejected_before_embedded_v3(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    certificate_sha = case["bundle"]["certificate"][
        release.CERTIFICATE_SHA256_FIELD
    ]
    case["bundle"]["revocation_registry"]["revoked_certificate_sha256s"] = [
        certificate_sha
    ]
    _resign_outer(case)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_certificate_revoked"
    assert verified.authenticated
    assert not verified.revocation_verified
    assert calls == []


def test_release_and_evidence_keys_must_be_distinct(monkeypatch: Any) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)

    verified = _verify(
        case,
        evidence_public_key=case["release_public_key"],
    )

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_signing_keys_not_distinct"
    assert len(calls) == 1


def test_exact_cost_mapping_tamper_fails_after_both_signatures(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    case["bundle"]["certificate"]["cost_binding"]["calibrations"][0][
        "p90_spread_bps"
    ] += 0.1
    _resign_outer(case, certificate_changed=True)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_cost_binding_mismatch"
    assert verified.authenticated
    assert verified.revocation_verified
    assert len(calls) == 1


def test_exact_44_cell_surface_tamper_is_rejected(monkeypatch: Any) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    case["bundle"]["certificate"]["qualification_surface"]["cells"][43][
        "win_probability_wilson_lower"
    ] += 0.001
    _resign_outer(case, certificate_changed=True)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == (
        "mtvclc_runtime_release_qualification_surface_mismatch"
    )
    assert verified.authenticated
    assert verified.revocation_verified
    assert len(calls) == 1


def test_release_expiry_cannot_outlive_evidence(monkeypatch: Any) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    case["bundle"]["certificate"]["expires_at_epoch"] = EVIDENCE_EXPIRES + 1.0
    _resign_outer(case, certificate_changed=True)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_expiry_exceeds_evidence"
    assert len(calls) == 1


def test_durable_anchor_rejects_registry_rollback_before_embedded_v3(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    anchor = release.MTVCLCRuntimeReleaseRegistryAnchor(
        generation_id=case["expectation"].generation_id,
        registry_revision=2,
        registry_sha256="f" * 64,
    )

    verified = _verify(case, registry_anchor=anchor)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_registry_rollback_detected"
    assert calls == []


def test_next_registry_revision_must_chain_to_durable_anchor(monkeypatch: Any) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    anchor_sha = "f" * 64
    registry = case["bundle"]["revocation_registry"]
    registry["registry_revision"] = 2
    registry["previous_registry_sha256"] = anchor_sha
    _resign_outer(case)
    anchor = release.MTVCLCRuntimeReleaseRegistryAnchor(
        generation_id=case["expectation"].generation_id,
        registry_revision=1,
        registry_sha256=anchor_sha,
    )

    verified = _verify(case, registry_anchor=anchor)

    assert verified.valid
    assert verified.registry_revision == 2
    assert verified.previous_registry_sha256 == anchor_sha


def test_runtime_needs_no_local_evaluator_hash_setting(monkeypatch: Any) -> None:
    case = _fixture()
    case["expectation"] = replace(
        case["expectation"], evaluator_source_sha256=""
    )
    calls = _install_evidence_verifier(monkeypatch, case)

    verified = _verify(case)

    assert verified.valid
    assert verified.evaluator_source_sha256 == "d" * 64
    assert calls == ["d" * 64]


def test_optional_local_evaluator_pin_is_enforced_after_revocation(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    case["expectation"] = replace(
        case["expectation"], evaluator_source_sha256="f" * 64
    )
    calls = _install_evidence_verifier(monkeypatch, case)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_evaluator_expectation_mismatch"
    assert verified.authenticated
    assert verified.revocation_verified
    assert calls == []


def test_registry_expiry_cannot_outlive_release(monkeypatch: Any) -> None:
    case = _fixture()
    calls = _install_evidence_verifier(monkeypatch, case)
    case["bundle"]["revocation_registry"]["expires_at_epoch"] = (
        RELEASE_EXPIRES + 1.0
    )
    _resign_outer(case)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == (
        "mtvclc_runtime_release_registry_expiry_exceeds_release"
    )
    assert calls == []


def test_immediate_trade_contract_is_not_pending_or_individual_authority() -> None:
    assert release.EXPECTED_IMMEDIATE_TRADE_CONTRACT["trade_instruction"] == (
        "immediate_market_trade"
    )
    assert release.EXPECTED_IMMEDIATE_TRADE_CONTRACT["buy_entry_price_side"] == "ask"
    assert release.EXPECTED_IMMEDIATE_TRADE_CONTRACT["sell_entry_price_side"] == "bid"
    assert release.EXPECTED_IMMEDIATE_TRADE_CONTRACT["pending_orders_forbidden"]
    assert release.EXPECTED_IMMEDIATE_TRADE_CONTRACT["pending_trades_forbidden"]
    assert not release.RUNTIME_RELEASE_AUTHORITY["individual_trade_authorized"]
    assert not release.RUNTIME_RELEASE_AUTHORITY["broker_access_authorized"]


def test_outer_v1_is_rejected_explicitly(monkeypatch: Any) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    case["bundle"]["schema_version"] = (
        "fxstack.scalp.mtvclc_runtime_release_bundle.v1"
    )
    case["bundle"][release.BUNDLE_SHA256_FIELD] = (
        release.release_bundle_body_sha256(case["bundle"])
    )

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_bundle_schema_invalid"


def test_signed_binding_tamper_is_rejected(monkeypatch: Any) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    certificate = case["bundle"]["certificate"]
    certificate["validated_preregistration_binding"]["runtime_policy_binding"][
        "pending_trades_forbidden"
    ] = False
    case["bundle"][release.BUNDLE_SHA256_FIELD] = (
        release.release_bundle_body_sha256(case["bundle"])
    )

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == "mtvclc_runtime_release_certificate_hash_invalid"


@pytest.mark.parametrize(
    ("artifact_field", "reason"),
    (
        (
            "preregistration_body_sha256",
            "mtvclc_runtime_release_validated_preregistration_"
            "evidence_body_sha256_mismatch",
        ),
        (
            "preregistration_artifact_sha256",
            "mtvclc_runtime_release_validated_preregistration_"
            "evidence_artifact_sha256_mismatch",
        ),
    ),
)
def test_signed_evidence_preregistration_hash_mismatch_is_rejected(
    monkeypatch: Any, artifact_field: str, reason: str
) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    preregistration = deepcopy(
        case["bundle"]["certificate"]["validated_preregistration_binding"][
            "preregistration"
        ]
    )
    evidence = case["bundle"]["evidence_bundle"]["certificate"]["evidence"]
    evidence["artifacts"][artifact_field] = "f" * 64
    _replace_preregistration(
        case, preregistration, update_evidence_hashes=False
    )

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == reason
    assert verified.preregistration_body_sha256 == ""
    assert verified.preregistration_artifact_sha256 == ""
    assert verified.runtime_policy_binding_sha256 == ""
    assert verified.sealed_engine_identity_sha256 == ""


def test_wrong_whole_engine_with_recomputed_digests_is_rejected(
    monkeypatch: Any,
) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    preregistration = deepcopy(
        case["bundle"]["certificate"]["validated_preregistration_binding"][
            "preregistration"
        ]
    )
    sealed_engine = preregistration["source_identities"][
        "production_runtime_context"
    ]["engine_identity"]
    sealed_engine["component_sha256"][0][1] = "f" * 64
    sealed_engine["engine_sha256"] = release._engine_sha256(
        tuple(tuple(row) for row in sealed_engine["component_sha256"])
    )
    preregistration["runtime_policy_binding"]["engine_identity_sha256"] = (
        evidence_v3.canonical_sha256(sealed_engine)
    )
    _replace_preregistration(case, preregistration)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == (
        "mtvclc_runtime_release_validated_preregistration_"
        "engine_identity_mismatch"
    )


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        (
            "accounting",
            "mtvclc_runtime_release_validated_preregistration_"
            "attempt_accounting_invalid",
        ),
        (
            "window",
            "mtvclc_runtime_release_validated_preregistration_window_invalid",
        ),
        (
            "abandoned_lineage",
            "mtvclc_runtime_release_validated_preregistration_"
            "abandoned_lineage_invalid",
        ),
        (
            "replacement_lineage",
            "mtvclc_runtime_release_validated_preregistration_"
            "replacement_lineage_invalid",
        ),
    ),
)
def test_rehashed_successor_history_drift_is_rejected(
    monkeypatch: Any, mutation: str, reason: str
) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    preregistration = deepcopy(
        case["bundle"]["certificate"]["validated_preregistration_binding"][
            "preregistration"
        ]
    )
    if mutation == "accounting":
        preregistration["attempt_accounting"][
            "cumulative_attempted_cells_lower_bound"
        ] = 4_830
    elif mutation == "window":
        preregistration["prospective_window"]["consecutive_days"] = 179
    elif mutation == "abandoned_lineage":
        preregistration["abandoned_preregistrations"][-1]["reason"] = (
            "invented_timeout"
        )
    else:
        preregistration["replacement_lineage"][
            "old_window_restart_or_extension"
        ] = True
    _replace_preregistration(case, preregistration)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == reason


@pytest.mark.parametrize(
    ("mutation", "reason"),
    (
        (
            "config",
            "mtvclc_runtime_release_validated_preregistration_"
            "strategy_config_mismatch",
        ),
        (
            "symbols",
            "mtvclc_runtime_release_validated_preregistration_"
            "scope_deployment_mismatch",
        ),
        (
            "policy_source",
            "mtvclc_runtime_release_validated_preregistration_"
            "policy_source_mismatch",
        ),
        (
            "immediate",
            "mtvclc_runtime_release_validated_preregistration_"
            "immediate_execution_mismatch",
        ),
        (
            "pending",
            "mtvclc_runtime_release_validated_preregistration_"
            "immediate_execution_mismatch",
        ),
        (
            "pending_trades",
            "mtvclc_runtime_release_validated_preregistration_"
            "immediate_execution_mismatch",
        ),
        (
            "sell_basis",
            "mtvclc_runtime_release_validated_preregistration_"
            "immediate_execution_mismatch",
        ),
        (
            "policy_authority",
            "mtvclc_runtime_release_validated_preregistration_"
            "immediate_execution_mismatch",
        ),
        (
            "authority",
            "mtvclc_runtime_release_validated_preregistration_envelope_invalid",
        ),
    ),
)
def test_rehashed_runtime_policy_drift_is_rejected(
    monkeypatch: Any, mutation: str, reason: str
) -> None:
    case = _fixture()
    _install_evidence_verifier(monkeypatch, case)
    preregistration = deepcopy(
        case["bundle"]["certificate"]["validated_preregistration_binding"][
            "preregistration"
        ]
    )
    policy = preregistration["runtime_policy_binding"]
    if mutation == "config":
        policy["config_sha256"] = "f" * 64
    elif mutation == "symbols":
        policy["ordered_symbols"] = policy["ordered_symbols"][:-1]
    elif mutation == "policy_source":
        policy["source_identity"]["sha256"] = "f" * 64
    elif mutation == "immediate":
        policy["buy_price_basis"] = "authenticated_bid"
    elif mutation == "pending":
        policy["pending_orders_forbidden"] = False
    elif mutation == "pending_trades":
        policy["pending_trades_forbidden"] = False
    elif mutation == "sell_basis":
        policy["sell_price_basis"] = "authenticated_ask"
    elif mutation == "policy_authority":
        policy["runtime_or_broker_authority_granted"] = True
    else:
        preregistration["authority"]["broker_trade_authorized"] = True
    _replace_preregistration(case, preregistration)

    verified = _verify(case)

    assert not verified.valid
    assert verified.reason == reason
