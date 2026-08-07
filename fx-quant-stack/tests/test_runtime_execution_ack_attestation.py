from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from fxstack.runtime.execution_ack_attestation import (
    EXECUTION_ACK_ATTESTATION_SCHEMA,
    MT4_ORDER_ACTUALS_SCHEMA,
    classify_execution_ack,
)


MAGIC = 246810
OWNER = "fxs-owner-eurusd-101"


def _entry_command(side: str = "BUY") -> dict[str, object]:
    side = side.upper()
    if side == "BUY":
        sl_price = 1.099
        tp_price = 1.102
        worst_fill = 1.1002
    else:
        sl_price = 1.101
        tp_price = 1.098
        worst_fill = 1.0998
    return {
        "command_id": f"entry-{side.lower()}-101",
        "cmd": side,
        "symbol": "EURUSD",
        "lots": 0.12,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "magic": MAGIC,
        "owner_token": OWNER,
        "execution_type": "market",
        "worst_fill_price": worst_fill,
        "expected_broker_contract_broker_symbol": "EURUSD.IG",
        "expected_broker_contract_tick_size": 0.00001,
        "expected_broker_contract_lot_step": 0.01,
    }


def _entry_ack(side: str = "BUY") -> dict[str, object]:
    command = _entry_command(side)
    side = side.upper()
    return {
        "actuals_schema": MT4_ORDER_ACTUALS_SCHEMA,
        "actual_command_id": command["command_id"],
        "status": "acked",
        "mutation_state": "confirmed",
        "actual_cmd": side,
        "actual_side": side,
        "actual_symbol": "EURUSD",
        "actual_broker_symbol": "EURUSD.IG",
        "actual_execution_type": "instant_market",
        "actual_ticket": 101,
        "actual_magic": MAGIC,
        "actual_owner_token": OWNER,
        "actual_order_comment": f"{OWNER}-broker-suffix",
        "actual_lots": 0.12,
        "actual_sl_price": command["sl_price"],
        "actual_tp_price": command["tp_price"],
        "actual_open_price": 1.10018 if side == "BUY" else 1.09982,
        "actual_remaining_lots": 0.12,
        "actual_close_time": 0,
    }


def _management_command(cmd: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "command_id": f"management-{cmd.lower()}-101",
        "cmd": cmd,
        "symbol": "EURUSD",
        "magic": MAGIC,
        "owner_token": OWNER,
        "target_ticket": 101,
        "expected_broker_contract_broker_symbol": "EURUSD.IG",
        "expected_broker_contract_tick_size": 0.00001,
        "expected_broker_contract_lot_step": 0.01,
    }
    if cmd == "CLOSE":
        payload["expected_target_lots"] = 0.12
    if cmd == "CLOSE_PARTIAL":
        payload["close_lots"] = 0.04
        payload["expected_target_lots"] = 0.12
    if cmd == "MODIFY_SL":
        payload["sl_price"] = 1.1005
        payload["expected_target_lots"] = 0.12
    return payload


def _management_ack(cmd: str) -> dict[str, object]:
    command = _management_command(cmd)
    payload: dict[str, object] = {
        "actuals_schema": MT4_ORDER_ACTUALS_SCHEMA,
        "actual_command_id": command["command_id"],
        "status": "acked",
        "mutation_state": "confirmed",
        "actual_cmd": cmd,
        "actual_symbol": "EURUSD",
        "actual_broker_symbol": "EURUSD.IG",
        "actual_ticket": 101,
        "actual_target_ticket": 101,
        "actual_magic": MAGIC,
        "actual_owner_token": OWNER,
        "actual_order_comment": f"{OWNER}[broker]",
    }
    if cmd == "CLOSE":
        payload["actual_closed_lots"] = 0.12
        payload["actual_remaining_lots"] = 0.0
        payload["actual_close_time"] = 1_800_000_001
    if cmd == "CLOSE_PARTIAL":
        payload["actual_closed_lots"] = 0.04
        payload["actual_remaining_lots"] = 0.08
        payload["actual_close_time"] = 0
    if cmd == "MODIFY_SL":
        payload["actual_sl"] = 1.1005
        payload["actual_lots"] = 0.12
        payload["actual_remaining_lots"] = 0.12
        payload["actual_close_time"] = 0
    return payload


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_exact_market_entry_ack_is_attested(side: str) -> None:
    result = classify_execution_ack(_entry_command(side), _entry_ack(side))

    assert result.effective_status == "acked"
    assert result.reported_status == "acked"
    assert result.mutation_state == "confirmed"
    assert result.broker_mutating is True
    assert result.attested is True
    assert result.terminal is True
    assert result.reasons == ()
    assert result.actuals.execution_type == "market"
    assert result.actuals.ticket == 101
    assert result.actuals.order_comment.startswith(OWNER)
    assert result.schema_version == EXECUTION_ACK_ATTESTATION_SCHEMA


