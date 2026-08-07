from __future__ import annotations

import base64
import copy
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.runtime import scalp_validation_evidence as verifier_module
from fxstack.runtime.scalp_validation_evidence import (
    CERTIFICATE_SHA256_FIELD,
    CERTIFICATE_SIGNATURE_FIELD,
    MAX_CERTIFICATE_VALIDITY_SECS,
    REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS,
    REVOCATION_SHA256_FIELD,
    REVOCATION_SIGNATURE_FIELD,
    SCALP_VALIDATION_CERTIFICATE_SCHEMA,
    SCALP_VALIDATION_EVIDENCE_SCHEMA,
    SCALP_VALIDATION_REVOCATION_SCHEMA,
    ScalpValidationExpectation,
    WIN_PROBABILITY_CI_METHOD,
    canonical_sha256,
    certificate_body_sha256,
    conservative_win_probability_interval,
    ed25519_public_key_id,
    revocation_body_sha256,
    verify_scalp_validation_evidence,
)
from fxstack.strategy.scalp_dislocation import SCALP_DISLOCATION_STRATEGY_VERSION


NOW = 1_800_000_000.0


def _canonical_bytes(payload: dict) -> bytes:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _expectation() -> ScalpValidationExpectation:
    return ScalpValidationExpectation(
        generation_id="scalp-validation-generation-7",
        strategy_id="scalp_dislocation",
        strategy_version=SCALP_DISLOCATION_STRATEGY_VERSION,
        engine_sha256="a" * 64,
        config_sha256="b" * 64,
    )


def _cell(*, trades: int = 30, days: int = 12, wins: int = 27) -> dict:
    lower, upper = conservative_win_probability_interval(
        wins=wins,
        trades=trades,
    )
    return {
        "trades": trades,
        "independent_days": days,
        "wins": wins,
        "win_probability": wins / trades,
        "win_probability_ci_lower": lower,
        "win_probability_ci_upper": upper,
        "win_probability_ci_method": WIN_PROBABILITY_CI_METHOD,
    }


def _evidence() -> dict:
    cells = {
        symbol: {"BUY": _cell(), "SELL": _cell()} for symbol in IG_MT4_SCALP_SYMBOLS
    }
    total_trades = sum(
        side["trades"]
        for symbol_cells in cells.values()
        for side in symbol_cells.values()
    )
    return {
        "schema_version": SCALP_VALIDATION_EVIDENCE_SCHEMA,
        "source_errors": [],
        "recorded_errors": [],
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": 1,
        "artifact_sha256": {
            field: f"{index:x}" * 64
            for index, field in enumerate(
                REQUIRED_EVIDENCE_ARTIFACT_SHA256_FIELDS,
                start=1,
            )
        },
        "overall": {
            "cost_stressed_expectancy": 0.12,
            "cost_stressed_ci_lower": 0.04,
            "cost_stressed_ci_upper": 0.20,
            "mcpt_p_value": 0.03,
            "pbo": 0.25,
            "dsr": 0.97,
            "trades": total_trades,
            "independent_days": 120,
            "max_drawdown_pct": 14.0,
        },
        "two_x_cost_stress": {
            "cost_multiplier": 2.0,
            "expectancy": 0.06,
            "ci_lower": 0.01,
            "ci_upper": 0.11,
        },
        "cells": cells,
    }


