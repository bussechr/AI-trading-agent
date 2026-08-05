from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.scalp_execution_authority import (
    ScalpAuthorityExpectation,
    build_active_authority,
    command_binding_fields,
)
from fxstack.runtime.scalp_position_lifecycle import (
    evaluate_scalp_position_lifecycle,
)
from fxstack.runtime.scalp_restart_reconciliation import (
    MT4_POSITIONS_SNAPSHOT_SCHEMA,
    TICKET_OWNER_CONTRACT,
    ScalpRestartOwnedPosition,
    reconcile_scalp_restart,
)
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


NOW = 1_800_001_000.0
MAX_AGE = 30.0
EXPECTED_MAGIC = 246_810
ACCOUNT_SCOPE = "ig-mt4-demo-scope"
BOOT_ID = "scalp-restart-boot"


def _authority(*, expires_at: float = NOW + 3_600.0) -> dict[str, Any]:
    return build_active_authority(
        ScalpAuthorityExpectation(
            generation_id="defined-scalp-generation-1",
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256="1" * 64,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="2" * 64,
            runtime_release_signing_key_id="3" * 64,
            research_evidence_sha256="4" * 64,
            research_evidence_signing_key_id="5" * 64,
            registry_generation_id="defined-scalp-generation-1",
            registry_revision=11,
            registry_sha256="6" * 64,
            qualification_surface_sha256="7" * 64,
            cost_mapping_sha256="8" * 64,
            execution_contract_sha256="9" * 64,
            validation_expires_at_epoch=expires_at,
            runtime_boot_id=BOOT_ID,
            authority_revision=7,
        ),
        activated_at=NOW - 120.0,
    )


def _position(
    symbol: str = "EURUSD",
    *,
    side: str = "BUY",
    ticket: Any = 101,
    owner_token: str = "fxs-owner-101",
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "broker_symbol": f"{symbol}.IG",
        "side": side,
        "ticket": ticket,
        "lots": 0.12,
        "magic": EXPECTED_MAGIC,
        "order_comment": owner_token,
        "open_price": 1.101,
        "open_time": NOW - 1_200.0,
        "sl": 1.099,
        "tp": 1.105,
        "profit": 2.5,
    }
    row.update(overrides)
    return row


def _state(positions: list[Any], **overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "positions_snapshot_authoritative": True,
        "positions_snapshot_source": "positions_snapshot",
        "positions_snapshot_schema": MT4_POSITIONS_SNAPSHOT_SCHEMA,
        "positions_snapshot_contract_current": True,
        "positions_snapshot_account_scope": ACCOUNT_SCOPE,
        "positions_snapshot_token": "snapshot-token-1",
        "positions_snapshot_received_at": NOW - 1.0,
        "positions": positions,
    }
    state.update(overrides)
    return state


def _entry_command(
    authority: dict[str, Any],
    *,
    command_id: str,
    symbol: str,
    side: str,
    owner_token: str,
    status: str = "acked",
    ticket: int | None = None,
    ack_overrides: dict[str, Any] | None = None,
    payload_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        **command_binding_fields(authority),
        "command_id": command_id,
        "cmd": side,
        "symbol": symbol,
        "magic": EXPECTED_MAGIC,
        "owner_token": owner_token,
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "lots": 0.12,
    }
    payload.update(payload_overrides or {})
    ack: dict[str, Any] | None = None
    if ticket is not None:
        ack = {
            "command_id": command_id,
            "status": status,
            "symbol": symbol,
            "ticket": ticket,
            "magic": EXPECTED_MAGIC,
            "owner_token": owner_token,
        }
        ack.update(ack_overrides or {})
    return {
        "command_id": command_id,
        "cmd": side,
        "symbol": symbol,
        "magic": EXPECTED_MAGIC,
        "intent": payload["intent"],
        "status": status,
        "payload_json": payload,
        "ack_json": ack,
    }