@pytest.mark.parametrize("cmd", ["CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"])
def test_exact_ticket_management_ack_is_attested(cmd: str) -> None:
    result = classify_execution_ack(
        _management_command(cmd),
        _management_ack(cmd),
    )

    assert result.effective_status == "acked"
    assert result.attested is True
    assert result.reasons == ()
    assert result.actuals.ticket == result.actuals.target_ticket == 101


@pytest.mark.parametrize(
    ("reported_status", "effective_status"),
    [
        ("failed", "failed"),
        ("duplicate", "duplicate"),
        ("delivered", "delivered"),
    ],
)
def test_ticketless_explicit_pre_mutation_outcome_is_preserved(
    reported_status: str,
    effective_status: str,
) -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": reported_status,
            "mutation_state": "not_attempted",
            "ticket": -1,
        },
    )

    assert result.effective_status == effective_status
    assert result.reasons == ()
    assert result.attested is False
    assert result.terminal is (effective_status != "delivered")


@pytest.mark.parametrize("reported_status", ["failed", "duplicate"])
def test_failed_or_duplicate_with_positive_ticket_requires_reconciliation(
    reported_status: str,
) -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": reported_status,
            "mutation_state": "not_attempted",
            "ticket": 101,
        },
    )

    assert result.effective_status == "reconcile_required"
    assert f"ack_{reported_status}_with_positive_ticket" in result.reasons
    assert "ack_ticket_conflicts_with_not_attempted" in result.reasons


@pytest.mark.parametrize(
    "mutation_state",
    ["attempted", "unknown", "post_mutation_unknown"],
)
def test_unknown_post_mutation_failure_requires_reconciliation(
    mutation_state: str,
) -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": "failed",
            "mutation_state": mutation_state,
        },
    )

    assert result.effective_status == "reconcile_required"
    assert "ack_unknown_post_mutation_outcome" in result.reasons
    assert "ack_not_conclusive_pre_mutation_refusal" in result.reasons


def test_explicit_reconcile_status_always_wins() -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": "reconcile_required",
            "mutation_state": "not_attempted",
        },
    )

    assert result.effective_status == "reconcile_required"
    assert "ack_explicit_reconcile_required" in result.reasons


def test_explicit_uncertainty_flag_always_wins() -> None:
    ack = _entry_ack()
    ack["execution_uncertain"] = True

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_explicit_reconcile_required" in result.reasons


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("actual_command_id", "other-command", "ack_command_id_mismatch"),
        ("actual_symbol", "USDJPY", "ack_symbol_mismatch"),
        ("actual_broker_symbol", "EURUSD.BAD", "ack_broker_symbol_mismatch"),
        ("actual_cmd", "SELL", "ack_cmd_mismatch"),
        ("actual_side", "SELL", "ack_side_mismatch"),
        ("actual_magic", MAGIC + 1, "ack_magic_mismatch"),
        (
            "actual_order_comment",
            "foreign-owner",
            "ack_owner_comment_prefix_mismatch",
        ),
    ],
)
def test_any_entry_identity_mismatch_requires_reconciliation(
    field: str,
    value: object,
    expected_reason: str,
) -> None:
    ack = _entry_ack()
    ack[field] = value

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "reconcile_required"
    assert expected_reason in result.reasons
    assert result.attested is False


def test_owner_comment_may_have_a_broker_suffix_but_not_a_prefix() -> None:
    passing = _entry_ack()
    passing["actual_order_comment"] = f"{OWNER}.server-added"
    assert classify_execution_ack(_entry_command(), passing).effective_status == "acked"

    failing = _entry_ack()
    failing["actual_order_comment"] = f"server.{OWNER}"
    result = classify_execution_ack(_entry_command(), failing)
    assert result.effective_status == "reconcile_required"
    assert "ack_owner_comment_prefix_mismatch" in result.reasons


@pytest.mark.parametrize(
    ("field", "value", "expected_reason"),
    [
        ("actual_lots", 0.13, "ack_actual_lots_mismatch"),
        ("actual_sl_price", 1.0989, "ack_actual_sl_mismatch"),
        ("actual_tp_price", 1.1021, "ack_actual_tp_mismatch"),
        ("actual_open_price", 1.10021, "ack_actual_open_price_exceeds_buy_bound"),
        (
            "actual_execution_type",
            "BUY_LIMIT",
            "ack_execution_type_not_market",
        ),
    ],
)
def test_entry_actual_or_market_bound_mismatch_requires_reconciliation(
    field: str,
    value: object,
    expected_reason: str,
) -> None:
    ack = _entry_ack()
    ack[field] = value

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "reconcile_required"
    assert expected_reason in result.reasons