def _sign_certificate(
    certificate: dict,
    signing_key: Ed25519PrivateKey,
) -> None:
    certificate.pop(CERTIFICATE_SIGNATURE_FIELD, None)
    certificate.pop(CERTIFICATE_SHA256_FIELD, None)
    certificate["signing_key_id"] = ed25519_public_key_id(signing_key.public_key())
    certificate["evidence_sha256"] = canonical_sha256(certificate["evidence"])
    certificate[CERTIFICATE_SHA256_FIELD] = certificate_body_sha256(certificate)
    signature = signing_key.sign(_canonical_bytes(certificate))
    certificate[CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(signature).decode(
        "ascii"
    )


def _sign_registry(registry: dict, signing_key: Ed25519PrivateKey) -> None:
    registry.pop(REVOCATION_SIGNATURE_FIELD, None)
    registry.pop(REVOCATION_SHA256_FIELD, None)
    registry["signing_key_id"] = ed25519_public_key_id(signing_key.public_key())
    registry[REVOCATION_SHA256_FIELD] = revocation_body_sha256(registry)
    signature = signing_key.sign(_canonical_bytes(registry))
    registry[REVOCATION_SIGNATURE_FIELD] = base64.b64encode(signature).decode("ascii")


def _bundle(
    *,
    evidence: dict | None = None,
    certificate_updates: dict | None = None,
    revoked: list[str] | None = None,
    active: bool = True,
) -> tuple[
    ScalpValidationExpectation,
    Ed25519PrivateKey,
    dict,
    dict,
]:
    expectation = _expectation()
    signing_key = Ed25519PrivateKey.generate()
    certificate = {
        "schema_version": SCALP_VALIDATION_CERTIFICATE_SCHEMA,
        "generation_id": expectation.generation_id,
        "strategy_id": expectation.strategy_id,
        "strategy_version": expectation.strategy_version,
        "engine_sha256": expectation.engine_sha256,
        "config_sha256": expectation.config_sha256,
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": 1,
        "issued_at_epoch": NOW - 60.0,
        "expires_at_epoch": NOW + 86_400.0,
        "evidence": copy.deepcopy(_evidence() if evidence is None else evidence),
    }
    certificate.update(dict(certificate_updates or {}))
    _sign_certificate(certificate, signing_key)
    certificate_sha = certificate[CERTIFICATE_SHA256_FIELD]
    registry = {
        "schema_version": SCALP_VALIDATION_REVOCATION_SCHEMA,
        "registry_revision": 1,
        "updated_at_epoch": NOW - 30.0,
        "active_certificate_sha256": certificate_sha if active else "",
        "revoked_certificate_sha256s": list(revoked or []),
    }
    _sign_registry(registry, signing_key)
    return expectation, signing_key, certificate, registry


def _verify(bundle):
    expectation, signing_key, certificate, registry = bundle
    return verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=registry,
        public_key=signing_key.public_key(),
        expectation=expectation,
        now_epoch=NOW,
    )


def test_valid_certificate_returns_hashes_and_authenticated_cell_bounds() -> None:
    expectation, signing_key, certificate, registry = _bundle()

    result = verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=registry,
        public_key=signing_key.public_key(),
        expectation=expectation,
        now_epoch=NOW,
    )

    assert result.valid is True
    assert result.reason == ""
    assert result.authenticated is True
    assert result.revocation_verified is True
    assert result.certificate_sha256 == certificate[CERTIFICATE_SHA256_FIELD]
    assert result.evidence_sha256 == certificate["evidence_sha256"]
    assert result.generation_id == expectation.generation_id
    assert result.symbol_scope == IG_MT4_SCALP_SYMBOLS
    assert set(result.win_probability_lower_bounds) == set(IG_MT4_SCALP_SYMBOLS)
    assert all(
        set(sides) == {"BUY", "SELL"}
        for sides in result.win_probability_lower_bounds.values()
    )
    lower = result.win_probability_lower_bounds["EURUSD"]["BUY"]
    point = certificate["evidence"]["cells"]["EURUSD"]["BUY"]["win_probability"]
    assert 0.0 < lower < point < 1.0
    assert result.win_probability_bounds_reason == ""
    assert result.to_dict()["win_probability_lower_bounds"]["NZDJPY"]["SELL"] > 0


def test_runtime_verifier_has_no_research_or_private_key_dependency() -> None:
    source = Path(verifier_module.__file__).read_text(encoding="utf-8")
    assert "fxstack.scalp" not in source
    assert "Ed25519PrivateKey" not in source
    assert "def sign_" not in source