def _close_command(
    authority: dict[str, Any],
    *,
    command_id: str,
    managed_entry_command_id: str,
    symbol: str,
    ticket: int,
    owner_token: str,
    status: str,
    cmd: str = "CLOSE",
    ack: bool = False,
    payload_overrides: dict[str, Any] | None = None,
    ack_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    management_bindings = {
        key: value
        for key, value in command_binding_fields(authority).items()
        if key.startswith("expected_strategy_")
    }
    payload: dict[str, Any] = {
        **management_bindings,
        "intent": "EXIT",
        "management_strategy": authority["strategy_id"],
        "command_id": command_id,
        "managed_entry_command_id": managed_entry_command_id,
        "cmd": cmd,
        "symbol": symbol,
        "magic": EXPECTED_MAGIC,
        "target_ticket": ticket,
        "owner_token": owner_token,
        "ownership_contract": TICKET_OWNER_CONTRACT,
    }
    payload.update(payload_overrides or {})
    ack_json: dict[str, Any] | None = None
    if ack:
        ack_json = {
            "command_id": command_id,
            "status": "acked",
            "symbol": symbol,
            "ticket": ticket,
            "magic": EXPECTED_MAGIC,
            "owner_token": owner_token,
        }
        ack_json.update(ack_overrides or {})
    return {
        "command_id": command_id,
        "cmd": cmd,
        "symbol": symbol,
        "magic": EXPECTED_MAGIC,
        "intent": payload["intent"],
        "status": status,
        "payload_json": payload,
        "ack_json": ack_json,
    }


def _reconcile(
    *,
    state: Any,
    commands: Any,
    authority: Any = None,
    **overrides: Any,
):
    kwargs: dict[str, Any] = {
        "state_snapshot": state,
        "durable_command_rows": commands,
        "production_scalp_authority": (
            _authority() if authority is None else authority
        ),
        "now_epoch": NOW,
        "max_snapshot_age_secs": MAX_AGE,
        "expected_magic": EXPECTED_MAGIC,
        "expected_account_scope": ACCOUNT_SCOPE,
    }
    kwargs.update(overrides)
    return reconcile_scalp_restart(**kwargs)


def test_exact_join_reports_open_pending_active_and_confirmed_sets() -> None:
    authority = _authority()
    positions = [
        _position("USDJPY", side="SELL", ticket=102, owner_token="fxs-owner-102"),
        _position("EURUSD", ticket=101, owner_token="fxs-owner-101"),
    ]
    commands = [
        _entry_command(
            authority,
            command_id="entry-usdjpy",
            symbol="USDJPY",
            side="SELL",
            owner_token="fxs-owner-102",
            ticket=102,
        ),
        _entry_command(
            authority,
            command_id="entry-eurusd",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        ),
        _entry_command(
            authority,
            command_id="pending-btcusd",
            symbol="BTCUSD",
            side="BUY",
            owner_token="fxs-pending-btcusd",
            status="queued",
        ),
        _close_command(
            authority,
            command_id="exit-eurusd-active",
            managed_entry_command_id="entry-eurusd",
            symbol="EURUSD",
            ticket=101,
            owner_token="fxs-owner-101",
            status="delivered",
        ),
        _close_command(
            authority,
            command_id="exit-usdjpy-acked",
            managed_entry_command_id="entry-usdjpy",
            symbol="USDJPY",
            ticket=102,
            owner_token="fxs-owner-102",
            status="acked",
            ack=True,
        ),
    ]

    result = _reconcile(
        state=_state(positions), commands=commands, authority=authority
    )

    assert result.entry_admission_ready is True
    assert result.quarantine_reasons == ()
    assert result.authoritative_open_symbols == ("EURUSD", "USDJPY")
    assert tuple(item.symbol for item in result.owned_positions) == (
        "EURUSD",
        "USDJPY",
    )
    assert result.owned_positions[0].owner_token == "fxs-owner-101"
    assert result.owned_positions[0].order_comment == "fxs-owner-101"
    assert result.owned_positions[0].ownership_contract == TICKET_OWNER_CONTRACT
    assert result.active_queued_entry_symbols == ("BTCUSD",)
    assert result.active_exit_tickets == (101,)
    assert result.broker_confirmed_exit_symbols == ("USDJPY",)


def test_broker_comment_suffix_preserves_exact_owner_prefix_join() -> None:
    authority = _authority()
    owner_token = "fxs-owner-prefix-101"
    position = _position(
        "EURUSD",
        ticket=101,
        owner_token=f"{owner_token}-srv",
    )
    command = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token=owner_token,
        ticket=101,
    )

    result = _reconcile(
        state=_state([position]),
        commands=[command],
        authority=authority,
    )

    assert result.entry_admission_ready is True
    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.owned_positions[0].owner_token == owner_token
    assert result.owned_positions[0].order_comment == f"{owner_token}-srv"