def test_sell_fill_uses_the_adverse_lower_bound() -> None:
    within = _entry_ack("SELL")
    within["actual_open_price"] = 1.0998
    assert classify_execution_ack(_entry_command("SELL"), within).effective_status == "acked"

    beyond = _entry_ack("SELL")
    beyond["actual_open_price"] = 1.09979
    result = classify_execution_ack(_entry_command("SELL"), beyond)
    assert result.effective_status == "reconcile_required"
    assert "ack_actual_open_price_exceeds_sell_bound" in result.reasons


@pytest.mark.parametrize(
    ("field", "expected_reason"),
    [
        ("actual_command_id", "ack_command_id_missing"),
        ("actual_lots", "ack_actual_lots_missing"),
        ("actual_sl_price", "ack_actual_sl_missing"),
        ("actual_tp_price", "ack_actual_tp_missing"),
        ("actual_open_price", "ack_actual_open_price_missing"),
        ("actual_broker_symbol", "ack_broker_symbol_missing"),
        ("actual_side", "ack_side_missing"),
        ("actual_execution_type", "ack_execution_type_missing"),
        ("actual_order_comment", "ack_owner_comment_missing"),
    ],
)
def test_success_missing_required_actual_requires_reconciliation(
    field: str,
    expected_reason: str,
) -> None:
    ack = _entry_ack()
    ack.pop(field)
    if field == "actual_order_comment":
        ack.pop("actual_owner_token")

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "reconcile_required"
    assert expected_reason in result.reasons


