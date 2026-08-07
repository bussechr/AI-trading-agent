from __future__ import annotations

from unittest.mock import patch

from fxstack.api.protocol_identity import BRIDGE_PROTOCOL_VERSION
from fxstack.runtime import market_source_identity
from fxstack.runtime.market_source_identity import (
    build_authenticated_market_source,
    current_authenticated_market_source,
    market_source_row_error,
)


def _source():
    source = build_authenticated_market_source(
        broker_account_scope="ig-demo-account",
        broker_venue_id="ig_mt4",
        producer_identity="ig-mt4-production-ea",
        producer_instance_id="mt4-terminal-instance-a",
        terminal_lease_scope="ig-mt4-terminal-scope",
        credential_generation_id="generation-1",
        bridge_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert source is not None
    return source


def test_source_id_binds_account_venue_producer_instance_terminal_generation_and_protocol() -> None:
    source = _source()
    fields = source.to_fields()

    assert not market_source_row_error(fields, expected=source)
    for key, replacement in (
        ("broker_account_scope", "other-account"),
        ("broker_venue_id", "other-venue"),
        ("producer_identity", "other-producer"),
        ("producer_instance_id", "mt4-terminal-instance-b"),
        ("terminal_lease_scope", "other-terminal"),
        ("credential_generation_id", "generation-2"),
        ("bridge_protocol_version", "v0.0.0"),
    ):
        tampered = {**fields, key: replacement}
        assert market_source_row_error(tampered, expected=source)


def test_current_source_requires_matching_active_singleton_lease_and_protocol() -> None:
    source = _source()
    state = {
        "broker_account_scope": source.broker_account_scope,
        "broker_venue_id": source.broker_venue_id,
        "bridge_producer_identity": source.producer_identity,
        "bridge_producer_instance_id": source.producer_instance_id,
        "bridge_terminal_lease_scope": source.terminal_lease_scope,
        "bridge_credential_generation_id": source.credential_generation_id,
        "bridge_protocol_version": source.bridge_protocol_version,
        "bridge_consumer_lease": {
            "consumer_identity": source.producer_identity,
            "producer_instance_id": source.producer_instance_id,
            "terminal_lease_scope": source.terminal_lease_scope,
            "credential_generation_id": source.credential_generation_id,
            "bridge_protocol_version": source.bridge_protocol_version,
            "expires_at": 101.0,
        },
    }

    current, error = current_authenticated_market_source(
        state,
        now_epoch=100.0,
        expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert error == ""
    assert current == source

    expired, expired_error = current_authenticated_market_source(
        state,
        now_epoch=102.0,
        expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert expired is None
    assert expired_error == "market_source_consumer_lease_expired"

    clone_lease_state = {
        **state,
        "bridge_consumer_lease": {
            **state["bridge_consumer_lease"],
            "producer_instance_id": "mt4-terminal-instance-b",
        },
    }
    clone, clone_error = current_authenticated_market_source(
        clone_lease_state,
        now_epoch=100.0,
        expected_protocol_version=BRIDGE_PROTOCOL_VERSION,
    )
    assert clone is None
    assert clone_error == "market_source_consumer_lease_identity_mismatch"

    stale_protocol, protocol_error = current_authenticated_market_source(
        state,
        now_epoch=100.0,
        expected_protocol_version="v9.9.9",
    )
    assert stale_protocol is None
    assert protocol_error == "market_source_protocol_version_mismatch"


def test_repeated_scalar_row_identity_uses_bounded_projection_cache() -> None:
    source = _source()
    fields = {**source.to_fields(), "broker_venue_id": " IG_MT4 "}
    market_source_identity._authenticated_market_source_from_projection.cache_clear()
    market_source_identity._market_source_row_error_from_projection.cache_clear()

    with patch.object(
        market_source_identity,
        "build_authenticated_market_source",
        wraps=market_source_identity.build_authenticated_market_source,
    ) as build:
        for _ in range(100):
            assert market_source_row_error(fields, expected=source) == ""
        assert build.call_count == 1

        tampered = {**fields, "producer_instance_id": "other-instance"}
        assert market_source_row_error(tampered, expected=source)
        assert build.call_count == 2


def test_unhashable_row_identity_uses_full_validation_path() -> None:
    source = _source()
    malformed = {**source.to_fields(), "producer_identity": ["invalid"]}

    assert market_source_row_error(malformed, expected=source)