def test_historical_entry_and_close_survive_full_current_authority_rollover() -> None:
    historical = _authority(expires_at=NOW - 1.0)
    current = build_active_authority(
        ScalpAuthorityExpectation(
            generation_id="defined-scalp-generation-2",
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256="4" * 64,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="a" * 64,
            runtime_release_signing_key_id="b" * 64,
            research_evidence_sha256="c" * 64,
            research_evidence_signing_key_id="d" * 64,
            registry_generation_id="defined-scalp-generation-2",
            registry_revision=21,
            registry_sha256="e" * 64,
            qualification_surface_sha256="f" * 64,
            cost_mapping_sha256="a" * 64,
            execution_contract_sha256="b" * 64,
            validation_expires_at_epoch=NOW + 7_200.0,
            runtime_boot_id="scalp-restart-new-boot",
            authority_revision=19,
        ),
        activated_at=NOW - 60.0,
    )
    entry = _entry_command(
        historical,
        command_id="historical-entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    close = _close_command(
        historical,
        command_id="historical-exit-eurusd",
        managed_entry_command_id="historical-entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="queued",
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, close],
        authority=current,
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    owned = result.owned_positions[0]
    expected_historical_binding = {
        field: value
        for field, value in command_binding_fields(historical).items()
        if field.startswith("expected_strategy_")
    }
    assert dict(owned.entry_authority_binding) == expected_historical_binding
    assert owned.entry_command_id == "historical-entry-eurusd"
    assert result.active_exit_tickets == (101,)
    assert result.quarantine_reasons == ()


def test_historical_binding_tamper_and_old_queued_entry_fail_closed() -> None:
    historical = _authority()
    current = build_active_authority(
        ScalpAuthorityExpectation(
            generation_id="defined-scalp-generation-2",
            strategy_id=MTVCLC_STRATEGY_ID,
            strategy_version=MTVCLC_STRATEGY_VERSION,
            engine_sha256="4" * 64,
            config_id=MTVCLC_CONFIG_ID,
            config_sha256=MTVCLC_CONFIG_SHA256,
            runtime_release_certificate_sha256="a" * 64,
            runtime_release_signing_key_id="b" * 64,
            research_evidence_sha256="c" * 64,
            research_evidence_signing_key_id="d" * 64,
            registry_generation_id="defined-scalp-generation-2",
            registry_revision=21,
            registry_sha256="e" * 64,
            qualification_surface_sha256="f" * 64,
            cost_mapping_sha256="a" * 64,
            execution_contract_sha256="b" * 64,
            validation_expires_at_epoch=NOW + 7_200.0,
            runtime_boot_id="scalp-restart-new-boot",
            authority_revision=19,
        ),
        activated_at=NOW - 60.0,
    )
    tampered_entry = _entry_command(
        historical,
        command_id="tampered-historical-entry",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
        payload_overrides={
            "expected_strategy_runtime_boot_id": "forged-old-boot",
        },
    )
    tampered = _reconcile(
        state=_state([_position()]),
        commands=[tampered_entry],
        authority=current,
    )
    assert tampered.owned_positions == ()
    assert "scalp_protective_history_binding_invalid" in (
        tampered.quarantine_reasons
    )

    old_queued_entry = _entry_command(
        historical,
        command_id="old-boot-queued-entry",
        symbol="BTCUSD",
        side="BUY",
        owner_token="fxs-old-queued-entry",
        status="queued",
    )
    pending = _reconcile(
        state=_state([]),
        commands=[old_queued_entry],
        authority=current,
    )
    assert pending.active_queued_entry_symbols == ()
    assert "expected_strategy_generation_id_changed" in (
        pending.quarantine_reasons
    )


