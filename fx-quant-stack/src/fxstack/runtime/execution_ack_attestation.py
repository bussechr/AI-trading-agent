# AGENT: ROLE: Purely attest MT4 broker mutation outcomes before queue finalization.
# AGENT: ENTRYPOINT: `classify_execution_ack`.
# AGENT: PRIMARY INPUTS: one durable command row/payload and one normalized/raw ACK mapping.
# AGENT: PRIMARY OUTPUTS: immutable effective ACK status, reasons, and broker actuals.
# AGENT: STATE / SIDE EFFECTS: none.
# AGENT: HANDSHAKES: command queue -> MT4 post-mutation OrderSelect attestation.
"""Fail-closed classification of execution acknowledgements.

An HTTP ACK proves transport, not necessarily a broker mutation.  Production
commands are terminal-successful only when MT4 reports a confirmed mutation
and the selected broker ticket exactly attests the command identity and its
applicable economic bounds.  A ticketless refusal is terminal only when the EA
explicitly states that no broker mutation was attempted.  Every ambiguous
post-mutation outcome remains subject to broker reconciliation.

The classifier intentionally accepts both a raw ACK mapping and a normalized
mapping containing ``raw`` and/or ``actual_fields`` mappings.  It does not
depend on the API DTO so it can be applied at the durable-store boundary.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import math
from typing import Any, Literal


EXECUTION_ACK_ATTESTATION_SCHEMA = "fxstack.execution_ack_attestation.v1"
MT4_ORDER_ACTUALS_SCHEMA = "fxstack.mt4_order_actuals.v1"

EffectiveAckStatus = Literal[
    "acked",
    "failed",
    "duplicate",
    "delivered",
    "reconcile_required",
]

_BROKER_MUTATING_COMMANDS = frozenset(
    {"BUY", "SELL", "CLOSE", "CLOSE_ALL", "CLOSE_PARTIAL", "MODIFY_SL"}
)
_ENTRY_COMMANDS = frozenset({"BUY", "SELL"})
_MANAGEMENT_COMMANDS = frozenset({"CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"})

_ACKED_STATUSES = frozenset({"acked", "ok", "success", "done", "executed", "filled"})
_FAILED_STATUSES = frozenset({"failed", "error", "rejected", "refused"})
_DELIVERED_STATUSES = frozenset({"delivered", "queued", "retry", "pending"})
_DUPLICATE_STATUSES = frozenset({"duplicate", "duplicated"})
_RECONCILE_STATUSES = frozenset(
    {
        "reconcile_required",
        "reconciliation_required",
        "uncertain",
        "unknown",
        "outcome_unknown",
    }
)

_NOT_ATTEMPTED_STATES = frozenset(
    {
        "not_attempted",
        "not_started",
        "pre_mutation",
        "preflight_refused",
        "refused_before_mutation",
    }
)
_CONFIRMED_STATES = frozenset(
    {
        "confirmed",
        "attested",
        "broker_confirmed",
        "post_mutation_confirmed",
        "succeeded",
    }
)
_ATTEMPTED_STATES = frozenset(
    {"attempted", "submitted", "sent", "post_mutation", "mutation_attempted"}
)
_UNKNOWN_STATES = frozenset(
    {"unknown", "uncertain", "outcome_unknown", "post_mutation_unknown"}
)


@dataclass(frozen=True, slots=True)
class ExecutionAckActuals:
    """Normalized broker facts retained with the classification."""

    command_id: str = ""
    cmd: str = ""
    symbol: str = ""
    broker_symbol: str = ""
    side: str = ""
    execution_type: str = ""
    ticket: int = -1
    target_ticket: int = -1
    magic: int = -1
    owner_token: str = ""
    order_comment: str = ""
    lots: float | None = None
    sl_price: float | None = None
    tp_price: float | None = None
    open_price: float | None = None
    remaining_lots: float | None = None
    close_time: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionAckAttestation:
    """Immutable effective status and the evidence used to derive it."""

    effective_status: EffectiveAckStatus
    reported_status: str
    mutation_state: str
    broker_mutating: bool
    attested: bool
    terminal: bool
    reasons: tuple[str, ...]
    actuals: ExecutionAckActuals
    schema_version: str = EXECUTION_ACK_ATTESTATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "effective_status": self.effective_status,
            "reported_status": self.reported_status,
            "mutation_state": self.mutation_state,
            "broker_mutating": bool(self.broker_mutating),
            "attested": bool(self.attested),
            "terminal": bool(self.terminal),
            "reasons": list(self.reasons),
            "actuals": self.actuals.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class _CommandExpectation:
    command_id: str
    cmd: str
    symbol: str
    broker_symbol: str
    side: str
    execution_type: str
    magic: int | None
    owner_token: str
    target_ticket: int | None
    lots: float | None
    sl_price: float | None
    tp_price: float | None
    worst_fill_price: float | None
    tick_size: float | None
    lot_step: float | None
    target_open_price: float | None
    target_lots: float | None
    expected_tp_price: float | None


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _command_view(command: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten a payload or durable row, keeping row columns authoritative."""

    top = _mapping(command)
    out: dict[str, Any] = {}
    out.update(_mapping(top.get("payload_json")))
    out.update(_mapping(top.get("payload")))
    out.update(
        {
            key: value
            for key, value in top.items()
            if key not in {"payload", "payload_json"} and value is not None
        }
    )
    return out


