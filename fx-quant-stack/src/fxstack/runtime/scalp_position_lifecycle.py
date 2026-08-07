# AGENT: ROLE: Pure restart-safe time-stop decisions for owned production scalp positions.
# AGENT: ENTRYPOINT: `evaluate_scalp_position_lifecycle`.
# AGENT: PRIMARY INPUTS: authoritative owned positions, finalized common M1 minute, as-of time.
# AGENT: PRIMARY OUTPUTS: immutable ticket-targeted CLOSE decisions and diagnostics.
# AGENT: STATE / SIDE EFFECTS: none; no command creation, I/O, settings, store, service, or runner access.
"""Derive causal time-stop closes from authoritative IG MT4 position truth.

This module deliberately does not infer stop-loss or take-profit fills.  Those
protections remain broker-native.  It only counts fully closed common M1 bars
from each broker open time and emits ticket-bound close decisions once the
global time-stop horizon has elapsed.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import math
import re
from typing import Any, Literal

from fxstack._serialization import flat_dataclass_dict
from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.scalp_rollover_guard import (
    PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION,
    ProductionScalpRolloverDecision,
)


SCALP_POSITION_LIFECYCLE_SCHEMA_VERSION = (
    "fxstack.runtime.scalp_position_lifecycle.v2"
)
TICKET_OWNER_CONTRACT: Literal["ticket_owner_v1"] = "ticket_owner_v1"
M1_SECONDS = 60
_OWNER_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,31}$")
_SUPPORTED_SYMBOLS = frozenset(IG_MT4_SCALP_SYMBOLS)


@dataclass(frozen=True, slots=True)
class ScalpTimeStopCloseDecision:
    """One immutable, exact-ticket time-stop close decision."""

    symbol: str
    target_ticket: int
    lots: float
    magic: int
    owner_token: str
    ownership_contract: Literal["ticket_owner_v1"]
    reason: Literal["time_stop", "rollover_funding_guard"]
    bars_held: int | None

    def to_dict(self) -> dict[str, Any]:
        return flat_dataclass_dict(self)


@dataclass(frozen=True, slots=True)
class ScalpPositionLifecycleDiagnostic:
    """Validation and causal age evidence for one supplied position row."""

    symbol: str | None
    ticket: int | None
    valid: bool
    reasons: tuple[str, ...]
    open_time_epoch: int | None
    open_minute_epoch: int | None
    bars_held: int | None
    time_stop_due: bool
    rollover_close_due: bool

    def to_dict(self) -> dict[str, Any]:
        return flat_dataclass_dict(self)


@dataclass(frozen=True, slots=True)
class ScalpPositionLifecycleDiagnostics:
    """Fail-closed lifecycle input and decision summary."""

    accepted: bool
    reasons: tuple[str, ...]
    finalized_common_minute_epoch: int | None
    as_of_epoch: float | None
    expected_magic: int | None
    expected_ownership_contract: str
    time_stop_bars: int | None
    time_stop_ready: bool
    time_stop_reasons: tuple[str, ...]
    rollover_guard_accepted: bool
    rollover_guard_reason: str
    rollover_force_close_active: bool
    position_count: int
    close_decision_count: int
    position_diagnostics: tuple[ScalpPositionLifecycleDiagnostic, ...]
    schema_version: str = SCALP_POSITION_LIFECYCLE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = flat_dataclass_dict(self)
        payload["position_diagnostics"] = tuple(
            diagnostic.to_dict() for diagnostic in self.position_diagnostics
        )
        return payload


@dataclass(frozen=True, slots=True)
class ScalpPositionLifecycleResult:
    """Immutable time-stop close decisions plus their validation evidence."""

    close_decisions: tuple[ScalpTimeStopCloseDecision, ...]
    diagnostics: ScalpPositionLifecycleDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "close_decisions": tuple(
                decision.to_dict() for decision in self.close_decisions
            ),
            "diagnostics": self.diagnostics.to_dict(),
        }


@dataclass(slots=True)
class _PreparedPosition:
    symbol: str | None
    ticket: int | None
    lots: float | None
    magic: int | None
    owner_token: str | None
    ownership_contract: str | None
    open_time_epoch: int | None
    reasons: list[str]


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


def _parse_timestamp(value: Any, *, require_integral: bool) -> float | int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        try:
            seconds = value.astimezone(timezone.utc).timestamp()
        except (OSError, OverflowError, ValueError):
            return None
    elif isinstance(value, (int, float)):
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            try:
                seconds = parsed.astimezone(timezone.utc).timestamp()
            except (OSError, OverflowError, ValueError):
                return None
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    if not require_integral:
        return float(seconds)
    rounded = round(seconds)
    if abs(seconds - rounded) > 1e-6:
        return None
    return int(rounded)


def _prepare_position(
    raw: Mapping[str, Any],
    *,
    expected_magic: int | None,
    expected_ownership_contract: str,
    as_of_epoch: float | None,
) -> _PreparedPosition:
    reasons: list[str] = []

    ticket = _strict_positive_int(raw.get("ticket"))
    if ticket is None:
        reasons.append("ticket_invalid")

    raw_symbol = raw.get("symbol")
    symbol = raw_symbol if isinstance(raw_symbol, str) else None
    if symbol not in _SUPPORTED_SYMBOLS:
        reasons.append("symbol_invalid")

    side = raw.get("side")
    if side not in {"BUY", "SELL"}:
        reasons.append("side_invalid")

    lots = _strict_positive_float(raw.get("lots"))
    if lots is None:
        reasons.append("lots_invalid")

    magic = _strict_positive_int(raw.get("magic"))
    if magic is None:
        reasons.append("magic_invalid")
    elif expected_magic is not None and magic != expected_magic:
        reasons.append("magic_mismatch")

    raw_owner_token = raw.get("owner_token")
    owner_token = raw_owner_token if isinstance(raw_owner_token, str) else None
    if owner_token is None or _OWNER_TOKEN_RE.fullmatch(owner_token) is None:
        reasons.append("owner_token_invalid")
    if raw.get("order_comment") != owner_token:
        reasons.append("owner_token_comment_mismatch")

    raw_contract = raw.get("ownership_contract")
    ownership_contract = raw_contract if isinstance(raw_contract, str) else None
    if ownership_contract != expected_ownership_contract:
        reasons.append("ownership_contract_mismatch")

    parsed_open_time = _parse_timestamp(
        raw.get("open_time"),
        require_integral=True,
    )
    open_time_epoch = (
        int(parsed_open_time) if isinstance(parsed_open_time, int) else None
    )
    if open_time_epoch is None:
        reasons.append("open_time_invalid")
    elif as_of_epoch is not None and open_time_epoch > as_of_epoch:
        reasons.append("open_time_after_as_of")

    return _PreparedPosition(
        symbol=symbol,
        ticket=ticket,
        lots=lots,
        magic=magic,
        owner_token=owner_token,
        ownership_contract=ownership_contract,
        open_time_epoch=open_time_epoch,
        reasons=reasons,
    )


def _position_sort_key(
    position: _PreparedPosition,
) -> tuple[str, int, tuple[str, ...]]:
    return (
        position.symbol or "\uffff",
        position.ticket if position.ticket is not None else 2**63 - 1,
        tuple(position.reasons),
    )


def _bars_held(
    *,
    open_time_epoch: int,
    finalized_common_minute_epoch: int,
) -> int:
    open_minute = (open_time_epoch // M1_SECONDS) * M1_SECONDS
    if open_minute > finalized_common_minute_epoch:
        return 0
    return ((finalized_common_minute_epoch - open_minute) // M1_SECONDS) + 1


def evaluate_scalp_position_lifecycle(
    *,
    authoritative_owned_positions: Sequence[Mapping[str, Any]],
    finalized_common_minute_epoch: Any,
    as_of_epoch: Any,
    expected_magic: int,
    expected_ownership_contract: str,
    time_stop_bars: int,
    rollover_guard_decision: ProductionScalpRolloverDecision | None = None,
) -> ScalpPositionLifecycleResult:
    """Return canonical exact-ticket time-stop or funding-guard closes.

    Every supplied row must already contain both the durable expected
    ``owner_token`` and the broker-observed ``order_comment``.  They must be
    byte-for-byte equal.  One invalid or duplicate row invalidates the whole
    batch, so a restart can never manage a partially joined position set.
    """

    reasons: list[str] = []
    magic = _strict_positive_int(expected_magic)
    if magic is None:
        reasons.append("expected_magic_invalid")

    if expected_ownership_contract != TICKET_OWNER_CONTRACT:
        reasons.append("expected_ownership_contract_invalid")

    horizon = _strict_positive_int(time_stop_bars)
    if horizon is None:
        reasons.append("time_stop_bars_invalid")

    rollover_accepted = True
    rollover_reason = ""
    rollover_force_close = False
    if rollover_guard_decision is not None:
        if not isinstance(
            rollover_guard_decision,
            ProductionScalpRolloverDecision,
        ):
            rollover_accepted = False
            rollover_reason = "rollover_guard_decision_invalid"
        elif (
            rollover_guard_decision.schema_version
            != PRODUCTION_SCALP_ROLLOVER_GUARD_SCHEMA_VERSION
        ):
            rollover_accepted = False
            rollover_reason = "rollover_guard_schema_invalid"
        elif not rollover_guard_decision.accepted:
            rollover_accepted = False
            rollover_reason = (
                rollover_guard_decision.reason
                or "rollover_guard_decision_rejected"
            )
        else:
            rollover_reason = str(rollover_guard_decision.reason or "")
            rollover_force_close = bool(
                rollover_guard_decision.force_close_active
            )
    if not rollover_accepted:
        reasons.append(rollover_reason)

    parsed_as_of = _parse_timestamp(as_of_epoch, require_integral=False)
    as_of = float(parsed_as_of) if parsed_as_of is not None else None
    if as_of is None:
        reasons.append("as_of_invalid")
    elif rollover_guard_decision is not None and rollover_accepted:
        guard_as_of = rollover_guard_decision.as_of_epoch
        if guard_as_of is None or abs(float(guard_as_of) - as_of) > 1e-6:
            rollover_accepted = False
            rollover_force_close = False
            rollover_reason = "rollover_guard_clock_mismatch"
            reasons.append(rollover_reason)

    parsed_finalized = _parse_timestamp(
        finalized_common_minute_epoch,
        require_integral=True,
    )
    finalized_minute = (
        int(parsed_finalized) if isinstance(parsed_finalized, int) else None
    )
    time_stop_reasons: list[str] = []
    if finalized_minute is None:
        time_stop_reasons.append("finalized_common_minute_invalid")
    elif finalized_minute % M1_SECONDS != 0:
        time_stop_reasons.append("finalized_common_minute_not_m1_aligned")
    elif as_of is not None and as_of < finalized_minute + M1_SECONDS:
        time_stop_reasons.append("finalized_common_minute_not_closed")
    time_stop_ready = not time_stop_reasons
    # Missing/invalid bar-finalization evidence still blocks a time-stop.  It
    # cannot suppress a separately authenticated exact-owner protective close
    # once the UTC funding guard is active.
    if time_stop_reasons and not rollover_force_close:
        reasons.extend(time_stop_reasons)

    valid_sequence = (
        isinstance(authoritative_owned_positions, Sequence)
        and not isinstance(
            authoritative_owned_positions,
            (str, bytes, bytearray, Mapping),
        )
    )
    raw_positions: Sequence[Mapping[str, Any]]
    if valid_sequence:
        raw_positions = authoritative_owned_positions
    else:
        raw_positions = ()
        reasons.append("authoritative_owned_positions_invalid")

    prepared: list[_PreparedPosition] = []
    non_mapping_rows = False
    for raw in raw_positions:
        if not isinstance(raw, Mapping):
            non_mapping_rows = True
            prepared.append(
                _PreparedPosition(
                    symbol=None,
                    ticket=None,
                    lots=None,
                    magic=None,
                    owner_token=None,
                    ownership_contract=None,
                    open_time_epoch=None,
                    reasons=["position_row_invalid"],
                )
            )
            continue
        prepared.append(
            _prepare_position(
                raw,
                expected_magic=magic,
                expected_ownership_contract=expected_ownership_contract,
                as_of_epoch=as_of,
            )
        )
    if non_mapping_rows:
        reasons.append("position_row_invalid")

    ticket_counts = Counter(
        position.ticket
        for position in prepared
        if position.ticket is not None
    )
    symbol_counts = Counter(
        position.symbol
        for position in prepared
        if position.symbol in _SUPPORTED_SYMBOLS
    )
    for position in prepared:
        if position.ticket is not None and ticket_counts[position.ticket] > 1:
            position.reasons.append("ticket_duplicate")
        if position.symbol is not None and symbol_counts[position.symbol] > 1:
            position.reasons.append("symbol_duplicate")

    if any(position.reasons for position in prepared):
        reasons.append("position_validation_failed")

    input_accepted = not reasons
    sorted_positions = tuple(sorted(prepared, key=_position_sort_key))
    position_diagnostics: list[ScalpPositionLifecycleDiagnostic] = []
    decisions: list[ScalpTimeStopCloseDecision] = []

    for position in sorted_positions:
        open_minute: int | None = None
        held: int | None = None
        due = False
        rollover_due = False
        if position.open_time_epoch is not None:
            open_minute = (
                position.open_time_epoch // M1_SECONDS
            ) * M1_SECONDS
        if (
            input_accepted
            and time_stop_ready
            and position.open_time_epoch is not None
            and finalized_minute is not None
            and horizon is not None
        ):
            held = _bars_held(
                open_time_epoch=position.open_time_epoch,
                finalized_common_minute_epoch=finalized_minute,
            )
            due = held >= horizon
        rollover_due = bool(input_accepted and rollover_force_close)
        if due or rollover_due:
            assert position.symbol is not None
            assert position.ticket is not None
            assert position.lots is not None
            assert position.magic is not None
            assert position.owner_token is not None
            decisions.append(
                ScalpTimeStopCloseDecision(
                    symbol=position.symbol,
                    target_ticket=position.ticket,
                    lots=position.lots,
                    magic=position.magic,
                    owner_token=position.owner_token,
                    ownership_contract=TICKET_OWNER_CONTRACT,
                    reason=(
                        "rollover_funding_guard"
                        if rollover_due
                        else "time_stop"
                    ),
                    bars_held=held,
                )
            )
        position_diagnostics.append(
            ScalpPositionLifecycleDiagnostic(
                symbol=position.symbol,
                ticket=position.ticket,
                valid=not position.reasons,
                reasons=tuple(position.reasons),
                open_time_epoch=position.open_time_epoch,
                open_minute_epoch=open_minute,
                bars_held=held,
                time_stop_due=due,
                rollover_close_due=rollover_due,
            )
        )

    ordered_decisions = tuple(
        sorted(decisions, key=lambda item: (item.symbol, item.target_ticket))
    )
    diagnostics = ScalpPositionLifecycleDiagnostics(
        accepted=input_accepted,
        reasons=tuple(dict.fromkeys(reasons)),
        finalized_common_minute_epoch=finalized_minute,
        as_of_epoch=as_of,
        expected_magic=magic,
        expected_ownership_contract=str(expected_ownership_contract),
        time_stop_bars=horizon,
        time_stop_ready=time_stop_ready,
        time_stop_reasons=tuple(time_stop_reasons),
        rollover_guard_accepted=rollover_accepted,
        rollover_guard_reason=rollover_reason,
        rollover_force_close_active=rollover_force_close,
        position_count=len(prepared),
        close_decision_count=len(ordered_decisions),
        position_diagnostics=tuple(position_diagnostics),
    )
    return ScalpPositionLifecycleResult(
        close_decisions=ordered_decisions,
        diagnostics=diagnostics,
    )


__all__ = [
    "M1_SECONDS",
    "SCALP_POSITION_LIFECYCLE_SCHEMA_VERSION",
    "TICKET_OWNER_CONTRACT",
    "ScalpPositionLifecycleDiagnostic",
    "ScalpPositionLifecycleDiagnostics",
    "ScalpPositionLifecycleResult",
    "ScalpTimeStopCloseDecision",
    "evaluate_scalp_position_lifecycle",
]