def test_historical_close_requires_exact_managed_entry_primary_key() -> None:
    historical = _authority()
    entry = _entry_command(
        historical,
        command_id="historical-entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    close = _close_command(
        historical,
        command_id="historical-exit-eurusd",
        managed_entry_command_id="different-entry-command",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="queued",
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, close],
        authority=_authority(),
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.active_exit_tickets == ()
    assert "exit_command_managed_entry_command_id_mismatch" in (
        result.quarantine_reasons
    )


@pytest.mark.parametrize(
    ("state_overrides", "expected_reason"),
    (
        ({"positions_snapshot_authoritative": False}, "snapshot_not_authoritative"),
        ({"positions_snapshot_source": "legacy_positions"}, "snapshot_source_invalid"),
        ({"positions_snapshot_schema": "v1"}, "snapshot_schema_invalid"),
        ({"positions_snapshot_contract_current": False}, "snapshot_contract_not_current"),
        ({"positions_snapshot_account_scope": "other"}, "snapshot_account_scope_mismatch"),
        ({"positions_snapshot_token": ""}, "snapshot_token_missing"),
        ({"positions_snapshot_received_at": NOW + 0.001}, "snapshot_received_at_future"),
        ({"positions_snapshot_received_at": NOW - MAX_AGE - 0.001}, "snapshot_stale"),
        ({"positions": "not-a-list"}, "snapshot_positions_invalid"),
    ),
)
def test_snapshot_must_be_structured_scoped_and_fresh(
    state_overrides: dict[str, Any], expected_reason: str
) -> None:
    authority = _authority()
    command = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )

    state = _state([_position()])
    state.update(state_overrides)
    result = _reconcile(
        state=state,
        commands=[command],
        authority=authority,
    )

    assert result.entry_admission_ready is False
    assert expected_reason in result.quarantine_reasons
    assert result.authoritative_open_symbols == ()
    assert result.owned_positions == ()


@pytest.mark.parametrize(
    ("position_overrides", "expected_reason"),
    (
        ({"ticket": 0}, "position_ticket_invalid"),
        ({"ticket": "101"}, "position_ticket_invalid"),
        ({"symbol": "eurusd"}, "position_symbol_invalid"),
        ({"symbol": "XAUUSD"}, "position_symbol_invalid"),
        ({"side": "buy"}, "position_side_invalid"),
        ({"side": []}, "position_side_invalid"),
        ({"lots": 0.0}, "position_lots_invalid"),
        ({"lots": "0.12"}, "position_lots_invalid"),
        ({"lots": float("nan")}, "position_lots_invalid"),
        ({"magic": EXPECTED_MAGIC + 1}, "position_magic_mismatch"),
        ({"order_comment": "bad token"}, "position_entry_command_missing"),
        ({"order_comment": "x" * 32}, "position_order_comment_invalid"),
    ),
)
def test_invalid_position_keeps_identifiable_occupancy_and_other_safe_join(
    position_overrides: dict[str, Any], expected_reason: str
) -> None:
    authority = _authority()
    invalid = _position(**position_overrides)
    valid = _position("GBPUSD", ticket=202, owner_token="fxs-owner-202")
    valid_command = _entry_command(
        authority,
        command_id="entry-gbpusd",
        symbol="GBPUSD",
        side="BUY",
        owner_token="fxs-owner-202",
        ticket=202,
    )

    result = _reconcile(
        state=_state([invalid, valid]),
        commands=[valid_command],
        authority=authority,
    )

    assert result.entry_admission_ready is False
    assert expected_reason in result.quarantine_reasons
    assert tuple(item.symbol for item in result.owned_positions) == ("GBPUSD",)
    if invalid.get("symbol") == "EURUSD":
        assert "EURUSD" in result.authoritative_open_symbols
    assert "GBPUSD" in result.authoritative_open_symbols


