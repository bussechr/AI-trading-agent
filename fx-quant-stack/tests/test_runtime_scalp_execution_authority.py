from __future__ import annotations

import copy

import pytest

from fxstack.runtime.scalp_execution_authority import (
    BROKER_NATIVE_BRACKET_POLICY,
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    SCALP_ENTRY_INTENT,
    SCALP_EXECUTION_AUTHORITY_SCHEMA,
    SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
    SCALP_EXECUTION_LANE,
    ScalpAuthorityExpectation,
    authority_binding_sha256,
    authority_error,
    build_active_authority,
    command_binding_error,
    command_binding_fields,
    expectation_from_command,
    protective_authority_structure_error,
    protective_history_binding_error,
    symbol_scope_error,
    validation_witness_error,
)
from fxstack.runtime.scalp_validation_evidence import (
    SCALP_ADMISSION_MODE_DIRECT_DEMO,
)
from fxstack.scalp.config import CONFIGURED_SYMBOLS, ScalpConfig
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


def _expectation(**overrides) -> ScalpAuthorityExpectation:
    values = {
        "generation_id": "scalp-generation-7",
        "strategy_id": MTVCLC_STRATEGY_ID,
        "strategy_version": MTVCLC_STRATEGY_VERSION,
        "engine_sha256": "1" * 64,
        "config_id": MTVCLC_CONFIG_ID,
        "config_sha256": MTVCLC_CONFIG_SHA256,
        "runtime_release_certificate_sha256": "2" * 64,
        "runtime_release_signing_key_id": "3" * 64,
        "research_evidence_sha256": "4" * 64,
        "research_evidence_signing_key_id": "5" * 64,
        "registry_generation_id": "scalp-generation-7",
        "registry_revision": 9,
        "registry_sha256": "6" * 64,
        "qualification_surface_sha256": "7" * 64,
        "cost_mapping_sha256": "8" * 64,
        "execution_contract_sha256": "9" * 64,
        "validation_expires_at_epoch": 2_000_000_000.0,
        "runtime_boot_id": "boot-9",
        "authority_revision": 4,
    }
    values.update(overrides)
    return ScalpAuthorityExpectation(**values)


def test_production_universe_is_the_exact_research_default() -> None:
    assert len(IG_MT4_SCALP_SYMBOLS) == 22
    assert len(set(IG_MT4_SCALP_SYMBOLS)) == 22
    assert CONFIGURED_SYMBOLS == IG_MT4_SCALP_SYMBOLS
    assert tuple(ScalpConfig().symbols) == IG_MT4_SCALP_SYMBOLS


@pytest.mark.parametrize(
    ("scope", "reason"),
    [
        ((), "scalp_authority_symbol_scope_missing"),
        (IG_MT4_SCALP_SYMBOLS + ("EURUSD",), "scalp_authority_symbol_scope_duplicate"),
        (IG_MT4_SCALP_SYMBOLS[:-1], "scalp_authority_symbol_scope_incomplete"),
        (IG_MT4_SCALP_SYMBOLS[:-1] + ("XAUUSD",), "scalp_authority_symbol_scope_incomplete"),
    ],
)
def test_full_scope_is_mandatory(scope, reason) -> None:
    assert symbol_scope_error(scope) == reason


def test_active_authority_binds_every_execution_semantic() -> None:
    expected = _expectation()
    authority = build_active_authority(expected, activated_at=1_800_000_000.0)

    assert authority_error(authority, expectation=expected) == ""
    assert authority["schema_version"] == SCALP_EXECUTION_AUTHORITY_SCHEMA
    assert authority["source"] == "production_runtime"
    assert authority["venue_id"] == IG_MT4_VENUE_ID
    assert authority["symbol_scope"] == list(IG_MT4_SCALP_SYMBOLS)
    assert authority["bracket_policy"] == BROKER_NATIVE_BRACKET_POLICY
    assert (
        authority["max_entries_per_symbol_utc_day"]
        == MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    )
    assert len(authority["binding_sha256"]) == 64


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("status", "revoked", "scalp_authority_inactive"),
        ("source", "research", "scalp_authority_source_invalid"),
        ("generation_id", "other", "scalp_authority_generation_changed"),
        ("strategy_id", "other", "scalp_authority_strategy_changed"),
        ("engine_sha256", "3" * 64, "scalp_authority_engine_changed"),
        ("config_sha256", "4" * 64, "scalp_authority_config_changed"),
        (
            "runtime_release_certificate_sha256",
            "5" * 64,
            "scalp_authority_runtime_release_certificate_changed",
        ),
        (
            "runtime_release_signing_key_id",
            "6" * 64,
            "scalp_authority_runtime_release_key_changed",
        ),
        (
            "research_evidence_sha256",
            "7" * 64,
            "scalp_authority_research_evidence_changed",
        ),
        (
            "research_evidence_signing_key_id",
            "8" * 64,
            "scalp_authority_research_evidence_key_changed",
        ),
        (
            "registry_sha256",
            "a" * 64,
            "scalp_authority_registry_changed",
        ),
        (
            "registry_revision",
            10,
            "scalp_authority_registry_revision_changed",
        ),
        (
            "qualification_surface_sha256",
            "b" * 64,
            "scalp_authority_qualification_surface_changed",
        ),
        (
            "cost_mapping_sha256",
            "c" * 64,
            "scalp_authority_cost_mapping_changed",
        ),
        (
            "execution_contract_sha256",
            "d" * 64,
            "scalp_authority_execution_contract_changed",
        ),
        (
            "validation_expires_at_epoch",
            1_900_000_000.0,
            "scalp_authority_validation_expiry_changed",
        ),
        ("venue_id", "other", "scalp_authority_venue_changed"),
        ("bracket_policy", "naked", "scalp_authority_bracket_policy_changed"),
        ("max_entries_per_symbol_utc_day", 2, "scalp_authority_daily_frequency_changed"),
        ("runtime_boot_id", "other", "scalp_authority_runtime_boot_changed"),
        ("authority_revision", 5, "scalp_authority_revision_changed"),
        ("binding_sha256", "f" * 64, "scalp_authority_binding_invalid"),
    ],
)
def test_authority_drift_fails_closed(field, value, reason) -> None:
    expected = _expectation()
    authority = build_active_authority(expected, activated_at=1_800_000_000.0)
    authority[field] = value
    assert authority_error(authority, expectation=expected) == reason