def test_certificate_body_tamper_fails_before_bounds_are_exposed() -> None:
    bundle = _bundle()
    bundle[2]["evidence"]["overall"]["dsr"] = 0.10

    result = _verify(bundle)

    assert result.valid is False
    assert result.reason == "validation_certificate_body_hash_invalid"
    assert result.authenticated is False
    assert result.win_probability_lower_bounds == {}
    assert result.win_probability_bounds_reason == result.reason


def test_wrong_operator_key_and_signature_forgery_fail_closed() -> None:
    expectation, _, certificate, registry = _bundle()
    wrong_key = Ed25519PrivateKey.generate().public_key()
    wrong_key_result = verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=registry,
        public_key=wrong_key,
        expectation=expectation,
        now_epoch=NOW,
    )
    assert wrong_key_result.reason == "validation_certificate_signing_key_mismatch"

    forged_bundle = _bundle()
    forged_bundle[2][CERTIFICATE_SIGNATURE_FIELD] = base64.b64encode(
        b"\x00" * 64
    ).decode("ascii")
    forged_result = _verify(forged_bundle)
    assert forged_result.reason == "validation_certificate_signature_invalid"
    assert forged_result.authenticated is False


def test_signed_revocation_registry_is_mandatory_and_tamper_evident() -> None:
    bundle = _bundle()
    bundle[3]["registry_revision"] = 2

    result = _verify(bundle)

    assert result.valid is False
    assert result.authenticated is True
    assert result.reason == "validation_revocation_registry_body_hash_invalid"

    forged_bundle = _bundle()
    forged_bundle[3][REVOCATION_SIGNATURE_FIELD] = base64.b64encode(
        b"\x00" * 64
    ).decode("ascii")
    forged_result = _verify(forged_bundle)
    assert forged_result.reason == "validation_revocation_registry_signature_invalid"


def test_authenticated_revocation_blocks_certificate_identity() -> None:
    expectation, signing_key, certificate, _ = _bundle()
    registry = {
        "schema_version": SCALP_VALIDATION_REVOCATION_SCHEMA,
        "registry_revision": 2,
        "updated_at_epoch": NOW,
        "active_certificate_sha256": "",
        "revoked_certificate_sha256s": [certificate[CERTIFICATE_SHA256_FIELD]],
    }
    _sign_registry(registry, signing_key)

    result = verify_scalp_validation_evidence(
        certificate=certificate,
        revocation_registry=registry,
        public_key=signing_key.public_key(),
        expectation=expectation,
        now_epoch=NOW,
    )

    assert result.valid is False
    assert result.revocation_verified is True
    assert result.reason == "validation_certificate_revoked"


def test_ordered_twenty_two_symbol_scope_is_exact() -> None:
    result = _verify(
        _bundle(
            certificate_updates={
                "symbol_scope": list(reversed(IG_MT4_SCALP_SYMBOLS)),
            }
        )
    )

    assert result.valid is False
    assert result.reason == "validation_certificate_symbol_scope_invalid"


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {"generation_id": "other-generation"},
            "validation_certificate_generation_id_mismatch",
        ),
        ({"strategy_id": "other"}, "validation_certificate_strategy_id_mismatch"),
        (
            {"strategy_version": "other.v2"},
            "validation_certificate_strategy_version_mismatch",
        ),
        ({"engine_sha256": "c" * 64}, "validation_certificate_engine_sha256_mismatch"),
        ({"config_sha256": "d" * 64}, "validation_certificate_config_sha256_mismatch"),
        ({"venue_id": "other"}, "validation_certificate_venue_invalid"),
        (
            {"max_entries_per_symbol_utc_day": 2},
            "validation_certificate_daily_frequency_invalid",
        ),
    ],
)
def test_signed_identity_or_policy_drift_is_rejected(updates, reason: str) -> None:
    result = _verify(_bundle(certificate_updates=updates))

    assert result.valid is False
    assert result.reason == reason