def test_unmatched_and_ambiguous_positions_remain_occupied_not_owned() -> None:
    authority = _authority()
    missing = _reconcile(state=_state([_position()]), commands=[])
    assert missing.authoritative_open_symbols == ("EURUSD",)
    assert missing.owned_positions == ()
    assert "position_entry_command_missing" in missing.quarantine_reasons

    commands = [
        _entry_command(
            authority,
            command_id=command_id,
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        )
        for command_id in ("entry-a", "entry-b")
    ]
    ambiguous = _reconcile(
        state=_state([_position()]), commands=commands, authority=authority
    )
    assert ambiguous.authoritative_open_symbols == ("EURUSD",)
    assert ambiguous.owned_positions == ()
    assert "position_entry_command_ambiguous" in ambiguous.quarantine_reasons


@pytest.mark.parametrize(
    ("ack_overrides", "expected_reason"),
    (
        ({"ticket": 999}, "command_ack_ticket_mismatch"),
        ({"magic": EXPECTED_MAGIC + 1}, "command_ack_magic_mismatch"),
        ({"owner_token": "fxs-other-owner"}, "command_ack_owner_token_mismatch"),
    ),
)
def test_entry_ack_identity_must_match_position(
    ack_overrides: dict[str, Any], expected_reason: str
) -> None:
    authority = _authority()
    command = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
        ack_overrides=ack_overrides,
    )

    result = _reconcile(
        state=_state([_position()]), commands=[command], authority=authority
    )

    assert result.owned_positions == ()
    assert expected_reason in result.quarantine_reasons


def test_protective_close_uses_management_binding_without_entry_lane() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    close = _close_command(
        authority,
        command_id="exit-eurusd",
        managed_entry_command_id="entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="queued",
    )
    close_payload = close["payload_json"]
    assert "strategy_lane" not in close_payload
    assert close_payload["intent"] == "EXIT"
    assert close_payload["management_strategy"] == authority["strategy_id"]

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, close],
        authority=authority,
    )

    assert result.entry_admission_ready is True
    assert result.active_exit_tickets == (101,)
    assert result.quarantine_reasons == ()


@pytest.mark.parametrize(
    ("cmd", "payload_overrides"),
    (
        ("CLOSE", {}),
        ("CLOSE_PARTIAL", {"close_lots": 0.06}),
        ("MODIFY_SL", {"new_sl": 1.1005}),
    ),
)
def test_reconcile_required_management_reserves_exact_owned_ticket(
    cmd: str,
    payload_overrides: dict[str, Any],
) -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    uncertain_management = _close_command(
        authority,
        command_id=f"uncertain-{cmd.lower()}",
        managed_entry_command_id="entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="reconcile_required",
        cmd=cmd,
        payload_overrides=payload_overrides,
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, uncertain_management],
        authority=authority,
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.active_exit_tickets == (101,)
    assert result.broker_confirmed_exit_symbols == ()
    assert "exit_command_reconcile_required" in result.unmatched_reasons
    assert "exit_command_reconcile_required" not in result.quarantine_reasons
    assert result.entry_admission_ready is True


def test_reconcile_required_management_requires_exact_owned_identity() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    wrong_owner = _close_command(
        authority,
        command_id="uncertain-close-wrong-owner",
        managed_entry_command_id="entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-other-owner",
        status="reconcile_required",
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, wrong_owner],
        authority=authority,
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.active_exit_tickets == ()
    assert "active_exit_position_identity_mismatch" in result.quarantine_reasons


def test_reconcile_required_entry_remains_owned_but_fences_entry_admission() -> None:
    authority = _authority()
    uncertain_entry = _entry_command(
        authority,
        command_id="uncertain-entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        status="reconcile_required",
        ticket=101,
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[uncertain_entry],
        authority=authority,
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.owned_positions[0].entry_command_id == "uncertain-entry-eurusd"
    assert "entry_command_reconcile_required" in result.quarantine_reasons
    assert result.entry_admission_ready is False


def test_close_that_claims_entry_lane_is_not_required_for_management() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    close = _close_command(
        authority,
        command_id="exit-eurusd",
        managed_entry_command_id="entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="queued",
        payload_overrides={"management_strategy": "wrong"},
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, close],
        authority=authority,
    )

    assert result.active_exit_tickets == ()
    assert "exit_command_management_strategy_invalid" in result.quarantine_reasons