def test_command_carries_recheckable_authority_identity() -> None:
    authority = build_active_authority(
        _expectation(), activated_at=1_800_000_000.0
    )
    payload = {
        "cmd": "BUY",
        "symbol": "BTCUSD",
        **command_binding_fields(authority),
    }

    assert payload["strategy_lane"] == SCALP_EXECUTION_LANE
    assert payload["intent"] == SCALP_ENTRY_INTENT
    assert command_binding_error(payload, authority=authority, symbol="BTCUSD") == ""
    assert expectation_from_command(payload) == _expectation()

    tampered = copy.deepcopy(payload)
    tampered["expected_strategy_config_sha256"] = "9" * 64
    assert (
        command_binding_error(tampered, authority=authority, symbol="BTCUSD")
        == "expected_strategy_config_sha256_changed"
    )


def test_real_account_uses_the_same_signed_authority_and_exact_binding_path() -> None:
    expected = _expectation(account_mode="real")
    authority = build_active_authority(expected, activated_at=1_800_000_000.0)
    payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        **command_binding_fields(authority),
    }

    assert authority_error(authority, expectation=expected) == ""
    assert authority["account_mode"] == "real"
    assert payload["expected_strategy_account_mode"] == "real"
    assert command_binding_error(
        payload,
        authority=authority,
        symbol="EURUSD",
    ) == ""

    cross_mode = copy.deepcopy(payload)
    cross_mode["expected_strategy_account_mode"] = "demo"
    assert command_binding_error(
        cross_mode,
        authority=authority,
        symbol="EURUSD",
    ) == "scalp_command_account_mode_invalid"


def test_invalid_expectation_never_builds_authority() -> None:
    with pytest.raises(ValueError, match="scalp_authority_engine_identity_invalid"):
        build_active_authority(
            _expectation(engine_sha256="not-a-sha"),
            activated_at=1_800_000_000.0,
        )


def _validation_witness(authority: dict) -> dict:
    return {
        "valid": True,
        "reason": "",
        "errors": [],
        "authenticated": True,
        "revocation_verified": True,
        "admission_mode": "signed_validation",
        "account_mode": "demo",
        "certificate_sha256": authority[
            "runtime_release_certificate_sha256"
        ],
        "runtime_release_certificate_sha256": authority[
            "runtime_release_certificate_sha256"
        ],
        "signing_key_id": authority["runtime_release_signing_key_id"],
        "runtime_release_signing_key_id": authority[
            "runtime_release_signing_key_id"
        ],
        "evidence_sha256": authority["research_evidence_sha256"],
        "evidence_signing_key_id": authority[
            "research_evidence_signing_key_id"
        ],
        "generation_id": authority["generation_id"],
        "strategy_id": authority["strategy_id"],
        "strategy_version": authority["strategy_version"],
        "engine_sha256": authority["engine_sha256"],
        "config_id": authority["config_id"],
        "config_sha256": authority["config_sha256"],
        "registry_generation_id": authority["registry_generation_id"],
        "registry_revision": authority["registry_revision"],
        "registry_sha256": authority["registry_sha256"],
        "qualification_surface_sha256": authority[
            "qualification_surface_sha256"
        ],
        "cost_mapping_sha256": authority["cost_mapping_sha256"],
        "execution_contract_sha256": authority["execution_contract_sha256"],
        "venue_id": authority["venue_id"],
        "scope_version": authority["scope_version"],
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": 1,
        "expires_at_epoch": authority["validation_expires_at_epoch"],
        "win_probability_lower_bounds": {
            symbol: {"BUY": 0.60, "SELL": 0.60}
            for symbol in IG_MT4_SCALP_SYMBOLS
        },
    }