def _ack_sources(ack: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return all normalized/raw containers from strongest to weakest."""

    top = _mapping(ack)
    raw = _mapping(top.get("raw"))
    sources: list[dict[str, Any]] = []
    for candidate in (
        _mapping(top.get("actual_fields")),
        _mapping(top.get("actuals")),
        _mapping(top.get("broker_actual")),
        top,
        _mapping(raw.get("actual_fields")),
        _mapping(raw.get("actuals")),
        _mapping(raw.get("broker_actual")),
        raw,
    ):
        if candidate and all(candidate is not existing for existing in sources):
            sources.append(candidate)
    return tuple(sources)


def _wire_ack(ack: Mapping[str, Any]) -> dict[str, Any]:
    """Return the producer-authored envelope, not DTO-derived aliases."""

    top = _mapping(ack)
    raw = _mapping(top.get("raw"))
    return raw or top


def _explicit_actual_reasons(
    expected: _CommandExpectation,
    ack: Mapping[str, Any],
) -> tuple[str, ...]:
    """Require versioned fields read back from MT4 OrderSelect/order history."""

    wire = _wire_ack(ack)
    wire_actuals = (
        _mapping(wire.get("actual_fields"))
        or _mapping(wire.get("broker_actual"))
        or _mapping(wire.get("actuals"))
    )
    reasons: list[str] = []
    if str(wire.get("actuals_schema") or "").strip() != MT4_ORDER_ACTUALS_SCHEMA:
        reasons.append("ack_actuals_schema_mismatch")
    required: dict[str, tuple[str, ...]] = {
        "command_id": ("actual_command_id",),
        "cmd": ("actual_cmd",),
        "symbol": ("actual_symbol",),
        "broker_symbol": ("actual_broker_symbol",),
        "ticket": ("actual_ticket",),
        "magic": ("actual_magic",),
        "order_comment": ("actual_order_comment",),
    }
    if expected.cmd in _ENTRY_COMMANDS:
        required.update(
            {
                "side": ("actual_side",),
                "execution_type": ("actual_execution_type",),
                "lots": ("actual_lots",),
                "sl_price": ("actual_sl_price", "actual_sl"),
                "tp_price": ("actual_tp_price", "actual_tp"),
                "open_price": ("actual_open_price",),
                "remaining_lots": ("actual_remaining_lots",),
                "close_time": ("actual_close_time",),
            }
        )
    if expected.cmd in _MANAGEMENT_COMMANDS:
        required["target_ticket"] = ("actual_target_ticket",)
        required["lots"] = (
            "actual_lots",
            "actual_closed_lots",
            "closed_lots",
        )
        required["remaining_lots"] = ("actual_remaining_lots",)
        required["close_time"] = ("actual_close_time",)
    if expected.cmd == "MODIFY_SL":
        required["sl_price"] = ("actual_sl_price", "actual_sl")

    def _explicit_value(aliases: tuple[str, ...]) -> Any:
        for alias in aliases:
            if alias in wire:
                return wire.get(alias)
            if alias in wire_actuals:
                return wire_actuals.get(alias)
        return None

    for label, aliases in required.items():
        if not any(alias in wire or alias in wire_actuals for alias in aliases):
            reasons.append(f"ack_explicit_actual_{label}_missing")
            continue
        value = _explicit_value(aliases)
        if label in {
            "command_id",
            "cmd",
            "symbol",
            "broker_symbol",
            "order_comment",
            "side",
            "execution_type",
        }:
            if not _text(value):
                reasons.append(f"ack_explicit_actual_{label}_invalid")
        elif label in {"ticket", "target_ticket", "magic"}:
            if _positive_int(value) is None:
                reasons.append(f"ack_explicit_actual_{label}_invalid")
        elif label in {"lots", "sl_price", "tp_price", "open_price"}:
            if _positive(value) is None:
                reasons.append(f"ack_explicit_actual_{label}_invalid")
        elif label in {"remaining_lots", "close_time"}:
            if _nonnegative(value) is None:
                reasons.append(f"ack_explicit_actual_{label}_invalid")
    return _unique(reasons)


def _first_value(view: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name not in view:
            continue
        value = view.get(name)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        return value
    return None


def _first_ack_value(sources: tuple[dict[str, Any], ...], *names: str) -> Any:
    for name in names:
        for source in sources:
            if name not in source:
                continue
            value = source.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            return value
    return None


def _all_ack_values(sources: tuple[dict[str, Any], ...], *names: str) -> tuple[Any, ...]:
    values: list[Any] = []
    for source in sources:
        for name in names:
            if name not in source:
                continue
            value = source.get(name)
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            values.append(value)
    return tuple(values)


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _upper(value: Any) -> str:
    return _text(value).upper()


def _lower(value: Any) -> str:
    return _text(value).lower()


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0.0 else None


def _nonnegative(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number >= 0.0 else None


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
        as_float = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(as_float) or float(number) != as_float or number <= 0:
        return None
    return number


def _first_positive_ack(
    sources: tuple[dict[str, Any], ...],
    *names: str,
) -> float | None:
    for name in names:
        for source in sources:
            if name not in source:
                continue
            value = _positive(source.get(name))
            if value is not None:
                return value
    return None


def _first_nonnegative_ack(
    sources: tuple[dict[str, Any], ...],
    *names: str,
) -> float | None:
    for name in names:
        for source in sources:
            if name not in source:
                continue
            value = _nonnegative(source.get(name))
            if value is not None:
                return value
    return None


def _first_positive_int_ack(
    sources: tuple[dict[str, Any], ...],
    *names: str,
) -> int | None:
    for name in names:
        for source in sources:
            if name not in source:
                continue
            value = _positive_int(source.get(name))
            if value is not None:
                return value
    return None


def _reported_ticket(sources: tuple[dict[str, Any], ...]) -> int:
    values = _all_ack_values(sources, "actual_ticket", "order_ticket", "ticket")
    positives = tuple(value for raw in values if (value := _positive_int(raw)) is not None)
    return positives[0] if positives else -1


def _normalize_status(raw: str) -> EffectiveAckStatus | None:
    if raw in _ACKED_STATUSES:
        return "acked"
    if raw in _FAILED_STATUSES:
        return "failed"
    if raw in _DUPLICATE_STATUSES:
        return "duplicate"
    if raw in _DELIVERED_STATUSES:
        return "delivered"
    if raw in _RECONCILE_STATUSES:
        return "reconcile_required"
    return None


def _normalize_execution_type(raw: Any) -> str:
    value = _lower(raw).replace("-", "_").replace(" ", "_")
    if value in {"market", "instant", "instant_market", "market_execution"}:
        return "market"
    return value


def _unique(reasons: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reason for reason in reasons if reason))


def _numeric_equal(
    actual: float,
    expected: float,
    *,
    quantum: float | None,
) -> bool:
    absolute_tolerance = max(
        1e-12,
        abs(float(expected)) * 1e-12,
        (float(quantum) * 1e-6) if quantum is not None and quantum > 0.0 else 0.0,
    )
    return math.isclose(
        float(actual),
        float(expected),
        rel_tol=1e-12,
        abs_tol=absolute_tolerance,
    )


def _expectation(command: Mapping[str, Any]) -> _CommandExpectation:
    view = _command_view(command)
    plan = _mapping(view.get("broker_entry_plan"))
    cmd = _upper(_first_value(view, "cmd", "command"))
    lots_name = "close_lots" if cmd == "CLOSE_PARTIAL" else "lots"
    target_lots = _positive(
        _first_value(view, "expected_target_lots", "target_lots", "open_lots")
    )
    target_open = _positive(
        _first_value(view, "expected_open_price", "target_open_price")
    )
    expected_tp = _positive(
        _first_value(view, "expected_tp_price", "target_tp_price")
    )
    execution_type = _normalize_execution_type(
        _first_value(view, "execution_type")
        or ("market" if cmd in _ENTRY_COMMANDS else "")
    )
    return _CommandExpectation(
        command_id=_text(_first_value(view, "command_id", "id", "signal_id")),
        cmd=cmd,
        symbol=_upper(_first_value(view, "symbol", "canonical_symbol", "logical_symbol")),
        broker_symbol=_text(
            _first_value(
                view,
                "expected_broker_contract_broker_symbol",
                "expected_broker_symbol",
                "broker_symbol",
            )
            or plan.get("broker_symbol")
        ),
        side=_upper(_first_value(view, "target_side", "position_side", "side"))
        or (cmd if cmd in _ENTRY_COMMANDS else ""),
        execution_type=execution_type,
        magic=_positive_int(_first_value(view, "magic")),
        owner_token=_text(_first_value(view, "owner_token")),
        target_ticket=_positive_int(_first_value(view, "target_ticket")),
        lots=_positive(_first_value(view, lots_name)),
        sl_price=_positive(_first_value(view, "sl_price", "sl")),
        tp_price=_positive(_first_value(view, "tp_price", "tp")),
        worst_fill_price=_positive(
            _first_value(view, "worst_fill_price", "entry_price")
            or plan.get("worst_fill_price")
        ),
        tick_size=_positive(
            _first_value(view, "expected_broker_contract_tick_size", "tick_size")
            or plan.get("tick_size")
        ),
        lot_step=_positive(
            _first_value(view, "expected_broker_contract_lot_step", "lot_step")
        ),
        target_open_price=target_open,
        target_lots=target_lots,
        expected_tp_price=expected_tp,
    )


def _mutation_state(
    sources: tuple[dict[str, Any], ...],
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    canonical_states: set[str] = set()
    for raw in _all_ack_values(
        sources,
        "mutation_state",
        "broker_mutation_state",
        "execution_mutation_state",
    ):
        raw_state = _lower(raw)
        if raw_state in _NOT_ATTEMPTED_STATES:
            canonical_states.add("not_attempted")
        elif raw_state in _CONFIRMED_STATES:
            canonical_states.add("confirmed")
        elif raw_state in _ATTEMPTED_STATES:
            canonical_states.add("attempted")
        elif raw_state in _UNKNOWN_STATES:
            canonical_states.add("unknown")
        elif raw_state:
            canonical_states.add("unknown")
            reasons.append("ack_mutation_state_unsupported")
    if len(canonical_states) > 1:
        reasons.append("ack_mutation_state_conflict")
        state = "unknown"
    else:
        state = next(iter(canonical_states), "")

    def boolean_evidence(*names: str) -> bool | None:
        values = {
            value
            for value in _all_ack_values(sources, *names)
            if isinstance(value, bool)
        }
        if len(values) > 1:
            reasons.append("ack_mutation_state_conflict")
            return None
        return next(iter(values), None)

    attempted = boolean_evidence(
        "broker_mutation_attempted", "mutation_attempted"
    )
    confirmed = boolean_evidence(
        "broker_mutation_confirmed", "mutation_confirmed"
    )
    outcome_known = boolean_evidence("broker_outcome_known", "outcome_known")

    inferred = ""
    if confirmed is True:
        inferred = "confirmed"
    elif attempted is False:
        inferred = "not_attempted"
    elif attempted is True and outcome_known is False:
        inferred = "unknown"
    elif attempted is True:
        inferred = "attempted"
    elif outcome_known is False:
        inferred = "unknown"

    if state and inferred and state != inferred:
        reasons.append("ack_mutation_state_conflict")
        return "unknown", _unique(reasons)
    state = state or inferred
    if confirmed is False and state == "confirmed":
        reasons.append("ack_mutation_state_conflict")
        state = "unknown"
    if outcome_known is False and state == "confirmed":
        reasons.append("ack_mutation_state_conflict")
        state = "unknown"
    return state, _unique(reasons)


def _actuals(sources: tuple[dict[str, Any], ...]) -> ExecutionAckActuals:
    return ExecutionAckActuals(
        command_id=_text(
            _first_ack_value(sources, "actual_command_id", "command_id", "signal_id", "id")
        ),
        cmd=_upper(_first_ack_value(sources, "actual_cmd", "executed_cmd", "cmd", "command")),
        symbol=_upper(
            _first_ack_value(
                sources, "actual_symbol", "canonical_symbol", "logical_symbol", "symbol"
            )
        ),
        broker_symbol=_text(
            _first_ack_value(
                sources, "actual_broker_symbol", "order_symbol", "broker_symbol"
            )
        ),
        side=_upper(_first_ack_value(sources, "actual_side", "order_side", "side")),
        execution_type=_normalize_execution_type(
            _first_ack_value(
                sources, "actual_execution_type", "execution_type", "order_kind"
            )
        ),
        ticket=_reported_ticket(sources),
        target_ticket=(
            _first_positive_int_ack(sources, "actual_target_ticket", "target_ticket")
            or -1
        ),
        magic=(
            _first_positive_int_ack(sources, "actual_magic", "order_magic", "magic")
            or -1
        ),
        owner_token=_text(
            _first_ack_value(sources, "actual_owner_token", "owner_token")
        ),
        order_comment=_text(
            _first_ack_value(
                sources,
                "actual_order_comment",
                "order_comment",
                "broker_comment",
            )
        ),
        lots=_first_positive_ack(
            sources,
            "actual_lots",
            "actual_closed_lots",
            "filled_lots",
            "closed_lots",
            "order_lots",
            "lots",
        ),
        sl_price=_first_positive_ack(
            sources, "actual_sl_price", "actual_sl", "order_sl", "sl_price", "sl"
        ),
        tp_price=_first_positive_ack(
            sources, "actual_tp_price", "actual_tp", "order_tp", "tp_price", "tp"
        ),
        open_price=_first_positive_ack(
            sources,
            "actual_open_price",
            "fill_price",
            "order_open_price",
            "executed_price",
            "open_price",
        ),
        remaining_lots=_first_nonnegative_ack(
            sources,
            "actual_remaining_lots",
            "remaining_lots",
        ),
        close_time=_first_nonnegative_ack(
            sources,
            "actual_close_time",
            "close_time",
        ),
    )


def _has_true(sources: tuple[dict[str, Any], ...], *names: str) -> bool:
    return any(value is True for value in _all_ack_values(sources, *names))


def _text_conflict(values: tuple[Any, ...], *, normalize: str = "exact") -> bool:
    normalized: set[str] = set()
    for raw in values:
        value = _text(raw)
        if not value:
            continue
        if normalize == "upper":
            value = value.upper()
        elif normalize == "lower":
            value = value.lower()
        normalized.add(value)
    return len(normalized) > 1


def _ticket_conflict(sources: tuple[dict[str, Any], ...]) -> bool:
    values = {
        ticket
        for raw in _all_ack_values(sources, "actual_ticket", "order_ticket", "ticket")
        if (ticket := _positive_int(raw)) is not None
    }
    return len(values) > 1


def _identity_reasons(
    expected: _CommandExpectation,
    actual: ExecutionAckActuals,
    sources: tuple[dict[str, Any], ...],
) -> tuple[str, ...]:
    reasons: list[str] = []
    if _text_conflict(
        _all_ack_values(sources, "actual_command_id", "command_id", "signal_id", "id")
    ):
        reasons.append("ack_command_id_conflict")
    if not actual.command_id:
        reasons.append("ack_command_id_missing")
    elif not expected.command_id or actual.command_id != expected.command_id:
        reasons.append("ack_command_id_mismatch")

    if _text_conflict(
        _all_ack_values(
            sources, "actual_symbol", "canonical_symbol", "logical_symbol", "symbol"
        ),
        normalize="upper",
    ):
        reasons.append("ack_symbol_conflict")
    if actual.symbol and expected.symbol and actual.symbol != expected.symbol:
        reasons.append("ack_symbol_mismatch")

    if _text_conflict(
        _all_ack_values(sources, "actual_broker_symbol", "order_symbol", "broker_symbol")
    ):
        reasons.append("ack_broker_symbol_conflict")
    if (
        actual.broker_symbol
        and expected.broker_symbol
        and actual.broker_symbol != expected.broker_symbol
    ):
        reasons.append("ack_broker_symbol_mismatch")

    if _text_conflict(
        _all_ack_values(sources, "actual_cmd", "executed_cmd", "cmd", "command"),
        normalize="upper",
    ):
        reasons.append("ack_cmd_conflict")
    if actual.cmd and expected.cmd and actual.cmd != expected.cmd:
        reasons.append("ack_cmd_mismatch")

    if _text_conflict(
        _all_ack_values(sources, "actual_side", "order_side", "side"),
        normalize="upper",
    ):
        reasons.append("ack_side_conflict")
    if actual.side and expected.side and actual.side != expected.side:
        reasons.append("ack_side_mismatch")

    magic_values = {
        magic
        for raw in _all_ack_values(sources, "actual_magic", "order_magic", "magic")
        if (magic := _positive_int(raw)) is not None
    }
    if len(magic_values) > 1:
        reasons.append("ack_magic_conflict")
    if actual.magic > 0 and expected.magic is not None and actual.magic != expected.magic:
        reasons.append("ack_magic_mismatch")

    comments = _all_ack_values(
        sources, "actual_order_comment", "order_comment", "broker_comment"
    )
    tokens = _all_ack_values(sources, "actual_owner_token", "owner_token")
    if expected.owner_token:
        for raw in comments:
            comment = _text(raw)
            if comment and not comment.startswith(expected.owner_token):
                reasons.append("ack_owner_comment_prefix_mismatch")
                break
        for raw in tokens:
            token = _text(raw)
            if token and not token.startswith(expected.owner_token):
                reasons.append("ack_owner_token_prefix_mismatch")
                break

    if _ticket_conflict(sources):
        reasons.append("ack_ticket_conflict")
    if expected.target_ticket is not None:
        reported_targets = {
            ticket
            for raw in _all_ack_values(sources, "actual_target_ticket", "target_ticket")
            if (ticket := _positive_int(raw)) is not None
        }
        if any(ticket != expected.target_ticket for ticket in reported_targets):
            reasons.append("ack_target_ticket_mismatch")
        if actual.ticket > 0 and actual.ticket != expected.target_ticket:
            reasons.append("ack_ticket_target_mismatch")
    return _unique(reasons)


def _successful_attestation_reasons(
    expected: _CommandExpectation,
    actual: ExecutionAckActuals,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not actual.command_id:
        reasons.append("ack_command_id_missing")
    if not expected.symbol:
        reasons.append("command_symbol_missing")
    if not actual.symbol:
        reasons.append("ack_symbol_missing")
    if not expected.broker_symbol:
        reasons.append("command_broker_symbol_missing")
    if not actual.broker_symbol:
        reasons.append("ack_broker_symbol_missing")
    if not actual.cmd:
        reasons.append("ack_cmd_missing")
    if expected.magic is None:
        reasons.append("command_magic_missing")
    if actual.magic <= 0:
        reasons.append("ack_magic_missing")
    if not expected.owner_token:
        reasons.append("command_owner_token_missing")
    if not actual.order_comment:
        reasons.append("ack_owner_comment_missing")
    elif expected.owner_token and not actual.order_comment.startswith(
        expected.owner_token
    ):
        reasons.append("ack_owner_comment_prefix_mismatch")
    if actual.ticket <= 0:
        reasons.append("ack_positive_ticket_missing")

    if expected.cmd in _ENTRY_COMMANDS:
        if actual.side != expected.cmd:
            reasons.append("ack_side_missing" if not actual.side else "ack_side_mismatch")
        if expected.execution_type != "market":
            reasons.append("command_execution_type_not_market")
        if not actual.execution_type:
            reasons.append("ack_execution_type_missing")
        elif actual.execution_type != "market":
            reasons.append("ack_execution_type_not_market")
        if expected.lots is None:
            reasons.append("command_lots_missing")
        if actual.lots is None:
            reasons.append("ack_actual_lots_missing")
        elif expected.lots is not None and not _numeric_equal(
            actual.lots, expected.lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_lots_mismatch")
        if expected.sl_price is None:
            reasons.append("command_sl_missing")
        if actual.sl_price is None:
            reasons.append("ack_actual_sl_missing")
        elif expected.sl_price is not None and not _numeric_equal(
            actual.sl_price, expected.sl_price, quantum=expected.tick_size
        ):
            reasons.append("ack_actual_sl_mismatch")
        if expected.tp_price is None:
            reasons.append("command_tp_missing")
        if actual.tp_price is None:
            reasons.append("ack_actual_tp_missing")
        elif expected.tp_price is not None and not _numeric_equal(
            actual.tp_price, expected.tp_price, quantum=expected.tick_size
        ):
            reasons.append("ack_actual_tp_mismatch")
        if expected.worst_fill_price is None:
            reasons.append("command_worst_fill_missing")
        if actual.open_price is None:
            reasons.append("ack_actual_open_price_missing")
        elif expected.worst_fill_price is not None:
            tolerance = max(
                1e-12,
                (expected.tick_size or 0.0) * 1e-6,
                expected.worst_fill_price * 1e-12,
            )
            if expected.cmd == "BUY" and actual.open_price > expected.worst_fill_price + tolerance:
                reasons.append("ack_actual_open_price_exceeds_buy_bound")
            if expected.cmd == "SELL" and actual.open_price < expected.worst_fill_price - tolerance:
                reasons.append("ack_actual_open_price_exceeds_sell_bound")
        if actual.remaining_lots is None:
            reasons.append("ack_actual_remaining_lots_missing")
        elif expected.lots is not None and not _numeric_equal(
            actual.remaining_lots, expected.lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_remaining_lots_mismatch")
        if actual.close_time is None:
            reasons.append("ack_actual_close_time_missing")
        elif actual.close_time != 0.0:
            reasons.append("ack_actual_close_time_not_open")

    if expected.cmd in _MANAGEMENT_COMMANDS:
        if expected.target_ticket is None:
            reasons.append("command_target_ticket_missing")
        if actual.target_ticket <= 0:
            reasons.append("ack_target_ticket_missing")
        elif expected.target_ticket is not None and actual.target_ticket != expected.target_ticket:
            reasons.append("ack_target_ticket_mismatch")

    if expected.cmd == "CLOSE_PARTIAL":
        if expected.lots is None:
            reasons.append("command_close_lots_missing")
        if actual.lots is None:
            reasons.append("ack_actual_close_lots_missing")
        elif expected.lots is not None and not _numeric_equal(
            actual.lots, expected.lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_close_lots_mismatch")
        if expected.target_lots is None:
            reasons.append("command_target_lots_missing")
        if actual.remaining_lots is None:
            reasons.append("ack_actual_remaining_lots_missing")
        elif expected.target_lots is not None and expected.lots is not None:
            expected_remaining = max(0.0, expected.target_lots - expected.lots)
            if not _numeric_equal(
                actual.remaining_lots,
                expected_remaining,
                quantum=expected.lot_step,
            ):
                reasons.append("ack_actual_remaining_lots_mismatch")
            if expected_remaining > 0.0:
                if actual.close_time is None:
                    reasons.append("ack_actual_close_time_missing")
                elif actual.close_time != 0.0:
                    reasons.append("ack_actual_close_time_not_open")
            elif actual.close_time is None or actual.close_time <= 0.0:
                reasons.append("ack_actual_close_time_missing")

    if expected.cmd == "CLOSE":
        if expected.target_lots is None:
            reasons.append("command_target_lots_missing")
        if actual.lots is None:
            reasons.append("ack_actual_close_lots_missing")
        elif expected.target_lots is not None and not _numeric_equal(
            actual.lots, expected.target_lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_close_lots_mismatch")
        if actual.remaining_lots is None:
            reasons.append("ack_actual_remaining_lots_missing")
        elif actual.remaining_lots != 0.0:
            reasons.append("ack_actual_remaining_lots_not_zero")
        if actual.close_time is None or actual.close_time <= 0.0:
            reasons.append("ack_actual_close_time_missing")

    if expected.cmd == "MODIFY_SL":
        if expected.sl_price is None:
            reasons.append("command_sl_missing")
        if actual.sl_price is None:
            reasons.append("ack_actual_sl_missing")
        elif expected.sl_price is not None and not _numeric_equal(
            actual.sl_price, expected.sl_price, quantum=expected.tick_size
        ):
            reasons.append("ack_actual_sl_mismatch")
        if expected.target_lots is None:
            reasons.append("command_target_lots_missing")
        if actual.remaining_lots is None:
            reasons.append("ack_actual_remaining_lots_missing")
        elif expected.target_lots is not None and not _numeric_equal(
            actual.remaining_lots,
            expected.target_lots,
            quantum=expected.lot_step,
        ):
            reasons.append("ack_actual_remaining_lots_mismatch")
        if actual.close_time is None:
            reasons.append("ack_actual_close_time_missing")
        elif actual.close_time != 0.0:
            reasons.append("ack_actual_close_time_not_open")

    if expected.target_open_price is not None and actual.open_price is not None:
        if not _numeric_equal(
            actual.open_price,
            expected.target_open_price,
            quantum=expected.tick_size,
        ):
            reasons.append("ack_actual_open_price_mismatch")
    if (
        expected.target_lots is not None
        and actual.lots is not None
        and expected.cmd not in {"CLOSE", "CLOSE_PARTIAL"}
    ):
        if not _numeric_equal(actual.lots, expected.target_lots, quantum=expected.lot_step):
            reasons.append("ack_actual_target_lots_mismatch")
    if expected.expected_tp_price is not None:
        if actual.tp_price is None:
            reasons.append("ack_actual_tp_missing")
        elif not _numeric_equal(
            actual.tp_price, expected.expected_tp_price, quantum=expected.tick_size
        ):
            reasons.append("ack_actual_tp_mismatch")
    return _unique(reasons)


def _reported_economic_mismatch_reasons(
    expected: _CommandExpectation,
    actual: ExecutionAckActuals,
) -> tuple[str, ...]:
    """Reject contradictory broker actuals even on a reported failure."""

    reasons: list[str] = []
    if expected.cmd in _ENTRY_COMMANDS:
        if actual.execution_type and actual.execution_type != "market":
            reasons.append("ack_execution_type_not_market")
        if actual.lots is not None and expected.lots is not None and not _numeric_equal(
            actual.lots, expected.lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_lots_mismatch")
        if (
            actual.sl_price is not None
            and expected.sl_price is not None
            and not _numeric_equal(
                actual.sl_price, expected.sl_price, quantum=expected.tick_size
            )
        ):
            reasons.append("ack_actual_sl_mismatch")
        if (
            actual.tp_price is not None
            and expected.tp_price is not None
            and not _numeric_equal(
                actual.tp_price, expected.tp_price, quantum=expected.tick_size
            )
        ):
            reasons.append("ack_actual_tp_mismatch")
        if actual.open_price is not None and expected.worst_fill_price is not None:
            tolerance = max(
                1e-12,
                (expected.tick_size or 0.0) * 1e-6,
                expected.worst_fill_price * 1e-12,
            )
            if expected.cmd == "BUY" and actual.open_price > expected.worst_fill_price + tolerance:
                reasons.append("ack_actual_open_price_exceeds_buy_bound")
            if expected.cmd == "SELL" and actual.open_price < expected.worst_fill_price - tolerance:
                reasons.append("ack_actual_open_price_exceeds_sell_bound")

    if expected.cmd == "CLOSE_PARTIAL":
        if actual.lots is not None and expected.lots is not None and not _numeric_equal(
            actual.lots, expected.lots, quantum=expected.lot_step
        ):
            reasons.append("ack_actual_close_lots_mismatch")
    if expected.cmd == "CLOSE":
        if (
            actual.lots is not None
            and expected.target_lots is not None
            and not _numeric_equal(
                actual.lots, expected.target_lots, quantum=expected.lot_step
            )
        ):
            reasons.append("ack_actual_close_lots_mismatch")
    if expected.cmd == "MODIFY_SL":
        if (
            actual.sl_price is not None
            and expected.sl_price is not None
            and not _numeric_equal(
                actual.sl_price, expected.sl_price, quantum=expected.tick_size
            )
        ):
            reasons.append("ack_actual_sl_mismatch")
    if (
        expected.target_open_price is not None
        and actual.open_price is not None
        and not _numeric_equal(
            actual.open_price,
            expected.target_open_price,
            quantum=expected.tick_size,
        )
    ):
        reasons.append("ack_actual_open_price_mismatch")
    if (
        expected.expected_tp_price is not None
        and actual.tp_price is not None
        and not _numeric_equal(
            actual.tp_price,
            expected.expected_tp_price,
            quantum=expected.tick_size,
        )
    ):
        reasons.append("ack_actual_tp_mismatch")
    return _unique(reasons)


def classify_execution_ack(
    command: Mapping[str, Any],
    ack: Mapping[str, Any],
) -> ExecutionAckAttestation:
    """Return the only safe durable status for an execution acknowledgement.

    ``failed`` and ``duplicate`` are terminal only for an explicit ticketless
    ``not_attempted`` mutation state.  Successful mutating commands require a
    positive broker ticket, ``confirmed`` mutation state, and exact applicable
    broker actuals.  This means an EA/network crash after an OrderSend,
    OrderClose, or OrderModify call can never be mistaken for a harmless
    refusal.
    """

    expected = _expectation(command)
    sources = _ack_sources(ack)
    reported_status = _text(
        _first_ack_value(sources, "reported_status", "status", "ack_status")
    )
    normalized_status = _normalize_status(reported_status.lower())
    mutation_state, mutation_reasons = _mutation_state(sources)
    actual = _actuals(sources)
    broker_mutating = expected.cmd in _BROKER_MUTATING_COMMANDS
    reasons = list(mutation_reasons)
    reasons.extend(_identity_reasons(expected, actual, sources))
    reasons.extend(_reported_economic_mismatch_reasons(expected, actual))

    observed_statuses = {
        canonical
        for raw in _all_ack_values(sources, "reported_status", "status", "ack_status")
        if (canonical := _normalize_status(_lower(raw))) is not None
    }
    if len(observed_statuses) > 1:
        reasons.append("ack_status_conflict")

    explicit_reconcile = bool(
        normalized_status == "reconcile_required"
        or _has_true(
            sources,
            "reconcile_required",
            "execution_uncertain",
            "broker_outcome_unknown",
        )
    )
    if explicit_reconcile:
        reasons.append("ack_explicit_reconcile_required")
    if normalized_status is None:
        reasons.append("ack_status_missing" if not reported_status else "ack_status_unsupported")
    if not expected.command_id:
        reasons.append("command_id_missing")
    if not expected.cmd:
        reasons.append("command_cmd_missing")
    elif expected.cmd not in _BROKER_MUTATING_COMMANDS and expected.cmd != "INFO":
        reasons.append("command_cmd_unsupported_for_attestation")

    if broker_mutating:
        if actual.ticket > 0 and mutation_state == "not_attempted":
            reasons.append("ack_ticket_conflicts_with_not_attempted")
        if normalized_status == "failed" and actual.ticket > 0:
            reasons.append("ack_failed_with_positive_ticket")
        if normalized_status == "duplicate" and actual.ticket > 0:
            reasons.append("ack_duplicate_with_positive_ticket")

        if normalized_status == "acked":
            if mutation_state != "confirmed":
                reasons.append("ack_mutation_not_confirmed")
            reasons.extend(_explicit_actual_reasons(expected, ack))
            reasons.extend(_successful_attestation_reasons(expected, actual))
        elif normalized_status in {"failed", "duplicate"}:
            if mutation_state != "not_attempted":
                reasons.append("ack_not_conclusive_pre_mutation_refusal")
        elif normalized_status == "delivered":
            if mutation_state != "not_attempted":
                reasons.append("ack_unknown_post_mutation_outcome")
        elif mutation_state in {"attempted", "confirmed", "unknown"}:
            reasons.append("ack_unknown_post_mutation_outcome")

        if mutation_state in {"attempted", "unknown"}:
            reasons.append("ack_unknown_post_mutation_outcome")

    reasons_tuple = _unique(reasons)
    if explicit_reconcile or normalized_status is None or reasons_tuple:
        effective_status: EffectiveAckStatus = "reconcile_required"
    else:
        assert normalized_status is not None
        effective_status = normalized_status

    attested = bool(
        effective_status == "acked"
        and (not broker_mutating or mutation_state == "confirmed")
    )
    terminal = effective_status in {"acked", "failed", "duplicate"}
    return ExecutionAckAttestation(
        effective_status=effective_status,
        reported_status=reported_status,
        mutation_state=mutation_state,
        broker_mutating=broker_mutating,
        attested=attested,
        terminal=terminal,
        reasons=reasons_tuple,
        actuals=actual,
    )


__all__ = [
    "EXECUTION_ACK_ATTESTATION_SCHEMA",
    "MT4_ORDER_ACTUALS_SCHEMA",
    "EffectiveAckStatus",
    "ExecutionAckActuals",
    "ExecutionAckAttestation",
    "classify_execution_ack",
]