def test_active_entries_are_unique_and_cannot_overlap_open_symbols() -> None:
    authority = _authority()
    commands = [
        _entry_command(
            authority,
            command_id="entry-open",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        ),
        _entry_command(
            authority,
            command_id="queued-open",
            symbol="EURUSD",
            side="SELL",
            owner_token="fxs-pending-eurusd",
            status="queued",
        ),
        _entry_command(
            authority,
            command_id="queued-btc-a",
            symbol="BTCUSD",
            side="BUY",
            owner_token="fxs-pending-btc-a",
            status="queued",
        ),
        _entry_command(
            authority,
            command_id="queued-btc-b",
            symbol="BTCUSD",
            side="SELL",
            owner_token="fxs-pending-btc-b",
            status="delivered",
        ),
    ]

    result = _reconcile(
        state=_state([_position()]), commands=commands, authority=authority
    )

    assert result.active_queued_entry_symbols == ("BTCUSD",)
    assert "active_entry_symbol_already_open" in result.quarantine_reasons
    assert "active_entry_symbol_duplicate" in result.quarantine_reasons
    assert result.entry_admission_ready is False


def test_invalid_active_exit_preserves_independent_owned_rows() -> None:
    authority = _authority()
    positions = [
        _position("EURUSD", ticket=101, owner_token="fxs-owner-101"),
        _position("USDJPY", ticket=102, owner_token="fxs-owner-102"),
    ]
    commands = [
        _entry_command(
            authority,
            command_id="entry-eurusd",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        ),
        _entry_command(
            authority,
            command_id="entry-usdjpy",
            symbol="USDJPY",
            side="BUY",
            owner_token="fxs-owner-102",
            ticket=102,
        ),
        _close_command(
            authority,
            command_id="valid-close",
            managed_entry_command_id="entry-eurusd",
            symbol="EURUSD",
            ticket=101,
            owner_token="fxs-owner-101",
            status="queued",
        ),
        _close_command(
            authority,
            command_id="invalid-close",
            managed_entry_command_id="entry-usdjpy",
            symbol="USDJPY",
            ticket=999,
            owner_token="fxs-not-owned",
            status="delivered",
        ),
    ]

    result = _reconcile(
        state=_state(positions), commands=commands, authority=authority
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101, 102)
    assert result.active_exit_tickets == (101,)
    assert "active_exit_target_not_joined" in result.quarantine_reasons


def test_active_exit_ack_identity_is_checked_when_present() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    close = _close_command(
        authority,
        command_id="exit-eurusd",
        managed_entry_command_id="entry-eurusd",
        symbol="EURUSD",
        ticket=101,
        owner_token="fxs-owner-101",
        status="delivered",
        ack=True,
        ack_overrides={"ticket": 999},
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, close],
        authority=authority,
    )

    assert result.active_exit_tickets == ()
    assert "command_ack_ticket_mismatch" in result.quarantine_reasons


def test_stale_exit_for_older_ticket_cannot_confirm_new_position() -> None:
    authority = _authority()
    commands = [
        _entry_command(
            authority,
            command_id="entry-new",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-new",
            ticket=901,
        ),
        _close_command(
            authority,
            command_id="exit-old",
            managed_entry_command_id="entry-old",
            symbol="EURUSD",
            ticket=900,
            owner_token="fxs-owner-old",
            status="acked",
            ack=True,
        ),
    ]

    result = _reconcile(
        state=_state([_position(ticket=901, owner_token="fxs-owner-new")]),
        commands=commands,
        authority=authority,
    )

    assert result.entry_admission_ready is True
    assert result.broker_confirmed_exit_symbols == ()
    assert result.owned_positions[0].ticket == 901


def test_same_ticket_exit_with_wrong_owner_never_counts_as_confirmed() -> None:
    authority = _authority()
    commands = [
        _entry_command(
            authority,
            command_id="entry-new",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-new",
            ticket=901,
        ),
        _close_command(
            authority,
            command_id="exit-wrong-owner",
            managed_entry_command_id="entry-new",
            symbol="EURUSD",
            ticket=901,
            owner_token="fxs-owner-old",
            status="acked",
            ack=True,
        ),
    ]

    result = _reconcile(
        state=_state([_position(ticket=901, owner_token="fxs-owner-new")]),
        commands=commands,
        authority=authority,
    )

    assert result.broker_confirmed_exit_symbols == ()
    assert "confirmed_exit_position_identity_mismatch" in result.quarantine_reasons


