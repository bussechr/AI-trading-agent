from __future__ import annotations

import pytest

from fxstack.runtime.scalp_daily_budget import classify_scalp_daily_budget_row


@pytest.mark.parametrize(
    ("status", "delivered_count", "expected_reason"),
    [
        ("queued", 0, "entry_queued"),
        ("delivered", 1, "entry_delivered"),
        ("reconcile_required", 1, "entry_reconcile_required"),
        ("expired", 1, "expired_after_delivery_uncertain"),
    ],
)
def test_pending_or_execution_uncertain_entry_reserves_daily_cell(
    status: str,
    delivered_count: int,
    expected_reason: str,
) -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": status,
            "delivered_count": delivered_count,
        }
    )

    assert result.consumed is True
    assert result.reason == expected_reason


def test_joined_positive_ticket_ack_consumes_daily_cell() -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": "acked",
            "delivered_count": 1,
            "ack_json": {
                "command_id": "entry-1",
                "status": "acked",
                "ticket": 731,
                "count_as_trade": True,
            },
        }
    )

    assert result.consumed is True
    assert result.reason == "broker_confirmed_entry"


@pytest.mark.parametrize(
    ("status", "delivered_count", "expected_reason"),
    [
        ("failed", 1, "entry_failed_before_mutation"),
        ("duplicate", 1, "entry_duplicate_before_mutation"),
        ("expired", 0, "expired_before_delivery"),
    ],
)
def test_conclusive_non_entry_does_not_consume_daily_cell(
    status: str,
    delivered_count: int,
    expected_reason: str,
) -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": status,
            "delivered_count": delivered_count,
            "ack_json": {"mutation_state": "not_attempted"},
        }
    )

    assert result.consumed is False
    assert result.reason == expected_reason


@pytest.mark.parametrize(
    "ack_json",
    [
        {},
        {"status": "acked", "ticket": -1, "count_as_trade": False},
        {
            "command_id": "other-entry",
            "status": "acked",
            "ticket": -1,
            "count_as_trade": False,
        },
    ],
)
def test_malformed_acked_entry_remains_conservatively_reserved(
    ack_json: dict,
) -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": "acked",
            "delivered_count": 1,
            "ack_json": ack_json,
        }
    )

    assert result.consumed is True
    assert result.reason == "acked_entry_proof_incomplete"


@pytest.mark.parametrize("status", ("failed", "duplicate"))
def test_positive_ticket_non_success_reserves_as_broker_uncertainty(
    status: str,
) -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": status,
            "delivered_count": 1,
            "ack_json": {
                "command_id": "entry-1",
                "status": status,
                "ticket": 731,
                "mutation_state": "attempted",
            },
        }
    )

    assert result.consumed is True
    assert result.reason == "broker_mutation_uncertain"


@pytest.mark.parametrize("status", ("failed", "duplicate"))
def test_ticketless_non_success_without_no_mutation_proof_still_reserves(
    status: str,
) -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": status,
            "delivered_count": 1,
            "ack_json": {"status": status, "ticket": -1},
        }
    )

    assert result.consumed is True
    assert result.reason == f"entry_{status}_mutation_unattested"


def test_unknown_state_fails_closed() -> None:
    result = classify_scalp_daily_budget_row(
        {
            "command_id": "entry-1",
            "status": "mystery",
            "delivered_count": 0,
        }
    )

    assert result.consumed is True
    assert result.reason == "entry_status_unknown"
