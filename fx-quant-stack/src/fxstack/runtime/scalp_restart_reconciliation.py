# AGENT: ROLE: Pure restart join for production-scalper broker positions and durable commands.
# AGENT: ENTRYPOINT: `reconcile_scalp_restart`.
# AGENT: PRIMARY INPUTS: authoritative MT4 positions snapshot, durable command rows, current scalp authority.
# AGENT: PRIMARY OUTPUTS: immutable occupancy, exact owned-position joins, pending/confirmed lifecycle evidence.
# AGENT: STATE / SIDE EFFECTS: none; no settings, I/O, service, store, runner, or research access.
"""Rebuild exact production-scalper ownership after a runtime restart.

The MT4 broker snapshot owns occupancy. Durable command rows may enrich an
open position with lifecycle authority only when the broker-observed ticket,
Magic, and order comment join to exactly one internally coherent historical
entry authority. Nothing in this module infers ownership from a symbol alone
or requires an old position to match the singleton lease for new entries.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
import math
import re
from typing import Any, Literal, cast

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_CATALOG,
    IG_MT4_SCALP_SYMBOLS,
)
from fxstack.runtime.scalp_execution_authority import (
    SCALP_ENTRY_INTENT,
    SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA,
    SCALP_EXECUTION_LANE,
    authority_error,
    build_active_authority,
    command_binding_error,
    command_binding_fields,
    expectation_from_authority,
    expectation_from_command,
    protective_authority_from_entry_payload,
    protective_authority_structure_error,
    protective_command_binding_fields,
    protective_history_binding_error,
)


SCALP_RESTART_RECONCILIATION_SCHEMA = (
    "fxstack.runtime.scalp_restart_reconciliation.v1"
)
MT4_POSITIONS_SNAPSHOT_SCHEMA = "fxstack_mt4_positions_snapshot_v2"
TICKET_OWNER_CONTRACT: Literal["ticket_owner_v1"] = "ticket_owner_v1"

_OWNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,31}$")
_ENTRY_COMMANDS = frozenset({"BUY", "SELL"})
_MANAGEMENT_COMMANDS = frozenset({"CLOSE", "CLOSE_PARTIAL", "MODIFY_SL"})
_ACTIVE_STATUSES = frozenset({"queued", "delivered"})
_KNOWN_STATUSES = frozenset(
    {
        "queued",
        "delivered",
        "acked",
        "failed",
        "expired",
        "duplicate",
        "reconcile_required",
    }
)
_SUCCESS_ACK_STATUSES = frozenset(
    {"acked", "ok", "success", "done", "executed", "filled"}
)
_MAPPING_FIELDS = (
    "symbol",
    "broker_symbol",
    "side",
    "ticket",
    "lots",
    "magic",
    "order_comment",
    "owner_token",
    "ownership_contract",
    "open_price",
    "open_time",
    "sl",
    "tp",
    "profit",
    "entry_command_id",
    "entry_authority_binding",
)


@dataclass(frozen=True, slots=True)
class ScalpRestartOwnedPosition(Mapping[str, Any]):
    """One broker position joined to its exact durable entry identity.

    The type implements ``Mapping`` so it can be passed directly to the pure
    production scalp lifecycle evaluator without converting it to a mutable
    dictionary. ``entry_authority_binding`` is a frozen projection of the
    historical ``expected_strategy_*`` fields used for protective management.
    """

    symbol: str
    broker_symbol: str
    side: Literal["BUY", "SELL"]
    ticket: int
    lots: float
    magic: int
    order_comment: str
    owner_token: str
    ownership_contract: Literal["ticket_owner_v1"]
    open_price: float | None
    open_time: float | None
    sl: float | None
    tp: float | None
    profit: float | None
    entry_command_id: str
    entry_authority_binding: tuple[tuple[str, Any], ...]

    def __getitem__(self, key: str) -> Any:
        if key not in _MAPPING_FIELDS:
            raise KeyError(key)
        return getattr(self, key)

    def __iter__(self) -> Iterator[str]:
        return iter(_MAPPING_FIELDS)

    def __len__(self) -> int:
        return len(_MAPPING_FIELDS)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpRestartIssue:
    """Deterministic evidence explaining an omitted or quarantined row."""

    scope: Literal["input", "authority", "snapshot", "position", "command"]
    reason: str
    symbol: str | None = None
    ticket: int | None = None
    command_id: str | None = None
    quarantines_entries: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpRestartReconciliationResult:
    """Immutable restart occupancy, ownership, and queue reconciliation."""

    authoritative_open_symbols: tuple[str, ...]
    owned_positions: tuple[ScalpRestartOwnedPosition, ...]
    active_queued_entry_symbols: tuple[str, ...]
    active_exit_tickets: tuple[int, ...]
    broker_confirmed_exit_symbols: tuple[str, ...]
    unmatched: tuple[ScalpRestartIssue, ...]
    unmatched_reasons: tuple[str, ...]
    quarantine_reasons: tuple[str, ...]
    entry_admission_ready: bool
    positions_snapshot_token: str
    positions_snapshot_received_at: float | None
    schema_version: str = SCALP_RESTART_RECONCILIATION_SCHEMA

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _PreparedPosition:
    raw: Mapping[str, Any]
    symbol: str | None
    side: str | None
    ticket: int | None
    lots: float | None
    magic: int | None
    order_comment: str | None
    reasons: list[str]


@dataclass(slots=True)
class _PreparedCommand:
    raw: Mapping[str, Any]
    command_id: str | None
    cmd: str | None
    symbol: str | None
    magic: int | None
    status: str | None
    payload: Mapping[str, Any] | None
    ack: Mapping[str, Any] | None
    ack_container_invalid: bool
    relevant: bool


def _strict_positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return int(value)


def _strict_positive_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0.0:
        return None
    return number


def _strict_finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _optional_finite_float(value: Any) -> float | None:
    if value is None:
        return None
    return _strict_finite_float(value)


def _exact_string(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text if text else None


def _exact_symbol(value: Any) -> str | None:
    text = _exact_string(value)
    if text is None or text != value or text not in IG_MT4_SCALP_CATALOG:
        return None
    return text


def _owner_token(value: Any) -> str | None:
    text = _exact_string(value)
    if text is None or text != value or _OWNER_TOKEN_RE.fullmatch(text) is None:
        return None
    return text


def _order_comment(value: Any) -> str | None:
    text = _exact_string(value)
    if (
        text is None
        or text != value
        or len(text) > 31
        or any(character in text for character in ("\x00", "\r", "\n"))
    ):
        return None
    return text


def _order_comment_has_owner_prefix(comment: str, owner_token: str) -> bool:
    return bool(owner_token and comment.startswith(owner_token))


def _mapping_sequence(value: Any) -> bool:
    return bool(
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes, bytearray, Mapping))
    )


def _catalog_order(values: set[str]) -> tuple[str, ...]:
    return tuple(symbol for symbol in IG_MT4_SCALP_SYMBOLS if symbol in values)


def _issue_sort_key(
    issue: ScalpRestartIssue,
) -> tuple[str, str, int, str, str, int]:
    return (
        issue.scope,
        issue.symbol or "\uffff",
        issue.ticket if issue.ticket is not None else 2**63 - 1,
        issue.command_id or "\uffff",
        issue.reason,
        0 if issue.quarantines_entries else 1,
    )


def _dedupe_issues(
    issues: Sequence[ScalpRestartIssue],
) -> tuple[ScalpRestartIssue, ...]:
    return tuple(sorted(set(issues), key=_issue_sort_key))


def _prepare_position(raw: Mapping[str, Any], *, expected_magic: int | None) -> _PreparedPosition:
    reasons: list[str] = []

    symbol = _exact_symbol(raw.get("symbol"))
    if symbol is None:
        reasons.append("position_symbol_invalid")

    raw_side = raw.get("side")
    side = (
        raw_side
        if isinstance(raw_side, str) and raw_side in _ENTRY_COMMANDS
        else None
    )
    if side is None:
        reasons.append("position_side_invalid")

    ticket = _strict_positive_int(raw.get("ticket"))
    if ticket is None:
        reasons.append("position_ticket_invalid")

    lots = _strict_positive_float(raw.get("lots"))
    if lots is None:
        reasons.append("position_lots_invalid")

    magic = _strict_positive_int(raw.get("magic"))
    if magic is None:
        reasons.append("position_magic_invalid")
    elif expected_magic is not None and magic != expected_magic:
        reasons.append("position_magic_mismatch")

    comment = _order_comment(raw.get("order_comment"))
    if comment is None:
        reasons.append("position_order_comment_invalid")

    return _PreparedPosition(
        raw=raw,
        symbol=symbol,
        side=side,
        ticket=ticket,
        lots=lots,
        magic=magic,
        order_comment=comment,
        reasons=reasons,
    )


def _command_is_relevant(
    raw: Mapping[str, Any],
    payload: Mapping[str, Any] | None,
) -> bool:
    candidates: tuple[Any, ...] = (
        raw.get("intent"),
        payload.get("intent") if payload is not None else None,
    )
    if any(
        str(value or "").strip().lower() == SCALP_ENTRY_INTENT
        for value in candidates
    ):
        return True
    if payload is None:
        return False
    if str(payload.get("management_strategy") or "").strip():
        return True
    if (
        str(payload.get("strategy_lane") or "").strip().lower()
        == SCALP_EXECUTION_LANE
    ):
        return True
    return any(str(key).startswith("expected_strategy_") for key in payload)


def _prepare_command(raw: Mapping[str, Any]) -> _PreparedCommand:
    raw_payload = raw.get("payload_json")
    payload = raw_payload if isinstance(raw_payload, Mapping) else None
    raw_ack = raw.get("ack_json")
    ack: Mapping[str, Any] | None = None
    ack_container_invalid = False
    if isinstance(raw_ack, Mapping):
        if raw_ack:
            ack = raw_ack
    elif raw_ack is not None:
        ack_container_invalid = True

    raw_command_id = raw.get("command_id")
    command_id = (
        raw_command_id.strip()
        if isinstance(raw_command_id, str) and raw_command_id.strip()
        else None
    )
    raw_cmd = raw.get("cmd")
    cmd = (
        raw_cmd
        if isinstance(raw_cmd, str)
        and raw_cmd in {*_ENTRY_COMMANDS, *_MANAGEMENT_COMMANDS}
        else None
    )
    symbol = _exact_symbol(raw.get("symbol"))
    magic = _strict_positive_int(raw.get("magic"))
    raw_status = raw.get("status")
    status = (
        raw_status.strip().lower()
        if isinstance(raw_status, str) and raw_status.strip()
        else None
    )
    return _PreparedCommand(
        raw=raw,
        command_id=command_id,
        cmd=cmd,
        symbol=symbol,
        magic=magic,
        status=status,
        payload=payload,
        ack=ack,
        ack_container_invalid=ack_container_invalid,
        relevant=_command_is_relevant(raw, payload),
    )


def _payload_positive_int(
    payload: Mapping[str, Any] | None,
    key: str,
) -> int | None:
    return _strict_positive_int(payload.get(key)) if payload is not None else None


def _payload_exact_symbol(payload: Mapping[str, Any] | None) -> str | None:
    return _exact_symbol(payload.get("symbol")) if payload is not None else None


def _payload_owner_token(payload: Mapping[str, Any] | None) -> str | None:
    return _owner_token(payload.get("owner_token")) if payload is not None else None


def _base_command_reasons(
    command: _PreparedCommand,
    *,
    expected_magic: int,
) -> list[str]:
    reasons: list[str] = []
    if command.command_id is None:
        reasons.append("command_id_invalid")
    if command.payload is None:
        reasons.append("command_payload_invalid")
    if command.symbol is None:
        reasons.append("command_symbol_invalid")
    if command.magic is None:
        reasons.append("command_magic_invalid")
    elif command.magic != expected_magic:
        reasons.append("command_magic_mismatch")
    if command.status not in _KNOWN_STATUSES:
        reasons.append("command_status_invalid")
    if command.ack_container_invalid:
        reasons.append("command_ack_invalid")
    return reasons


def _authority_binding_reasons(
    payload: Mapping[str, Any] | None,
    *,
    authority: Mapping[str, Any],
    entry: bool,
    symbol: str | None,
) -> list[str]:
    if payload is None:
        return []
    if entry:
        error = command_binding_error(
            dict(payload),
            authority=dict(authority),
            symbol=str(symbol or ""),
        )
        return [error] if error else []

    reasons: list[str] = []
    expected = command_binding_fields(dict(authority))
    for field, expected_value in expected.items():
        if not field.startswith("expected_strategy_"):
            continue
        observed = str(payload.get(field) or "").strip().lower()
        wanted = str(expected_value or "").strip().lower()
        if not observed:
            reasons.append(f"{field}_missing")
        elif observed != wanted:
            reasons.append(f"{field}_changed")
    return reasons


def _entry_contract_reasons(
    command: _PreparedCommand,
    *,
    authority: Mapping[str, Any] | None,
    expected_magic: int,
    duplicate_command_id: bool,
) -> list[str]:
    reasons = _base_command_reasons(command, expected_magic=expected_magic)
    if command.cmd not in _ENTRY_COMMANDS:
        reasons.append("entry_command_type_invalid")
    payload = command.payload
    if payload is not None:
        if payload.get("cmd") != command.cmd:
            reasons.append("entry_command_payload_cmd_mismatch")
        if _payload_exact_symbol(payload) != command.symbol:
            reasons.append("entry_command_payload_symbol_mismatch")
        if _payload_positive_int(payload, "magic") != command.magic:
            reasons.append("entry_command_payload_magic_mismatch")
        if _payload_owner_token(payload) is None:
            reasons.append("entry_command_owner_token_invalid")
        if payload.get("ownership_contract") != TICKET_OWNER_CONTRACT:
            reasons.append("entry_command_ownership_contract_invalid")
        row_intent = str(command.raw.get("intent") or "").strip().lower()
        if row_intent and row_intent != SCALP_ENTRY_INTENT:
            reasons.append("entry_command_row_intent_mismatch")
    if authority is not None:
        reasons.extend(
            _authority_binding_reasons(
                payload,
                authority=authority,
                entry=True,
                symbol=command.symbol,
            )
        )
    if duplicate_command_id:
        reasons.append("command_id_duplicate")
    return list(dict.fromkeys(reasons))


def _management_contract_reasons(
    command: _PreparedCommand,
    *,
    historical_entry_payload: Mapping[str, Any] | None,
    managed_entry_command_id: str | None,
    expected_magic: int,
    duplicate_command_id: bool,
) -> list[str]:
    reasons = _base_command_reasons(command, expected_magic=expected_magic)
    if command.cmd not in _MANAGEMENT_COMMANDS:
        reasons.append("exit_command_type_invalid")
    payload = command.payload
    if payload is not None:
        if payload.get("cmd") != command.cmd:
            reasons.append("exit_command_payload_cmd_mismatch")
        if _payload_exact_symbol(payload) != command.symbol:
            reasons.append("exit_command_payload_symbol_mismatch")
        if _payload_positive_int(payload, "magic") != command.magic:
            reasons.append("exit_command_payload_magic_mismatch")
        if _payload_positive_int(payload, "target_ticket") is None:
            reasons.append("exit_command_target_ticket_invalid")
        if _payload_owner_token(payload) is None:
            reasons.append("exit_command_owner_token_invalid")
        if payload.get("ownership_contract") != TICKET_OWNER_CONTRACT:
            reasons.append("exit_command_ownership_contract_invalid")
        observed_entry_command_id = str(
            payload.get("managed_entry_command_id") or ""
        ).strip()
        if not observed_entry_command_id:
            reasons.append("exit_command_managed_entry_command_id_missing")
        elif observed_entry_command_id != str(
            managed_entry_command_id or ""
        ).strip():
            reasons.append("exit_command_managed_entry_command_id_mismatch")
        if (
            historical_entry_payload is not None
            and str(payload.get("management_strategy") or "").strip()
            != str(
                historical_entry_payload.get("expected_strategy_id") or ""
            ).strip()
        ):
            reasons.append("exit_command_management_strategy_invalid")
    if historical_entry_payload is None:
        reasons.append("exit_command_historical_entry_binding_missing")
    elif payload is not None:
        binding_error = protective_history_binding_error(
            dict(historical_entry_payload),
            close_payload=dict(payload),
            symbol=str(command.symbol or ""),
        )
        if binding_error:
            reasons.append(str(binding_error))
    if duplicate_command_id:
        reasons.append("command_id_duplicate")
    return list(dict.fromkeys(reasons))


def _ack_identity_reasons(
    command: _PreparedCommand,
    *,
    ticket: int,
    symbol: str,
    magic: int,
    owner_token: str,
    require_success: bool,
) -> list[str]:
    ack = command.ack
    if ack is None:
        return ["successful_exit_ack_missing"] if require_success else []

    reasons: list[str] = []
    if require_success and (
        str(ack.get("status") or "").strip().lower()
        not in _SUCCESS_ACK_STATUSES
    ):
        reasons.append("successful_exit_ack_status_invalid")
    ack_command_id = ack.get("command_id")
    if ack_command_id is not None and str(ack_command_id).strip() != str(
        command.command_id or ""
    ):
        reasons.append("command_ack_command_id_mismatch")
    if "symbol" in ack and str(ack.get("symbol") or "").strip().upper() != symbol:
        reasons.append("command_ack_symbol_mismatch")
    if "ticket" in ack and _strict_positive_int(ack.get("ticket")) != ticket:
        reasons.append("command_ack_ticket_mismatch")
    if "magic" in ack and _strict_positive_int(ack.get("magic")) != magic:
        reasons.append("command_ack_magic_mismatch")
    if "owner_token" in ack and _owner_token(ack.get("owner_token")) != owner_token:
        reasons.append("command_ack_owner_token_mismatch")
    return reasons


def _loose_entry_identity_matches(
    command: _PreparedCommand,
    position: _PreparedPosition,
) -> bool:
    if command.cmd not in _ENTRY_COMMANDS or command.payload is None:
        return False
    token = _payload_owner_token(command.payload)
    ack_ticket = (
        _strict_positive_int(command.ack.get("ticket"))
        if command.ack is not None and "ticket" in command.ack
        else None
    )
    return bool(
        (
            token is not None
            and position.order_comment is not None
            and _order_comment_has_owner_prefix(position.order_comment, token)
        )
        or (ack_ticket is not None and ack_ticket == position.ticket)
    )


def _entry_matches_position(
    command: _PreparedCommand,
    position: _PreparedPosition,
) -> bool:
    if command.payload is None:
        return False
    return bool(
        command.cmd == position.side
        and command.symbol == position.symbol
        and command.magic == position.magic
        and _payload_exact_symbol(command.payload) == position.symbol
        and _payload_positive_int(command.payload, "magic") == position.magic
        and (
            (token := _payload_owner_token(command.payload)) is not None
            and position.order_comment is not None
            and _order_comment_has_owner_prefix(position.order_comment, token)
        )
    )


def _owned_position(
    position: _PreparedPosition,
    *,
    entry_command_id: str,
    entry_authority_binding: tuple[tuple[str, Any], ...],
    owner_token: str,
) -> ScalpRestartOwnedPosition:
    assert position.symbol is not None
    assert position.side in _ENTRY_COMMANDS
    assert position.ticket is not None
    assert position.lots is not None
    assert position.magic is not None
    assert position.order_comment is not None
    return ScalpRestartOwnedPosition(
        symbol=position.symbol,
        broker_symbol=str(position.raw.get("broker_symbol") or "").strip(),
        side=cast(Literal["BUY", "SELL"], position.side),
        ticket=position.ticket,
        lots=position.lots,
        magic=position.magic,
        order_comment=position.order_comment,
        owner_token=owner_token,
        ownership_contract=TICKET_OWNER_CONTRACT,
        open_price=_optional_finite_float(position.raw.get("open_price")),
        open_time=_optional_finite_float(position.raw.get("open_time")),
        sl=_optional_finite_float(position.raw.get("sl")),
        tp=_optional_finite_float(position.raw.get("tp")),
        profit=_optional_finite_float(position.raw.get("profit")),
        entry_command_id=entry_command_id,
        entry_authority_binding=entry_authority_binding,
    )


def _position_sort_key(
    position: _PreparedPosition,
) -> tuple[int, str, int, tuple[str, ...]]:
    symbol_index = (
        IG_MT4_SCALP_SYMBOLS.index(position.symbol)
        if position.symbol in IG_MT4_SCALP_CATALOG
        else len(IG_MT4_SCALP_SYMBOLS)
    )
    return (
        symbol_index,
        position.symbol or "\uffff",
        position.ticket if position.ticket is not None else 2**63 - 1,
        tuple(position.reasons),
    )


def _command_sort_key(
    command: _PreparedCommand,
) -> tuple[str, str, str, int]:
    return (
        command.symbol or "\uffff",
        command.cmd or "\uffff",
        command.command_id or "\uffff",
        _payload_positive_int(command.payload, "target_ticket") or 2**63 - 1,
    )


def _snapshot_contract(
    state: Mapping[str, Any],
    *,
    now_epoch: float | None,
    max_snapshot_age_secs: float | None,
    expected_account_scope: str | None,
) -> tuple[list[ScalpRestartIssue], str, float | None, Sequence[Any] | None]:
    issues: list[ScalpRestartIssue] = []
    if state.get("positions_snapshot_authoritative") is not True:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_not_authoritative"))
    if state.get("positions_snapshot_source") != "positions_snapshot":
        issues.append(ScalpRestartIssue("snapshot", "snapshot_source_invalid"))
    if state.get("positions_snapshot_schema") != MT4_POSITIONS_SNAPSHOT_SCHEMA:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_schema_invalid"))
    if state.get("positions_snapshot_contract_current") is not True:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_contract_not_current"))

    token = _exact_string(state.get("positions_snapshot_token")) or ""
    if not token:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_token_missing"))

    observed_scope = _exact_string(state.get("positions_snapshot_account_scope"))
    if observed_scope is None:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_account_scope_missing"))
    elif expected_account_scope is not None and observed_scope != expected_account_scope:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_account_scope_mismatch"))

    received_at = _strict_positive_float(state.get("positions_snapshot_received_at"))
    if received_at is None:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_received_at_invalid"))
    elif now_epoch is not None and received_at > now_epoch:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_received_at_future"))
    elif (
        now_epoch is not None
        and max_snapshot_age_secs is not None
        and now_epoch - received_at > max_snapshot_age_secs
    ):
        issues.append(ScalpRestartIssue("snapshot", "snapshot_stale"))

    raw_positions = state.get("positions")
    positions: Sequence[Any] | None = (
        raw_positions if _mapping_sequence(raw_positions) else None
    )
    if positions is None:
        issues.append(ScalpRestartIssue("snapshot", "snapshot_positions_invalid"))
    return issues, token, received_at, positions


def _management_authority_error(authority: Mapping[str, Any]) -> str:
    """Validate durable ownership identity without requiring entry authority.

    A safety revocation or certificate expiry must stop new entries, but it
    cannot erase the immutable generation identity needed to manage positions
    that generation already opened.  Reuse the canonical structural validator
    at a synthetic pre-expiry instant after admitting only active/revoked
    durable states; status and wall-clock expiry remain entry-only checks.
    """

    status = str(authority.get("status") or "").strip().lower()
    if str(authority.get("schema_version") or "") == (
        SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA
    ):
        if status != "revoked":
            return "scalp_management_legacy_authority_not_revoked"
        return protective_authority_structure_error(dict(authority))
    if status not in {"active", "revoked"}:
        return "scalp_management_authority_status_invalid"
    expiry = _strict_positive_float(authority.get("validation_expires_at_epoch"))
    if expiry is None:
        return "scalp_authority_validation_expiry_invalid"
    structural = dict(authority)
    structural["status"] = "active"
    return authority_error(
        structural,
        expectation=expectation_from_authority(structural),
        now_epoch=expiry / 2.0,
    )


def _historical_entry_authority(
    command: _PreparedCommand,
) -> tuple[dict[str, Any] | None, tuple[tuple[str, Any], ...], list[str]]:
    """Rebuild and validate the complete authority that admitted an entry.

    Runtime boot, authority revision, strategy generation, engine, config, and
    certificate may all have changed since the position opened. The durable
    entry payload therefore supplies every variable authority field, while
    ``build_active_authority`` supplies only immutable production constants.
    The reconstructed hash must equal the binding durably stamped at enqueue.
    """

    if command.payload is None:
        return None, (), []
    payload = dict(command.payload)
    if str(payload.get("expected_strategy_authority_schema") or "") == (
        SCALP_LEGACY_EXECUTION_AUTHORITY_SCHEMA
    ):
        try:
            historical = protective_authority_from_entry_payload(payload)
            management_fields = protective_command_binding_fields(payload)
        except (TypeError, ValueError, OverflowError) as exc:
            reason = str(exc).strip() or "scalp_protective_authority_invalid"
            return None, (), [reason]
        management_binding = tuple(
            (field, value)
            for field, value in management_fields.items()
            if field.startswith("expected_strategy_")
        )
        return historical, management_binding, []
    try:
        expectation = expectation_from_command(payload)
        expiry = _strict_positive_float(
            expectation.validation_expires_at_epoch
        )
        if expiry is None:
            return None, (), ["scalp_authority_validation_expiry_invalid"]
        historical = build_active_authority(
            expectation,
            activated_at=expiry / 2.0,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        reason = str(exc).strip() or "scalp_protective_authority_invalid"
        return None, (), [reason]

    observed_binding = str(
        payload.get("expected_strategy_binding_sha256") or ""
    ).strip().lower()
    if observed_binding != str(historical.get("binding_sha256") or ""):
        return None, (), ["scalp_protective_history_binding_invalid"]
    historical["status"] = "revoked"
    structure_error = protective_authority_structure_error(historical)
    if structure_error:
        return None, (), [str(structure_error)]
    binding_error = command_binding_error(
        payload,
        authority=historical,
        symbol=str(command.symbol or ""),
    )
    if binding_error:
        return None, (), [str(binding_error)]
    management_binding = tuple(
        (field, value)
        for field, value in command_binding_fields(historical).items()
        if field.startswith("expected_strategy_")
    )
    return historical, management_binding, []


def _owned_historical_entry_payload(
    position: ScalpRestartOwnedPosition,
) -> dict[str, Any]:
    """Project the validated entry identity needed by protective CLOSE checks."""

    return {
        **dict(position.entry_authority_binding),
        "strategy_lane": SCALP_EXECUTION_LANE,
        "intent": SCALP_ENTRY_INTENT,
        "symbol": position.symbol,
    }


def reconcile_scalp_restart(
    *,
    state_snapshot: Mapping[str, Any],
    durable_command_rows: Sequence[Mapping[str, Any]],
    production_scalp_authority: Mapping[str, Any],
    now_epoch: Any,
    max_snapshot_age_secs: Any,
    expected_magic: Any,
    expected_account_scope: Any,
) -> ScalpRestartReconciliationResult:
    """Join current broker positions to exact production-scalper commands.

    Invalid rows fail new-entry admission but do not erase independently proven
    lifecycle joins.  Open symbols are always derived from the authoritative
    broker snapshot, including recognizable rows that cannot be ownership-
    joined, so an unowned position can never release capacity.
    """

    issues: list[ScalpRestartIssue] = []
    now = _strict_positive_float(now_epoch)
    if now is None:
        issues.append(ScalpRestartIssue("input", "now_epoch_invalid"))
    max_age = _strict_positive_float(max_snapshot_age_secs)
    if max_age is None:
        issues.append(ScalpRestartIssue("input", "max_snapshot_age_invalid"))
    magic = _strict_positive_int(expected_magic)
    if magic is None:
        issues.append(ScalpRestartIssue("input", "expected_magic_invalid"))
    account_scope = _exact_string(expected_account_scope)
    if account_scope is None:
        issues.append(ScalpRestartIssue("input", "expected_account_scope_invalid"))

    state_valid = isinstance(state_snapshot, Mapping)
    state = state_snapshot if state_valid else {}
    if not state_valid:
        issues.append(ScalpRestartIssue("snapshot", "state_snapshot_invalid"))

    management_authority_valid = isinstance(
        production_scalp_authority, Mapping
    )
    entry_authority_valid = False
    authority = (
        production_scalp_authority if management_authority_valid else {}
    )
    if not management_authority_valid:
        issues.append(ScalpRestartIssue("authority", "scalp_authority_invalid"))
    else:
        try:
            management_error = _management_authority_error(authority)
        except (TypeError, ValueError, OverflowError):
            management_error = "scalp_authority_invalid"
        if management_error:
            issues.append(ScalpRestartIssue("authority", management_error))
            management_authority_valid = False
        elif now is not None:
            try:
                entry_error = authority_error(
                    dict(authority),
                    expectation=expectation_from_authority(dict(authority)),
                    now_epoch=now,
                )
            except (TypeError, ValueError, OverflowError):
                entry_error = "scalp_authority_invalid"
            if entry_error:
                issues.append(ScalpRestartIssue("authority", entry_error))
            else:
                entry_authority_valid = True

    snapshot_issues, snapshot_token, snapshot_received_at, raw_positions = (
        _snapshot_contract(
            state,
            now_epoch=now,
            max_snapshot_age_secs=max_age,
            expected_account_scope=account_scope,
        )
    )
    issues.extend(snapshot_issues)
    snapshot_contract_valid = not snapshot_issues and state_valid

    prepared_positions: list[_PreparedPosition] = []
    if raw_positions is not None and magic is not None:
        for raw_position in raw_positions:
            if not isinstance(raw_position, Mapping):
                issues.append(
                    ScalpRestartIssue("position", "position_row_invalid")
                )
                continue
            prepared_positions.append(
                _prepare_position(raw_position, expected_magic=magic)
            )

    ticket_counts = Counter(
        position.ticket
        for position in prepared_positions
        if position.ticket is not None
    )
    symbol_counts = Counter(
        position.symbol
        for position in prepared_positions
        if position.symbol is not None
    )
    for position in prepared_positions:
        if position.ticket is not None and ticket_counts[position.ticket] > 1:
            position.reasons.append("position_ticket_duplicate")
        if position.symbol is not None and symbol_counts[position.symbol] > 1:
            position.reasons.append("position_symbol_duplicate")

    sorted_positions = tuple(sorted(prepared_positions, key=_position_sort_key))
    open_symbols = (
        {
            position.symbol
            for position in sorted_positions
            if position.symbol is not None
        }
        if snapshot_contract_valid
        else set()
    )
    for position in sorted_positions:
        issues.extend(
            (
                ScalpRestartIssue(
                    "position",
                    reason,
                    symbol=position.symbol,
                    ticket=position.ticket,
                )
                for reason in dict.fromkeys(position.reasons)
            )
        )

    command_container_valid = _mapping_sequence(durable_command_rows)
    raw_commands: Sequence[Any] = durable_command_rows if command_container_valid else ()
    if not command_container_valid:
        issues.append(ScalpRestartIssue("input", "durable_command_rows_invalid"))

    prepared_commands: list[_PreparedCommand] = []
    for raw_command in raw_commands:
        if not isinstance(raw_command, Mapping):
            issues.append(ScalpRestartIssue("command", "command_row_invalid"))
            continue
        prepared_commands.append(_prepare_command(raw_command))
    sorted_commands = tuple(sorted(prepared_commands, key=_command_sort_key))

    relevant_id_counts = Counter(
        command.command_id
        for command in sorted_commands
        if command.relevant and command.command_id is not None
    )

    owned_positions: list[ScalpRestartOwnedPosition] = []
    if snapshot_contract_valid and magic is not None:
        for position in sorted_positions:
            if position.reasons:
                continue
            loose_candidates = [
                command
                for command in sorted_commands
                if _loose_entry_identity_matches(command, position)
            ]
            exact_candidates: list[
                tuple[_PreparedCommand, tuple[tuple[str, Any], ...]]
            ] = []
            candidate_reasons: list[tuple[_PreparedCommand, list[str]]] = []
            for command in loose_candidates:
                _, historical_binding, historical_reasons = (
                    _historical_entry_authority(command)
                )
                reasons = _entry_contract_reasons(
                    command,
                    authority=None,
                    expected_magic=magic,
                    duplicate_command_id=bool(
                        command.command_id is not None
                        and relevant_id_counts[command.command_id] > 1
                    ),
                )
                reasons.extend(historical_reasons)
                if not _entry_matches_position(command, position):
                    reasons.append("entry_command_position_identity_mismatch")
                reasons.extend(
                    _ack_identity_reasons(
                        command,
                        ticket=cast(int, position.ticket),
                        symbol=cast(str, position.symbol),
                        magic=cast(int, position.magic),
                        owner_token=cast(
                            str,
                            _payload_owner_token(command.payload),
                        ),
                        require_success=False,
                    )
                )
                reasons = list(dict.fromkeys(reasons))
                if reasons:
                    candidate_reasons.append((command, reasons))
                else:
                    exact_candidates.append((command, historical_binding))

            if len(exact_candidates) == 1:
                exact, historical_binding = exact_candidates[0]
                assert exact.command_id is not None
                exact_owner_token = _payload_owner_token(exact.payload)
                assert exact_owner_token is not None
                owned_positions.append(
                    _owned_position(
                        position,
                        entry_command_id=exact.command_id,
                        entry_authority_binding=historical_binding,
                        owner_token=exact_owner_token,
                    )
                )
            elif len(exact_candidates) > 1:
                issues.append(
                    ScalpRestartIssue(
                        "position",
                        "position_entry_command_ambiguous",
                        symbol=position.symbol,
                        ticket=position.ticket,
                    )
                )
            else:
                issues.append(
                    ScalpRestartIssue(
                        "position",
                        "position_entry_command_missing",
                        symbol=position.symbol,
                        ticket=position.ticket,
                    )
                )
            for command, reasons in candidate_reasons:
                issues.extend(
                    (
                        ScalpRestartIssue(
                            "command",
                            reason,
                            symbol=position.symbol,
                            ticket=position.ticket,
                            command_id=command.command_id,
                        )
                        for reason in reasons
                    )
                )

    owned_positions.sort(
        key=lambda item: (IG_MT4_SCALP_SYMBOLS.index(item.symbol), item.ticket)
    )
    joined_by_ticket = {position.ticket: position for position in owned_positions}

    active_entry_symbols: set[str] = set()
    active_entries_by_symbol: dict[str, list[_PreparedCommand]] = {}
    if snapshot_contract_valid and entry_authority_valid and magic is not None:
        for command in sorted_commands:
            if not command.relevant or command.status not in _ACTIVE_STATUSES:
                continue
            if command.cmd not in _ENTRY_COMMANDS:
                continue
            reasons = _entry_contract_reasons(
                command,
                authority=authority,
                expected_magic=magic,
                duplicate_command_id=bool(
                    command.command_id is not None
                    and relevant_id_counts[command.command_id] > 1
                ),
            )
            if reasons:
                issues.extend(
                    (
                        ScalpRestartIssue(
                            "command",
                            reason,
                            symbol=command.symbol,
                            command_id=command.command_id,
                        )
                        for reason in reasons
                    )
                )
                continue
            assert command.symbol is not None
            if command.symbol in open_symbols:
                issues.append(
                    ScalpRestartIssue(
                        "command",
                        "active_entry_symbol_already_open",
                        symbol=command.symbol,
                        command_id=command.command_id,
                    )
                )
                continue
            active_entries_by_symbol.setdefault(command.symbol, []).append(command)

        for symbol, commands in active_entries_by_symbol.items():
            active_entry_symbols.add(symbol)
            if len(commands) > 1:
                issues.append(
                    ScalpRestartIssue(
                        "command",
                        "active_entry_symbol_duplicate",
                        symbol=symbol,
                    )
                )

    active_exit_tickets: set[int] = set()
    active_exits_by_ticket: dict[int, list[_PreparedCommand]] = {}
    confirmed_exit_symbols: set[str] = set()
    confirmed_exits_by_ticket: dict[int, list[_PreparedCommand]] = {}
    if snapshot_contract_valid and magic is not None:
        for command in sorted_commands:
            if not command.relevant or command.cmd not in _MANAGEMENT_COMMANDS:
                continue
            target_ticket = _payload_positive_int(command.payload, "target_ticket")
            current_position = (
                joined_by_ticket.get(target_ticket)
                if target_ticket is not None
                else None
            )

            if command.status in {*_ACTIVE_STATUSES, "reconcile_required"}:
                reasons = _management_contract_reasons(
                    command,
                    historical_entry_payload=(
                        _owned_historical_entry_payload(current_position)
                        if current_position is not None
                        else None
                    ),
                    managed_entry_command_id=(
                        current_position.entry_command_id
                        if current_position is not None
                        else None
                    ),
                    expected_magic=magic,
                    duplicate_command_id=bool(
                        command.command_id is not None
                        and relevant_id_counts[command.command_id] > 1
                    ),
                )
                if current_position is None:
                    reasons.append("active_exit_target_not_joined")
                elif command.payload is not None and not (
                    command.symbol == current_position.symbol
                    and command.magic == current_position.magic
                    and _payload_owner_token(command.payload)
                    == current_position.owner_token
                ):
                    reasons.append("active_exit_position_identity_mismatch")
                if current_position is not None:
                    reasons.extend(
                        _ack_identity_reasons(
                            command,
                            ticket=current_position.ticket,
                            symbol=current_position.symbol,
                            magic=current_position.magic,
                            owner_token=current_position.owner_token,
                            require_success=False,
                        )
                    )
                reasons = list(dict.fromkeys(reasons))
                if reasons:
                    issues.extend(
                        (
                            ScalpRestartIssue(
                                "command",
                                reason,
                                symbol=command.symbol,
                                ticket=target_ticket,
                                command_id=command.command_id,
                            )
                            for reason in reasons
                        )
                    )
                    continue
                assert target_ticket is not None
                active_exits_by_ticket.setdefault(target_ticket, []).append(command)
                if command.status == "reconcile_required":
                    # Global command uncertainty owns the new-entry fence.  This
                    # restart projection keeps the exact broker ticket reserved
                    # so lifecycle code cannot issue a second mutation while the
                    # first command's broker outcome is unresolved.
                    issues.append(
                        ScalpRestartIssue(
                            "command",
                            "exit_command_reconcile_required",
                            symbol=command.symbol,
                            ticket=target_ticket,
                            command_id=command.command_id,
                            quarantines_entries=False,
                        )
                    )
                continue

            if (
                command.status != "acked"
                or command.cmd != "CLOSE"
                or current_position is None
            ):
                continue
            reasons = _management_contract_reasons(
                command,
                historical_entry_payload=(
                    _owned_historical_entry_payload(current_position)
                ),
                managed_entry_command_id=current_position.entry_command_id,
                expected_magic=magic,
                duplicate_command_id=bool(
                    command.command_id is not None
                    and relevant_id_counts[command.command_id] > 1
                ),
            )
            if command.payload is not None and not (
                command.symbol == current_position.symbol
                and command.magic == current_position.magic
                and _payload_owner_token(command.payload)
                == current_position.owner_token
            ):
                reasons.append("confirmed_exit_position_identity_mismatch")
            reasons.extend(
                _ack_identity_reasons(
                    command,
                    ticket=current_position.ticket,
                    symbol=current_position.symbol,
                    magic=current_position.magic,
                    owner_token=current_position.owner_token,
                    require_success=True,
                )
            )
            reasons = list(dict.fromkeys(reasons))
            if reasons:
                issues.extend(
                    (
                        ScalpRestartIssue(
                            "command",
                            reason,
                            symbol=current_position.symbol,
                            ticket=current_position.ticket,
                            command_id=command.command_id,
                        )
                        for reason in reasons
                    )
                )
                continue
            confirmed_exits_by_ticket.setdefault(current_position.ticket, []).append(
                command
            )
            confirmed_exit_symbols.add(current_position.symbol)

        for ticket, commands in active_exits_by_ticket.items():
            active_exit_tickets.add(ticket)
            if len(commands) > 1:
                issues.append(
                    ScalpRestartIssue(
                        "command",
                        "active_exit_ticket_duplicate",
                        symbol=joined_by_ticket[ticket].symbol,
                        ticket=ticket,
                    )
                )
        for ticket, commands in confirmed_exits_by_ticket.items():
            if len(commands) > 1:
                issues.append(
                    ScalpRestartIssue(
                        "command",
                        "confirmed_exit_ticket_duplicate",
                        symbol=joined_by_ticket[ticket].symbol,
                        ticket=ticket,
                    )
                )
            if ticket in active_exit_tickets:
                issues.append(
                    ScalpRestartIssue(
                        "command",
                        "exit_ticket_active_and_confirmed",
                        symbol=joined_by_ticket[ticket].symbol,
                        ticket=ticket,
                    )
                )

    for command in sorted_commands:
        if not command.relevant:
            continue
        if command.status == "reconcile_required" and command.cmd in _ENTRY_COMMANDS:
            issues.append(
                ScalpRestartIssue(
                    "command",
                    "entry_command_reconcile_required",
                    symbol=command.symbol,
                    command_id=command.command_id,
                )
            )
        elif command.status in _ACTIVE_STATUSES and command.cmd not in {
            *_ENTRY_COMMANDS,
            *_MANAGEMENT_COMMANDS,
        }:
            issues.append(
                ScalpRestartIssue(
                    "command",
                    "active_scalp_command_unsupported",
                    symbol=command.symbol,
                    command_id=command.command_id,
                )
            )
        elif command.status not in _KNOWN_STATUSES:
            issues.append(
                ScalpRestartIssue(
                    "command",
                    "command_status_invalid",
                    symbol=command.symbol,
                    command_id=command.command_id,
                )
            )

    ordered_issues = _dedupe_issues(issues)
    unmatched_reasons = tuple(sorted({issue.reason for issue in ordered_issues}))
    quarantine_reasons = tuple(
        sorted(
            {
                issue.reason
                for issue in ordered_issues
                if issue.quarantines_entries
            }
        )
    )
    return ScalpRestartReconciliationResult(
        authoritative_open_symbols=_catalog_order(open_symbols),
        owned_positions=tuple(owned_positions),
        active_queued_entry_symbols=_catalog_order(active_entry_symbols),
        active_exit_tickets=tuple(sorted(active_exit_tickets)),
        broker_confirmed_exit_symbols=_catalog_order(confirmed_exit_symbols),
        unmatched=ordered_issues,
        unmatched_reasons=unmatched_reasons,
        quarantine_reasons=quarantine_reasons,
        entry_admission_ready=bool(
            snapshot_contract_valid
            and entry_authority_valid
            and now is not None
            and max_age is not None
            and magic is not None
            and account_scope is not None
            and command_container_valid
            and not quarantine_reasons
        ),
        positions_snapshot_token=snapshot_token,
        positions_snapshot_received_at=snapshot_received_at,
    )


__all__ = [
    "MT4_POSITIONS_SNAPSHOT_SCHEMA",
    "SCALP_RESTART_RECONCILIATION_SCHEMA",
    "TICKET_OWNER_CONTRACT",
    "ScalpRestartIssue",
    "ScalpRestartOwnedPosition",
    "ScalpRestartReconciliationResult",
    "reconcile_scalp_restart",
]