def test_invalid_command_row_quarantines_but_preserves_safe_join() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )

    result = _reconcile(
        state=_state([_position()]),
        commands=[entry, "not-a-command"],
        authority=authority,
    )

    assert result.owned_positions[0].ticket == 101
    assert result.entry_admission_ready is False
    assert "command_row_invalid" in result.quarantine_reasons


@pytest.mark.parametrize(
    ("duplicate", "expected_reason"),
    (
        ("ticket", "position_ticket_duplicate"),
        ("symbol", "position_symbol_duplicate"),
    ),
)
def test_duplicate_position_identity_fails_closed(
    duplicate: str, expected_reason: str
) -> None:
    first = _position("EURUSD", ticket=101, owner_token="fxs-owner-101")
    second = _position("USDJPY", ticket=102, owner_token="fxs-owner-102")
    if duplicate == "ticket":
        second["ticket"] = 101
    else:
        second["symbol"] = "EURUSD"
        second["broker_symbol"] = "EURUSD.IG"

    result = _reconcile(state=_state([first, second]), commands=[])

    assert result.owned_positions == ()
    assert expected_reason in result.quarantine_reasons


def test_position_and_command_permutations_are_identical() -> None:
    authority = _authority()
    positions = (
        _position("USDJPY", side="SELL", ticket=102, owner_token="fxs-owner-102"),
        _position("EURUSD", ticket=101, owner_token="fxs-owner-101"),
        _position("BTCUSD", ticket=103, owner_token="fxs-owner-103"),
    )
    commands = (
        _entry_command(
            authority,
            command_id="entry-usdjpy",
            symbol="USDJPY",
            side="SELL",
            owner_token="fxs-owner-102",
            ticket=102,
        ),
        _entry_command(
            authority,
            command_id="entry-eurusd",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        ),
        _entry_command(
            authority,
            command_id="entry-btcusd",
            symbol="BTCUSD",
            side="BUY",
            owner_token="fxs-owner-103",
            ticket=103,
        ),
        _entry_command(
            authority,
            command_id="pending-ethusd",
            symbol="ETHUSD",
            side="SELL",
            owner_token="fxs-pending-ethusd",
            status="queued",
        ),
    )
    expected = _reconcile(
        state=_state(list(positions)),
        commands=list(commands),
        authority=authority,
    ).to_dict()

    for position_order in permutations(positions):
        for command_order in permutations(commands):
            observed = _reconcile(
                state=_state(list(position_order)),
                commands=list(command_order),
                authority=authority,
            ).to_dict()
            assert observed == expected


def test_owned_rows_are_frozen_mappings_and_feed_lifecycle_directly() -> None:
    authority = _authority()
    entry = _entry_command(
        authority,
        command_id="entry-eurusd",
        symbol="EURUSD",
        side="BUY",
        owner_token="fxs-owner-101",
        ticket=101,
    )
    result = _reconcile(
        state=_state([_position()]), commands=[entry], authority=authority
    )
    owned = result.owned_positions[0]

    assert isinstance(owned, ScalpRestartOwnedPosition)
    assert owned["owner_token"] == owned["order_comment"] == "fxs-owner-101"
    assert dict(owned)["entry_command_id"] == "entry-eurusd"
    with pytest.raises(FrozenInstanceError):
        owned.lots = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.owned_positions = ()  # type: ignore[misc]

    finalized_minute = ((int(NOW) // 60) - 1) * 60
    lifecycle = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=result.owned_positions,
        finalized_common_minute_epoch=finalized_minute,
        as_of_epoch=finalized_minute + 60,
        expected_magic=EXPECTED_MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=1,
    )
    assert lifecycle.diagnostics.accepted is True
    assert lifecycle.close_decisions[0].target_ticket == 101


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        ({"now_epoch": 0}, "now_epoch_invalid"),
        ({"max_snapshot_age_secs": 0}, "max_snapshot_age_invalid"),
        ({"expected_magic": 0}, "expected_magic_invalid"),
        ({"expected_account_scope": ""}, "expected_account_scope_invalid"),
        ({"durable_command_rows": "bad"}, "durable_command_rows_invalid"),
    ),
)
def test_invalid_global_inputs_fail_closed(
    overrides: dict[str, Any], expected_reason: str
) -> None:
    kwargs: dict[str, Any] = {
        "state_snapshot": _state([]),
        "durable_command_rows": [],
        "production_scalp_authority": _authority(),
        "now_epoch": NOW,
        "max_snapshot_age_secs": MAX_AGE,
        "expected_magic": EXPECTED_MAGIC,
        "expected_account_scope": ACCOUNT_SCOPE,
    }
    kwargs.update(overrides)

    result = reconcile_scalp_restart(**kwargs)

    assert result.entry_admission_ready is False
    assert expected_reason in result.quarantine_reasons