@pytest.mark.parametrize(
    "cmd",
    ["BUY", "SELL", "CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"],
)
def test_any_broker_mutating_success_without_a_positive_ticket_reconciles(
    cmd: str,
) -> None:
    command = _entry_command(cmd) if cmd in {"BUY", "SELL"} else _management_command(cmd)
    ack = _entry_ack(cmd) if cmd in {"BUY", "SELL"} else _management_ack(cmd)
    ack["actual_ticket"] = -1

    result = classify_execution_ack(command, ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_positive_ticket_missing" in result.reasons


@pytest.mark.parametrize("cmd", ["CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"])
def test_management_target_ticket_mismatch_requires_reconciliation(cmd: str) -> None:
    ack = _management_ack(cmd)
    ack["actual_target_ticket"] = 202

    result = classify_execution_ack(_management_command(cmd), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_target_ticket_mismatch" in result.reasons


def test_partial_close_actual_amount_must_equal_the_command() -> None:
    ack = _management_ack("CLOSE_PARTIAL")
    ack["actual_closed_lots"] = 0.05

    result = classify_execution_ack(_management_command("CLOSE_PARTIAL"), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_actual_close_lots_mismatch" in result.reasons


def test_full_close_actual_amount_is_checked_when_the_command_binds_target_lots() -> None:
    ack = _management_ack("CLOSE")
    ack["actual_closed_lots"] = 0.11

    result = classify_execution_ack(_management_command("CLOSE"), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_actual_close_lots_mismatch" in result.reasons


def test_modify_sl_actual_must_equal_the_command() -> None:
    ack = _management_ack("MODIFY_SL")
    ack["actual_sl_price"] = 1.1004

    result = classify_execution_ack(_management_command("MODIFY_SL"), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_actual_sl_mismatch" in result.reasons


def test_normalized_ack_can_retain_broker_actuals_in_raw_mapping() -> None:
    raw = _entry_ack()
    normalized = {
        "command_id": raw["actual_command_id"],
        "status": "acked",
        "ticket": -1,
        "symbol": "",
        "magic": -1,
        "raw": raw,
    }
    row = {
        "command_id": _entry_command()["command_id"],
        "cmd": "BUY",
        "symbol": "EURUSD",
        "lots": 0.12,
        "tp_price": 1.102,
        "sl_price": 1.099,
        "magic": MAGIC,
        "payload_json": _entry_command(),
    }

    result = classify_execution_ack(row, normalized)

    assert result.effective_status == "acked"
    assert result.actuals.open_price == pytest.approx(1.10018)
    assert result.actuals.broker_symbol == "EURUSD.IG"


def test_nested_actual_fields_are_supported_and_preserved() -> None:
    flat = _entry_ack()
    schema = flat.pop("actuals_schema")
    actual_fields = {
        key: value
        for key, value in flat.items()
        if key not in {"status", "mutation_state"}
    }
    ack = {
        "actuals_schema": schema,
        "status": "success",
        "mutation_state": "broker_confirmed",
        "actual_fields": actual_fields,
    }

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "acked"
    assert result.reported_status == "success"
    assert result.actuals.to_dict()["lots"] == pytest.approx(0.12)


def test_generic_echoes_cannot_replace_explicit_post_select_actuals() -> None:
    command = _entry_command()
    ack = {
        "actuals_schema": MT4_ORDER_ACTUALS_SCHEMA,
        "command_id": command["command_id"],
        "status": "acked",
        "mutation_state": "confirmed",
        "cmd": "BUY",
        "side": "BUY",
        "symbol": "EURUSD",
        "broker_symbol": "EURUSD.IG",
        "execution_type": "market",
        "ticket": 101,
        "magic": MAGIC,
        "owner_token": OWNER,
        "order_comment": OWNER,
        "lots": 0.12,
        "sl_price": 1.099,
        "tp_price": 1.102,
        "open_price": 1.10018,
        "actual_command_id": "",
        "actual_cmd": "",
        "actual_side": "",
        "actual_symbol": "",
        "actual_broker_symbol": "",
        "actual_execution_type": "",
        "actual_ticket": -1,
        "actual_magic": -1,
        "actual_order_comment": "",
        "actual_lots": -1,
        "actual_sl_price": -1,
        "actual_tp_price": -1,
        "actual_open_price": -1,
        "actual_remaining_lots": -1,
        "actual_close_time": -1,
    }

    result = classify_execution_ack(command, ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_explicit_actual_order_comment_invalid" in result.reasons
    assert "ack_explicit_actual_ticket_invalid" in result.reasons
    assert "ack_explicit_actual_remaining_lots_invalid" in result.reasons


def test_conflicting_normalized_and_raw_tickets_require_reconciliation() -> None:
    raw = _entry_ack()
    raw["actual_ticket"] = 202
    ack = {**_entry_ack(), "raw": raw}

    result = classify_execution_ack(_entry_command(), ack)

    assert result.effective_status == "reconcile_required"
    assert "ack_ticket_conflict" in result.reasons


def test_boolean_mutation_evidence_can_prove_a_preflight_refusal() -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": "rejected",
            "broker_mutation_attempted": False,
            "broker_outcome_known": True,
        },
    )

    assert result.effective_status == "failed"
    assert result.mutation_state == "not_attempted"


def test_missing_mutation_evidence_cannot_make_a_failure_terminal() -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {"command_id": command["command_id"], "status": "failed"},
    )

    assert result.effective_status == "reconcile_required"
    assert "ack_not_conclusive_pre_mutation_refusal" in result.reasons


def test_non_mutating_info_ack_does_not_require_a_ticket() -> None:
    result = classify_execution_ack(
        {"command_id": "info-1", "cmd": "INFO"},
        {"command_id": "info-1", "status": "acked"},
    )

    assert result.effective_status == "acked"
    assert result.broker_mutating is False
    assert result.attested is True


def test_legacy_close_all_is_mutating_and_cannot_claim_ticketless_success() -> None:
    command = {"command_id": "close-all-1", "cmd": "CLOSE_ALL", "magic": MAGIC}

    uncertain = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": "acked",
            "mutation_state": "confirmed",
        },
    )
    refused = classify_execution_ack(
        command,
        {
            "command_id": command["command_id"],
            "status": "failed",
            "mutation_state": "not_attempted",
            "ticket": -1,
        },
    )

    assert uncertain.broker_mutating is True
    assert uncertain.effective_status == "reconcile_required"
    assert "ack_positive_ticket_missing" in uncertain.reasons
    assert refused.effective_status == "failed"
    assert refused.terminal is True


@pytest.mark.parametrize("status", ["", "mystery"])
def test_missing_or_unknown_status_requires_reconciliation(status: str) -> None:
    command = _entry_command()
    result = classify_execution_ack(
        command,
        {"command_id": command["command_id"], "status": status},
    )

    assert result.effective_status == "reconcile_required"
    assert any(reason.startswith("ack_status_") for reason in result.reasons)


def test_result_and_actuals_are_immutable() -> None:
    result = classify_execution_ack(_entry_command(), _entry_ack())

    with pytest.raises(FrozenInstanceError):
        result.effective_status = "failed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.actuals.ticket = 202  # type: ignore[misc]

    copied = result.to_dict()
    copied["actuals"]["ticket"] = 202
    assert result.actuals.ticket == 101


def test_input_mappings_are_not_mutated() -> None:
    command = _entry_command()
    ack = _entry_ack()
    command_before = deepcopy(command)
    ack_before = deepcopy(ack)

    classify_execution_ack(command, ack)

    assert command == command_before
    assert ack == ack_before