def test_active_authority_requires_complete_authenticated_validation_witness() -> None:
    authority = build_active_authority(
        _expectation(), activated_at=1_800_000_000.0
    )
    witness = _validation_witness(authority)

    assert (
        validation_witness_error(
            witness,
            authority=authority,
            now_epoch=1_850_000_000.0,
        )
        == ""
    )
    assert (
        validation_witness_error(
            None,
            authority=authority,
            now_epoch=1_850_000_000.0,
        )
        == "scalp_validation_witness_missing"
    )
    missing_cell = copy.deepcopy(witness)
    missing_cell["win_probability_lower_bounds"].pop("NZDJPY")
    assert (
        validation_witness_error(
            missing_cell,
            authority=authority,
            now_epoch=1_850_000_000.0,
        )
        == "scalp_validation_witness_probability_scope_invalid"
    )
    assert (
        validation_witness_error(
            witness,
            authority=authority,
            now_epoch=2_000_000_000.0,
        )
        == "scalp_validation_witness_expired"
    )


def test_direct_demo_cannot_build_active_authority_but_legacy_owner_remains_closable() -> None:
    expected = _expectation(
        admission_mode=SCALP_ADMISSION_MODE_DIRECT_DEMO,
        account_mode="demo",
        generation_id="direct-demo-v1",
        validation_evidence_sha256="7" * 64,
        validation_expires_at_epoch=4_102_444_800.0,
    )
    with pytest.raises(
        ValueError,
        match="scalp_authority_admission_mode_invalid",
    ):
        build_active_authority(expected, activated_at=1_800_000_000.0)

    authority = {
        "schema_version": SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
        "status": "revoked",
        "source": "production_runtime",
        "admission_mode": SCALP_ADMISSION_MODE_DIRECT_DEMO,
        "account_mode": "demo",
        "generation_id": "direct-demo-v1",
        "strategy_id": "legacy-scalp-strategy",
        "engine_sha256": "1" * 64,
        "config_sha256": "2" * 64,
        "validation_evidence_sha256": "7" * 64,
        "validation_expires_at_epoch": 4_102_444_800.0,
        "venue_id": IG_MT4_VENUE_ID,
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "bracket_policy": BROKER_NATIVE_BRACKET_POLICY,
        "max_entries_per_symbol_utc_day": 1,
        "runtime_boot_id": "legacy-boot",
        "authority_revision": 3,
    }
    authority["binding_sha256"] = authority_binding_sha256(authority)
    witness = {
        "admission_mode": SCALP_ADMISSION_MODE_DIRECT_DEMO,
        "valid": True,
        "reason": "",
        "errors": [],
        "authenticated": False,
        "revocation_verified": False,
        "certificate_sha256": "7" * 64,
        "evidence_sha256": "7" * 64,
        "signing_key_id": "",
        "generation_id": "direct-demo-v1",
        "strategy_id": authority["strategy_id"],
        "engine_sha256": authority["engine_sha256"],
        "config_sha256": authority["config_sha256"],
        "venue_id": authority["venue_id"],
        "symbol_scope": list(IG_MT4_SCALP_SYMBOLS),
        "max_entries_per_symbol_utc_day": 1,
        "expires_at_epoch": 4_102_444_800.0,
        "win_probability_lower_bounds": {},
    }

    assert protective_authority_structure_error(authority) == ""
    assert validation_witness_error(
        witness,
        authority=authority,
        now_epoch=1_850_000_000.0,
    ) == "scalp_validation_witness_authority_schema_invalid"

    binding_fields = {
        "expected_strategy_authority_schema": authority["schema_version"],
        "expected_strategy_admission_mode": authority["admission_mode"],
        "expected_strategy_account_mode": authority["account_mode"],
        "expected_strategy_generation_id": authority["generation_id"],
        "expected_strategy_id": authority["strategy_id"],
        "expected_strategy_engine_sha256": authority["engine_sha256"],
        "expected_strategy_config_sha256": authority["config_sha256"],
        "expected_strategy_validation_evidence_sha256": authority[
            "validation_evidence_sha256"
        ],
        "expected_strategy_validation_expires_at_epoch": authority[
            "validation_expires_at_epoch"
        ],
        "expected_strategy_venue_id": authority["venue_id"],
        "expected_strategy_binding_sha256": authority["binding_sha256"],
        "expected_strategy_runtime_boot_id": authority["runtime_boot_id"],
        "expected_strategy_authority_revision": authority[
            "authority_revision"
        ],
        "strategy_lane": SCALP_EXECUTION_LANE,
        "intent": SCALP_ENTRY_INTENT,
    }
    entry_payload = {
        "cmd": "BUY",
        "symbol": "EURUSD",
        **binding_fields,
    }
    close_payload = {
        "cmd": "CLOSE",
        "symbol": "EURUSD",
        "management_strategy": authority["strategy_id"],
        **binding_fields,
    }
    assert protective_history_binding_error(
        entry_payload,
        close_payload=close_payload,
        symbol="EURUSD",
    ) == ""
    assert (
        command_binding_error(
            entry_payload,
            authority=authority,
            symbol="EURUSD",
        )
        == "scalp_command_authority_schema_invalid"
    )