def test_certificate_validity_cannot_exceed_seven_days() -> None:
    result = _verify(
        _bundle(
            certificate_updates={
                "issued_at_epoch": NOW - 60.0,
                "expires_at_epoch": NOW - 60.0 + MAX_CERTIFICATE_VALIDITY_SECS + 1.0,
            }
        )
    )

    assert result.valid is False
    assert result.reason == "validation_certificate_time_window_invalid"


@pytest.mark.parametrize(
    ("section", "field", "value", "reason"),
    [
        (
            "overall",
            "cost_stressed_ci_lower",
            0.0,
            "validation_evidence_cost_stressed_expectancy_invalid",
        ),
        ("overall", "mcpt_p_value", 0.0501, "validation_evidence_mcpt_failed"),
        ("overall", "pbo", 0.4001, "validation_evidence_pbo_failed"),
        ("overall", "dsr", 0.9499, "validation_evidence_dsr_failed"),
        (
            "overall",
            "trades",
            299,
            "validation_evidence_trade_sample_insufficient",
        ),
        (
            "overall",
            "independent_days",
            59,
            "validation_evidence_day_sample_insufficient",
        ),
        (
            "overall",
            "max_drawdown_pct",
            25.01,
            "validation_evidence_max_drawdown_failed",
        ),
        (
            "two_x_cost_stress",
            "expectancy",
            0.0,
            "validation_evidence_two_x_cost_stress_failed",
        ),
    ],
)
def test_weak_signed_statistics_fail_recomputed_gates(
    section: str,
    field: str,
    value: float,
    reason: str,
) -> None:
    evidence = _evidence()
    evidence[section][field] = value

    result = _verify(_bundle(evidence=evidence))

    assert result.valid is False
    assert result.authenticated is True
    assert result.reason == reason
    assert result.win_probability_lower_bounds == {}


def test_every_symbol_requires_both_buy_and_sell_cells() -> None:
    evidence = _evidence()
    evidence["cells"]["BTCUSD"].pop("SELL")

    result = _verify(_bundle(evidence=evidence))

    assert result.valid is False
    assert result.reason == "validation_evidence_cell_side_missing:BTCUSD"


def test_cell_sample_floors_are_recomputed() -> None:
    evidence = _evidence()
    evidence["cells"]["EURUSD"]["BUY"] = _cell(
        trades=29,
        days=10,
        wins=26,
    )

    result = _verify(_bundle(evidence=evidence))

    assert result.valid is False
    assert result.reason == "validation_evidence_cell_sample_invalid:EURUSD:BUY"


def test_point_estimate_cannot_masquerade_as_probability_lower_bound() -> None:
    evidence = _evidence()
    cell = evidence["cells"]["GBPJPY"]["SELL"]
    cell["win_probability_ci_lower"] = cell["win_probability"]

    result = _verify(_bundle(evidence=evidence))

    assert result.valid is False
    assert result.reason == "validation_evidence_cell_win_ci_invalid:GBPJPY:SELL"
    assert result.win_probability_lower_bounds == {}
    assert result.win_probability_bounds_reason == result.reason


def test_source_errors_and_artifact_identities_fail_closed() -> None:
    source_error_evidence = _evidence()
    source_error_evidence["source_errors"] = ["missing_cost_rows"]
    source_result = _verify(_bundle(evidence=source_error_evidence))
    assert source_result.reason == "validation_evidence_source_errors_present"

    artifact_error_evidence = _evidence()
    artifact_error_evidence["artifact_sha256"]["trade_ledger"] = "not-a-sha"
    artifact_result = _verify(_bundle(evidence=artifact_error_evidence))
    assert artifact_result.reason == "validation_evidence_artifact_sha256_invalid"


def test_malformed_evidence_fails_closed_with_named_reason() -> None:
    result = _verify(_bundle(evidence={}))

    assert result.valid is False
    assert result.authenticated is True
    assert result.reason == "validation_evidence_malformed"
    assert result.win_probability_bounds_reason == result.reason
