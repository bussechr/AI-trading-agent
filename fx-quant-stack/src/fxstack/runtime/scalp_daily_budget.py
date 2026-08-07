"""Classify durable production-scalper entries for the signed daily budget.

The validation certificate limits broker exposure, not database write attempts.
A queued command reserves its cell, an execution-uncertain delivery continues
to reserve it, and a broker-confirmed successful ACK consumes it.  A conclusive
broker refusal or a command that expired before delivery does not consume the
entire UTC day.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


SCALP_DAILY_BUDGET_CLASSIFICATION_SCHEMA = (
    "fxstack.production_scalp_daily_budget_classification.v1"
)
_RESERVING_STATUSES = frozenset({"queued", "delivered", "reconcile_required"})
_SUCCESS_ACK_STATUSES = frozenset(
    {"acked", "ok", "success", "done", "executed", "filled"}
)
_CONCLUSIVE_NON_ENTRY_STATUSES = frozenset({"failed", "duplicate"})


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return number if number > 0 else 0


@dataclass(frozen=True, slots=True)
class ScalpDailyBudgetClassification:
    consumed: bool
    reason: str
    schema_version: str = SCALP_DAILY_BUDGET_CLASSIFICATION_SCHEMA


def classify_scalp_daily_budget_row(
    row: Mapping[str, Any] | None,
) -> ScalpDailyBudgetClassification:
    """Return whether one durable entry row reserves/consumes its daily cell."""

    item = dict(row or {})
    status = str(item.get("status") or "").strip().lower()
    delivered_count = _positive_int(item.get("delivered_count"))
    ack_raw = item.get("ack_json")
    ack = dict(ack_raw) if isinstance(ack_raw, Mapping) else {}
    ack_ticket = _positive_int(ack.get("ticket"))
    mutation_state = str(ack.get("mutation_state") or "").strip().lower()
    attestation_reasons = ack.get("attestation_reasons")
    has_attestation_mismatch = bool(
        str(ack.get("attestation_reason") or "").strip()
        or (
            isinstance(attestation_reasons, (list, tuple))
            and any(str(reason or "").strip() for reason in attestation_reasons)
        )
    )

    # A broker ticket or attestation mismatch proves that an ordinary
    # failed/duplicate label is not a conclusive no-mutation refusal. Reserve
    # the cell until authenticated broker truth resolves it.
    if (ack_ticket > 0 and status != "acked") or has_attestation_mismatch:
        return ScalpDailyBudgetClassification(
            True,
            "broker_mutation_uncertain",
        )

    if status in _RESERVING_STATUSES:
        return ScalpDailyBudgetClassification(True, f"entry_{status}")

    if status == "acked":
        ack_status = str(ack.get("status") or "").strip().lower()
        ticket = ack_ticket
        count_as_trade = ack.get("count_as_trade") is True
        row_command_id = str(item.get("command_id") or "").strip()
        ack_command_id = str(ack.get("command_id") or "").strip()
        command_joined = bool(
            row_command_id and (not ack_command_id or ack_command_id == row_command_id)
        )
        if (
            ack_status in _SUCCESS_ACK_STATUSES
            and ticket > 0
            and count_as_trade
            and command_joined
        ):
            return ScalpDailyBudgetClassification(
                True,
                "broker_confirmed_entry",
            )
        # An ``acked`` entry row without a joined, positive-ticket trade proof
        # is malformed. Reserve conservatively; another entry must not be
        # admitted merely because its success witness is incomplete.
        return ScalpDailyBudgetClassification(
            True,
            "acked_entry_proof_incomplete",
        )

    if status == "expired":
        if delivered_count > 0:
            return ScalpDailyBudgetClassification(
                True,
                "expired_after_delivery_uncertain",
            )
        return ScalpDailyBudgetClassification(
            False,
            "expired_before_delivery",
        )

    if status in _CONCLUSIVE_NON_ENTRY_STATUSES:
        if mutation_state != "not_attempted":
            return ScalpDailyBudgetClassification(
                True,
                f"entry_{status}_mutation_unattested",
            )
        return ScalpDailyBudgetClassification(
            False,
            f"entry_{status}_before_mutation",
        )

    # Unknown durable states fail closed. The queue uncertainty gate should
    # independently block execution, but the signed frequency limit must not
    # become the weaker boundary if that state is inspected in isolation.
    return ScalpDailyBudgetClassification(True, "entry_status_unknown")


__all__ = [
    "SCALP_DAILY_BUDGET_CLASSIFICATION_SCHEMA",
    "ScalpDailyBudgetClassification",
    "classify_scalp_daily_budget_row",
]
