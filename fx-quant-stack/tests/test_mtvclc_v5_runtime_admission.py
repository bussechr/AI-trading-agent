from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fxstack.runtime import mtvclc_runtime_release as runtime_release
from fxstack.runtime import mtvclc_validation_evidence_v2 as evidence_v2
from fxstack.runtime import mtvclc_validation_evidence_v3 as evidence_v3
from fxstack.runtime.mtvclc_entry_qualification import (
    MTVCLC_SIGNED_PROBABILITY_SOURCE,
)
from fxstack.runtime.scalp_engine_identity import (
    SCALP_ENGINE_COMPONENTS,
    SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS,
    production_scalp_engine_identity,
)


def _expectation() -> evidence_v3.MTVCLCValidationExpectation:
    return evidence_v3.MTVCLCValidationExpectation(
        generation_id="v5-successor-test-generation",
        strategy_id=evidence_v3.MTVCLC_STRATEGY_ID,
        strategy_version=evidence_v3.MTVCLC_STRATEGY_VERSION,
        config_id=evidence_v3.MTVCLC_CONFIG_ID,
        config_sha256="a" * 64,
        evaluator_source_sha256="b" * 64,
    )


def test_production_runtime_release_is_v3_only_and_4874_bound() -> None:
    assert runtime_release.evidence_v3 is evidence_v3
    assert runtime_release.PUBLIC_EVIDENCE_FAMILY_ATTEMPTED_CELLS == 4_874
    assert runtime_release.LEGACY_V2_PUBLIC_EVIDENCE_ACCEPTED is False
    assert evidence_v3.MTVCLC_VALIDATION_EVIDENCE_SCHEMA == (
        "fxstack.scalp.mtvclc_validation_evidence.v3"
    )
    assert evidence_v3.MTVCLC_VALIDATION_CERTIFICATE_SCHEMA == (
        "fxstack.scalp.mtvclc_validation_certificate.v3"
    )
    assert evidence_v3.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA == (
        "fxstack.scalp.mtvclc_signed_evidence_bundle.v3"
    )
    assert evidence_v3.WILSON_FAMILY_ATTEMPTED_CELLS == 4_874
    assert evidence_v3.EXPECTED_ATTEMPT_ACCOUNTING == {
        "prior_attempted_cells_lower_bound": 4_830,
        "current_attempted_cells": 44,
        "cumulative_attempted_cells_lower_bound": 4_874,
    }


def test_public_v3_verifier_explicitly_rejects_legacy_v2_bundle_schema() -> None:
    public_key = Ed25519PrivateKey.generate().public_key()
    legacy_bundle = {
        "schema_version": evidence_v2.MTVCLC_SIGNED_EVIDENCE_BUNDLE_SCHEMA,
        "certificate": {},
        evidence_v3.BUNDLE_SHA256_FIELD: "c" * 64,
    }

    result = evidence_v3.verify_mtvclc_validation_evidence(
        bundle=legacy_bundle,
        public_key=public_key,
        expectation=_expectation(),
        now_epoch=2_000_000_000.0,
    )

    assert result.valid is False
    assert result.authenticated is False
    assert result.reason == "mtvclc_bundle_schema_invalid"


def test_v5_engine_identity_contains_adapter_and_pinned_v2_template() -> None:
    required = {
        "runtime/mtvclc_validation_evidence_v2.py",
        "runtime/mtvclc_validation_evidence_v3.py",
        "runtime/mtvclc_runtime_release.py",
        "runtime/mtvclc_entry_qualification.py",
        "runtime/live_launch_authority_preflight.py",
    }
    assert required.issubset(SCALP_ENGINE_COMPONENTS)
    assert {
        "runtime/mtvclc_validation_evidence_v2.py",
        "runtime/mtvclc_validation_evidence_v3.py",
    }.issubset(SCALP_ENGINE_REQUIRED_SECURITY_COMPONENTS)

    identity = production_scalp_engine_identity()
    component_names = {name for name, _digest in identity.component_sha256}
    assert required.issubset(component_names)


def test_entry_seam_labels_the_authenticated_v3_4874_probability() -> None:
    assert MTVCLC_SIGNED_PROBABILITY_SOURCE == (
        "mtvclc_signed_evidence_v3_wilson_lower_4874"
    )
    assert runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT[
        "trade_instruction"
    ] == "immediate_market_trade"
    assert runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT[
        "execution_type"
    ] == "market"
    assert runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT[
        "pending_orders_forbidden"
    ] is True
    assert runtime_release.EXPECTED_IMMEDIATE_TRADE_CONTRACT[
        "pending_trades_forbidden"
    ] is True