def test_expired_or_tampered_current_authority_fails_closed() -> None:
    expired = _authority()
    expired["validation_expires_at_epoch"] = NOW - 1.0
    expired_result = _reconcile(
        state=_state([]), commands=[], authority=expired
    )
    assert expired_result.entry_admission_ready is False
    assert any(
        reason.startswith("scalp_authority_")
        for reason in expired_result.quarantine_reasons
    )

    tampered = _authority()
    tampered["binding_sha256"] = "f" * 64
    tampered_result = _reconcile(
        state=_state([]), commands=[], authority=tampered
    )
    assert tampered_result.entry_admission_ready is False
    assert "scalp_authority_binding_invalid" in tampered_result.quarantine_reasons


@pytest.mark.parametrize(
    ("authority_status", "expected_reason"),
    (
        ("revoked", "scalp_authority_inactive"),
        ("expired", "scalp_authority_validation_expired"),
    ),
)
def test_entry_stop_never_strands_exact_owned_position_or_protective_exit(
    authority_status: str,
    expected_reason: str,
) -> None:
    authority = (
        _authority(expires_at=NOW - 1.0)
        if authority_status == "expired"
        else _authority()
    )
    if authority_status == "revoked":
        authority["status"] = "revoked"
        authority["reason"] = "operator_safety_stop"
    commands = [
        _entry_command(
            authority,
            command_id="entry-eurusd",
            symbol="EURUSD",
            side="BUY",
            owner_token="fxs-owner-101",
            ticket=101,
        ),
        _entry_command(
            authority,
            command_id="pending-btcusd",
            symbol="BTCUSD",
            side="BUY",
            owner_token="fxs-pending-btcusd",
            status="queued",
        ),
        _close_command(
            authority,
            command_id="protective-close-eurusd",
            managed_entry_command_id="entry-eurusd",
            symbol="EURUSD",
            ticket=101,
            owner_token="fxs-owner-101",
            status="queued",
        ),
    ]

    result = _reconcile(
        state=_state([_position()]),
        commands=commands,
        authority=authority,
    )

    assert tuple(item.ticket for item in result.owned_positions) == (101,)
    assert result.active_exit_tickets == (101,)
    assert result.active_queued_entry_symbols == ()
    assert result.entry_admission_ready is False
    assert expected_reason in result.quarantine_reasons


def test_empty_authoritative_snapshot_is_ready_for_exact_22_scope() -> None:
    result = _reconcile(state=_state([]), commands=[])

    assert len(IG_MT4_SCALP_SYMBOLS) == 22
    assert result.entry_admission_ready is True
    assert result.authoritative_open_symbols == ()
    assert result.owned_positions == ()


def test_restart_module_imports_no_side_effect_or_research_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "runtime"
        / "scalp_restart_reconciliation.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    forbidden = (
        "fxstack.scalp",
        "fxstack.settings",
        "fxstack.runtime.dto",
        "fxstack.runtime.runner",
        "fxstack.runtime.service",
        "fxstack.runtime.postgres_store",
        "fxstack.runtime.protocol",
    )
    assert all(
        not any(module == root or module.startswith(f"{root}.") for root in forbidden)
        for module in imported
    )
