from __future__ import annotations

import json
import math
import re
import time
from typing import Any

from fxstack.runtime.dto import ExecutionCommand, command_ownership_fields
from fxstack.strategy.mtvclc import (
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
)


_PRODUCTION_SCALPER_LANE = "production_scalper"
_PRODUCTION_SCALPER_ENTRY_INTENT = "production_scalper_entry"
_PRODUCTION_SCALP_AUTHORITY_SCHEMA = "fxstack_production_scalp_authority_v3"
_PRODUCTION_SCALP_SCOPE_VERSION = "fxstack.ig_mt4.scalp_scope.v3"
_BROKER_ENTRY_PLAN_SCHEMA = "fxstack.production_scalp_broker_entry_plan.v2"
_BROKER_CONTRACT_STATE_SCHEMA = "fxstack_ig_mt4_contract_state_v1"
_PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS = 20
_PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS = 5
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_EXACT_MARKET_ENTRY_POSITIVE_FIELDS = (
    "entry_quote_price",
    "entry_price",
    "worst_fill_price",
    "expected_broker_contract_lot_size",
    "expected_broker_contract_min_lot",
    "expected_broker_contract_lot_step",
    "expected_broker_contract_max_lot",
    "expected_broker_contract_point",
    "expected_broker_contract_tick_size",
    "expected_broker_contract_margin_required",
)
_EXACT_MARKET_ENTRY_NONNEGATIVE_FIELDS = (
    "expected_broker_contract_stop_level_points",
    "expected_broker_contract_freeze_level_points",
)


def _wire_number(value: Any, *, field: str, positive: bool) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if not math.isfinite(number) or (number <= 0.0 if positive else number < 0.0):
        qualifier = "positive" if positive else "non-negative"
        raise ValueError(f"{field} must be a finite {qualifier} number")
    return number


def _wire_integer(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    number = _wire_number(value, field=field, positive=False)
    if not number.is_integer():
        raise ValueError(f"{field} must be an integer")
    integer = int(number)
    if integer < minimum or integer > maximum:
        raise ValueError(f"{field} is outside the allowed range")
    return integer


def _wire_epoch_seconds(
    value: Any,
    *,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    """Serialize signed fractional epoch seconds conservatively for MQL4."""

    number = _wire_number(value, field=field, positive=False)
    if number < minimum or number > maximum:
        raise ValueError(f"{field} is outside the allowed range")
    return math.floor(number)


def _wire_text(value: Any, *, field: str, max_len: int) -> str:
    text = str(value or "").strip()
    if (
        not text
        or len(text) > max_len
        or any(character in text for character in (";", "\r", "\n"))
    ):
        raise ValueError(f"{field} is missing or not wire-safe")
    return text


def _same_number(left: Any, right: Any) -> bool:
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError, OverflowError):
        return False
    return (
        math.isfinite(left_number)
        and math.isfinite(right_number)
        and math.isclose(
            left_number,
            right_number,
            rel_tol=1e-12,
            abs_tol=1e-15,
        )
    )


def _exact_market_entry_wire_contract(
    command: ExecutionCommand,
    payload: dict[str, Any],
    *,
    entry_label: str,
    symbol_max_len: int,
    broker_symbol_max_len: int,
) -> tuple[list[str], dict[str, Any]]:
    """Validate and serialize the strategy-neutral live MT4 entry envelope."""

    execution_type = _wire_text(
        payload.get("execution_type"),
        field="execution_type",
        max_len=16,
    ).lower()
    if execution_type != "market":
        raise ValueError(f"{entry_label} entries must use execution_type=market")
    if payload.get("pending_orders_forbidden") is not True:
        raise ValueError(
            f"{entry_label} entries must use pending_orders_forbidden=true"
        )
    if command.sl_price is None or command.tp_price is None:
        raise ValueError(f"{entry_label} market entry requires SL and TP")
    sl_price = _wire_number(command.sl_price, field="sl_price", positive=True)
    tp_price = _wire_number(command.tp_price, field="tp_price", positive=True)
    if not _same_number(payload.get("sl_price"), sl_price):
        raise ValueError("payload sl_price does not match command")
    if not _same_number(payload.get("tp_price"), tp_price):
        raise ValueError("payload tp_price does not match command")

    contract_schema = _wire_text(
        payload.get("expected_broker_contract_state_schema"),
        field="expected_broker_contract_state_schema",
        max_len=96,
    )
    if contract_schema != _BROKER_CONTRACT_STATE_SCHEMA:
        raise ValueError("expected_broker_contract_state_schema is incompatible")
    expected_venue = _wire_text(
        payload.get("expected_broker_contract_venue_id"),
        field="expected_broker_contract_venue_id",
        max_len=32,
    ).lower()
    if expected_venue != "ig_mt4":
        raise ValueError("expected_broker_contract_venue_id must be ig_mt4")
    expected_symbol = _wire_text(
        payload.get("expected_broker_contract_symbol"),
        field="expected_broker_contract_symbol",
        max_len=symbol_max_len,
    ).upper()
    if expected_symbol != str(command.symbol or "").strip().upper():
        raise ValueError("expected_broker_contract_symbol does not match command")
    expected_broker_symbol = _wire_text(
        payload.get("expected_broker_contract_broker_symbol"),
        field="expected_broker_contract_broker_symbol",
        max_len=broker_symbol_max_len,
    )
    expected_account_currency = _wire_text(
        payload.get("expected_broker_contract_account_currency"),
        field="expected_broker_contract_account_currency",
        max_len=8,
    ).upper()
    binding = _wire_text(
        payload.get("expected_broker_contract_binding_sha256"),
        field="expected_broker_contract_binding_sha256",
        max_len=64,
    ).lower()
    if not _SHA256_RE.fullmatch(binding):
        raise ValueError("expected_broker_contract_binding_sha256 must be SHA-256")
    if payload.get("expected_broker_contract_trade_allowed") is not True:
        raise ValueError("expected_broker_contract_trade_allowed must be true")

    numbers = {
        field: _wire_number(payload.get(field), field=field, positive=True)
        for field in _EXACT_MARKET_ENTRY_POSITIVE_FIELDS
    }
    numbers.update(
        {
            field: _wire_number(payload.get(field), field=field, positive=False)
            for field in _EXACT_MARKET_ENTRY_NONNEGATIVE_FIELDS
        }
    )
    digits = _wire_integer(
        payload.get("expected_broker_contract_digits"),
        field="expected_broker_contract_digits",
        minimum=1,
        maximum=8,
    )
    max_slippage_points = _wire_integer(
        payload.get("max_slippage_points"),
        field="max_slippage_points",
        minimum=0,
        maximum=100,
    )
    if max_slippage_points != _PRODUCTION_SCALP_MAX_SLIPPAGE_POINTS:
        raise ValueError("max_slippage_points is incompatible")
    margin_utilization_cap = _wire_number(
        payload.get("broker_contract_margin_utilization_cap"),
        field="broker_contract_margin_utilization_cap",
        positive=True,
    )
    if margin_utilization_cap > 1.0:
        raise ValueError("broker_contract_margin_utilization_cap must not exceed 1")
    if not _same_number(numbers["entry_price"], numbers["worst_fill_price"]):
        raise ValueError("entry_price must equal worst_fill_price")

    side = str(command.cmd or "").strip().upper()
    quote = numbers["entry_quote_price"]
    worst = numbers["worst_fill_price"]
    allowed = max_slippage_points * numbers["expected_broker_contract_point"]
    tolerance = max(
        1e-12,
        numbers["expected_broker_contract_point"] * 1e-7,
    )
    if side == "BUY" and not (
        quote - tolerance <= worst <= quote + allowed + tolerance
    ):
        raise ValueError("BUY worst_fill_price is outside the slippage envelope")
    if side == "SELL" and not (
        quote - allowed - tolerance <= worst <= quote + tolerance
    ):
        raise ValueError("SELL worst_fill_price is outside the slippage envelope")

    fields = [
        "execution_type=market",
        "pending_orders_forbidden=true",
        f"entry_quote_price={numbers['entry_quote_price']}",
        f"entry_price={numbers['entry_price']}",
        f"worst_fill_price={numbers['worst_fill_price']}",
        f"max_slippage_points={max_slippage_points}",
        f"expected_broker_contract_state_schema={contract_schema}",
        f"expected_broker_contract_venue_id={expected_venue}",
        f"expected_broker_contract_symbol={expected_symbol}",
        f"expected_broker_contract_broker_symbol={expected_broker_symbol}",
        f"expected_broker_contract_account_currency={expected_account_currency}",
        f"expected_broker_contract_binding_sha256={binding}",
        f"broker_contract_margin_utilization_cap={margin_utilization_cap}",
    ]
    fields.extend(
        f"{field}={numbers[field]}"
        for field in (
            *_EXACT_MARKET_ENTRY_POSITIVE_FIELDS[3:],
            *_EXACT_MARKET_ENTRY_NONNEGATIVE_FIELDS,
        )
    )
    fields.extend(
        (
            f"expected_broker_contract_digits={digits}",
            "expected_broker_contract_trade_allowed=1",
        )
    )
    return fields, {
        "numbers": numbers,
        "digits": digits,
        "max_slippage_points": max_slippage_points,
        "margin_utilization_cap": margin_utilization_cap,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "contract_schema": contract_schema,
        "expected_venue": expected_venue,
        "expected_symbol": expected_symbol,
        "expected_broker_symbol": expected_broker_symbol,
        "expected_account_currency": expected_account_currency,
        "binding": binding,
    }


def _production_scalp_entry_wire_fields(
    command: ExecutionCommand,
    payload: dict[str, Any],
) -> list[str]:
    """Validate and serialize the instant-market scalp execution envelope."""

    exact_fields, exact = _exact_market_entry_wire_contract(
        command,
        payload,
        entry_label="production scalp",
        symbol_max_len=16,
        broker_symbol_max_len=32,
    )
    numbers = exact["numbers"]
    digits = exact["digits"]
    max_slippage_points = exact["max_slippage_points"]
    sl_price = exact["sl_price"]
    tp_price = exact["tp_price"]
    expected_symbol = exact["expected_symbol"]
    expected_broker_symbol = exact["expected_broker_symbol"]
    entry_deadline_epoch = _wire_integer(
        payload.get("entry_deadline_epoch"),
        field="entry_deadline_epoch",
        minimum=1,
        maximum=2_147_483_647,
    )
    if entry_deadline_epoch <= time.time():
        raise ValueError("production scalp entry_deadline_epoch has expired")
    plan_schema = _wire_text(
        payload.get("broker_entry_plan_schema"),
        field="broker_entry_plan_schema",
        max_len=96,
    )
    if plan_schema != _BROKER_ENTRY_PLAN_SCHEMA:
        raise ValueError("broker_entry_plan_schema is incompatible")
    protection_cushion_points = _wire_integer(
        payload.get("protection_cushion_points"),
        field="protection_cushion_points",
        minimum=0,
        maximum=100,
    )
    if protection_cushion_points != _PRODUCTION_SCALP_PROTECTION_CUSHION_POINTS:
        raise ValueError("protection_cushion_points is incompatible")
    admission_mode = _wire_text(
        payload.get("expected_strategy_admission_mode"),
        field="expected_strategy_admission_mode",
        max_len=32,
    ).lower()
    if admission_mode != "signed_validation":
        raise ValueError("expected_strategy_admission_mode is incompatible")
    authority_schema = _wire_text(
        payload.get("expected_strategy_authority_schema"),
        field="expected_strategy_authority_schema",
        max_len=96,
    )
    if authority_schema != _PRODUCTION_SCALP_AUTHORITY_SCHEMA:
        raise ValueError("expected_strategy_authority_schema is incompatible")
    scope_version = _wire_text(
        payload.get("expected_strategy_scope_version"),
        field="expected_strategy_scope_version",
        max_len=96,
    )
    if scope_version != _PRODUCTION_SCALP_SCOPE_VERSION:
        raise ValueError("expected_strategy_scope_version is incompatible")
    authority_digest_fields = (
        "expected_strategy_runtime_release_certificate_sha256",
        "expected_strategy_runtime_release_signing_key_id",
        "expected_strategy_research_evidence_sha256",
        "expected_strategy_research_evidence_signing_key_id",
        "expected_strategy_registry_sha256",
        "expected_strategy_qualification_surface_sha256",
        "expected_strategy_cost_mapping_sha256",
        "expected_strategy_execution_contract_sha256",
        "expected_strategy_engine_sha256",
        "expected_strategy_config_sha256",
        "expected_strategy_binding_sha256",
    )
    authority_digests: dict[str, str] = {}
    for field_name in authority_digest_fields:
        digest = _wire_text(
            payload.get(field_name),
            field=field_name,
            max_len=64,
        ).lower()
        if not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"{field_name} must be SHA-256")
        authority_digests[field_name] = digest
    if (
        authority_digests["expected_strategy_config_sha256"]
        != MTVCLC_CONFIG_SHA256
    ):
        raise ValueError("expected_strategy_config_sha256 is incompatible")
    registry_generation = _wire_text(
        payload.get("expected_strategy_registry_generation_id"),
        field="expected_strategy_registry_generation_id",
        max_len=128,
    )
    strategy_generation = _wire_text(
        payload.get("expected_strategy_generation_id"),
        field="expected_strategy_generation_id",
        max_len=128,
    )
    if registry_generation != strategy_generation:
        raise ValueError("expected_strategy_registry_generation_id mismatch")
    registry_revision = _wire_integer(
        payload.get("expected_strategy_registry_revision"),
        field="expected_strategy_registry_revision",
        minimum=1,
        maximum=2_147_483_647,
    )
    strategy_id = _wire_text(
        payload.get("expected_strategy_id"),
        field="expected_strategy_id",
        max_len=128,
    )
    if strategy_id != MTVCLC_STRATEGY_ID:
        raise ValueError("expected_strategy_id is incompatible")
    strategy_version = _wire_text(
        payload.get("expected_strategy_version"),
        field="expected_strategy_version",
        max_len=64,
    )
    if strategy_version != MTVCLC_STRATEGY_VERSION:
        raise ValueError("expected_strategy_version is incompatible")
    strategy_config_id = _wire_text(
        payload.get("expected_strategy_config_id"),
        field="expected_strategy_config_id",
        max_len=128,
    )
    if strategy_config_id != MTVCLC_CONFIG_ID:
        raise ValueError("expected_strategy_config_id is incompatible")
    strategy_venue = _wire_text(
        payload.get("expected_strategy_venue_id"),
        field="expected_strategy_venue_id",
        max_len=32,
    ).lower()
    if strategy_venue != "ig_mt4":
        raise ValueError("expected_strategy_venue_id is incompatible")
    runtime_boot_id = _wire_text(
        payload.get("expected_strategy_runtime_boot_id"),
        field="expected_strategy_runtime_boot_id",
        max_len=128,
    )
    authority_revision = _wire_integer(
        payload.get("expected_strategy_authority_revision"),
        field="expected_strategy_authority_revision",
        minimum=1,
        maximum=2_147_483_647,
    )
    authority_expires_at_epoch = _wire_epoch_seconds(
        payload.get("expected_strategy_validation_expires_at_epoch"),
        field="expected_strategy_validation_expires_at_epoch",
        minimum=1,
        maximum=2_147_483_647,
    )
    if authority_expires_at_epoch <= time.time():
        raise ValueError("expected_strategy_validation_expires_at_epoch has expired")
    strategy_account_mode = _wire_text(
        payload.get("expected_strategy_account_mode"),
        field="expected_strategy_account_mode",
        max_len=16,
    ).lower()
    expected_account_mode = _wire_text(
        payload.get("expected_account_mode"),
        field="expected_account_mode",
        max_len=16,
    ).lower()
    if strategy_account_mode != "demo":
        raise ValueError("expected_strategy_account_mode is incompatible")
    if expected_account_mode != "demo":
        raise ValueError("expected_account_mode is incompatible")
    if strategy_account_mode != expected_account_mode:
        raise ValueError(
            "expected_strategy_account_mode does not match expected_account_mode"
        )
    authority_wire_fields = [
        f"expected_strategy_authority_schema={authority_schema}",
        f"expected_strategy_admission_mode={admission_mode}",
        f"expected_strategy_account_mode={strategy_account_mode}",
        f"expected_strategy_generation_id={strategy_generation}",
        f"expected_strategy_id={strategy_id}",
        f"expected_strategy_version={strategy_version}",
        (
            "expected_strategy_engine_sha256="
            + authority_digests["expected_strategy_engine_sha256"]
        ),
        f"expected_strategy_config_id={strategy_config_id}",
        (
            "expected_strategy_config_sha256="
            + authority_digests["expected_strategy_config_sha256"]
        ),
        (
            "expected_strategy_runtime_release_certificate_sha256="
            + authority_digests[
                "expected_strategy_runtime_release_certificate_sha256"
            ]
        ),
        (
            "expected_strategy_runtime_release_signing_key_id="
            + authority_digests[
                "expected_strategy_runtime_release_signing_key_id"
            ]
        ),
        (
            "expected_strategy_research_evidence_sha256="
            + authority_digests["expected_strategy_research_evidence_sha256"]
        ),
        (
            "expected_strategy_research_evidence_signing_key_id="
            + authority_digests[
                "expected_strategy_research_evidence_signing_key_id"
            ]
        ),
        f"expected_strategy_registry_generation_id={registry_generation}",
        f"expected_strategy_registry_revision={registry_revision}",
        (
            "expected_strategy_registry_sha256="
            + authority_digests["expected_strategy_registry_sha256"]
        ),
        (
            "expected_strategy_qualification_surface_sha256="
            + authority_digests[
                "expected_strategy_qualification_surface_sha256"
            ]
        ),
        (
            "expected_strategy_cost_mapping_sha256="
            + authority_digests["expected_strategy_cost_mapping_sha256"]
        ),
        (
            "expected_strategy_execution_contract_sha256="
            + authority_digests[
                "expected_strategy_execution_contract_sha256"
            ]
        ),
        (
            "expected_strategy_validation_expires_at_epoch="
            + str(authority_expires_at_epoch)
        ),
        f"expected_strategy_venue_id={strategy_venue}",
        f"expected_strategy_scope_version={scope_version}",
        (
            "expected_strategy_binding_sha256="
            + authority_digests["expected_strategy_binding_sha256"]
        ),
        f"expected_strategy_runtime_boot_id={runtime_boot_id}",
        f"expected_strategy_authority_revision={authority_revision}",
    ]

    plan = payload.get("broker_entry_plan")
    if not isinstance(plan, dict):
        raise ValueError("broker_entry_plan is required")
    plan_identity = {
        "schema_version": plan_schema,
        "symbol": expected_symbol,
        "broker_symbol": expected_broker_symbol,
        "side": str(command.cmd or "").strip().upper(),
    }
    for field, expected_value in plan_identity.items():
        if str(plan.get(field) or "").strip().lower() != str(expected_value).lower():
            raise ValueError(f"broker_entry_plan {field} mismatch")
    if str(plan.get("execution_type") or "").strip().lower() != "market":
        raise ValueError("broker_entry_plan execution_type mismatch")
    if plan.get("pending_orders_forbidden") is not True:
        raise ValueError("broker_entry_plan pending_orders_forbidden mismatch")
    if not _same_number(
        plan.get("entry_deadline_epoch"),
        entry_deadline_epoch,
    ):
        raise ValueError("broker_entry_plan entry_deadline_epoch mismatch")
    plan_numbers = {
        "quote_entry_price": numbers["entry_quote_price"],
        "worst_fill_price": numbers["worst_fill_price"],
        "sl_price": sl_price,
        "tp_price": tp_price,
        "max_slippage_points": max_slippage_points,
        "protection_cushion_points": protection_cushion_points,
        "point": numbers["expected_broker_contract_point"],
        "tick_size": numbers["expected_broker_contract_tick_size"],
        "digits": digits,
    }
    for field, expected_value in plan_numbers.items():
        if not _same_number(plan.get(field), expected_value):
            raise ValueError(f"broker_entry_plan {field} mismatch")

    return [
        *exact_fields[:2],
        f"entry_deadline_epoch={entry_deadline_epoch}",
        f"broker_entry_plan_schema={plan_schema}",
        *exact_fields[2:6],
        f"protection_cushion_points={protection_cushion_points}",
        *authority_wire_fields,
        *exact_fields[6:],
    ]


def _model_stack_market_entry_wire_fields(
    command: ExecutionCommand,
    payload: dict[str, Any],
) -> list[str]:
    """Serialize the exact broker envelope for a non-MTVCLC live entry."""

    fields, _ = _exact_market_entry_wire_contract(
        command,
        payload,
        entry_label="MT4",
        symbol_max_len=32,
        broker_symbol_max_len=64,
    )
    return fields


def safe_text(value: Any, max_len: int = 1400) -> str:
    out = str(value or "").replace("\r", " ").replace("\n", " | ").replace(";", ",")
    return out[:max_len]


def command_to_wire_line(command: ExecutionCommand) -> str:
    payload = dict(command.payload or {})
    cmd = str(command.cmd).upper().strip()
    ownership_contract, target_ticket, owner_token = command_ownership_fields(command)
    parts: list[str] = ["cmd=CLOSE_ALL"] if cmd == "CLOSE_ALL" else [f"cmd={cmd}"]
    if cmd != "CLOSE_ALL" and command.symbol:
        parts.append(f"symbol={command.symbol}")

    lots_value = float(command.lots)
    if cmd == "CLOSE_PARTIAL":
        lots_value = float(command.close_lots if command.close_lots > 0.0 else command.lots)
        parts.append(f"close_lots={float(lots_value)}")
    parts.append(f"lots={float(lots_value)}")
    if command.tp_cash is not None:
        parts.append(f"tp_cash={float(command.tp_cash)}")
    if command.tp_price is not None:
        parts.append(f"tp_price={float(command.tp_price)}")
    if command.sl_price is not None:
        parts.append(f"sl={float(command.sl_price)}")
    if target_ticket > 0:
        parts.append(f"target_ticket={int(target_ticket)}")
    if owner_token:
        parts.append(f"owner_token={safe_text(owner_token, max_len=31)}")
    if ownership_contract:
        parts.append(
            f"ownership_contract={safe_text(ownership_contract, max_len=32)}"
        )
    if command.action:
        parts.append(f"action={safe_text(command.action, max_len=64)}")
    if float(command.action_score) != 0.0:
        parts.append(f"action_score={float(command.action_score):.6f}")
    if command.reversal_token:
        parts.append(f"reversal_token={safe_text(command.reversal_token, max_len=96)}")
    parts.extend(
        [
            f"magic={int(command.magic)}",
            f"proto={safe_text(command.proto or 'v2', max_len=16)}",
            f"command_id={command.command_id}",
            f"session_id={command.session_id}",
            f"intent={command.intent}",
            f"trace_id={command.trace_id or command.command_id}",
            f"t_bridge_queued={float(command.created_at):.6f}",
        ]
    )
    if command.correlation_id:
        parts.append(f"correlation_id={safe_text(command.correlation_id, max_len=160)}")
    if command.thread_id:
        parts.append(f"thread_id={safe_text(command.thread_id, max_len=192)}")
    if command.idempotency_key:
        parts.append(f"idempotency_key={safe_text(command.idempotency_key, max_len=160)}")
    if command.schema_version:
        parts.append(f"schema_version={safe_text(command.schema_version, max_len=96)}")
    if command.orchestration_meta_json:
        payload_json = json.dumps(dict(command.orchestration_meta_json or {}), separators=(",", ":"), sort_keys=True)
        parts.append(f"orchestration_meta_json={safe_text(payload_json)}")
    if cmd in {"BUY", "SELL"}:
        claimed_admission_mode = str(
            payload.get("expected_strategy_admission_mode") or ""
        ).strip().lower()
        if claimed_admission_mode and claimed_admission_mode != "signed_validation":
            raise ValueError(
                "BUY/SELL expected_strategy_admission_mode must be signed_validation"
            )
        strategy_lane = str(payload.get("strategy_lane") or "").strip().lower()
        command_intent = str(command.intent or "").strip().lower()
        payload_intent = str(payload.get("intent") or "").strip().lower()
        production_scalp_marked = bool(
            strategy_lane == _PRODUCTION_SCALPER_LANE
            or command_intent == _PRODUCTION_SCALPER_ENTRY_INTENT
            or payload_intent == _PRODUCTION_SCALPER_ENTRY_INTENT
        )
        if production_scalp_marked and not (
            strategy_lane == _PRODUCTION_SCALPER_LANE
            and command_intent == _PRODUCTION_SCALPER_ENTRY_INTENT
            and payload_intent == _PRODUCTION_SCALPER_ENTRY_INTENT
        ):
            raise ValueError("production scalp lane and intent must agree")
        if production_scalp_marked:
            parts.extend(_production_scalp_entry_wire_fields(command, payload))
        else:
            parts.extend(_model_stack_market_entry_wire_fields(command, payload))
        expected_account_mode = str(payload.get("expected_account_mode") or "").strip().lower()
        expected_account_scope = str(payload.get("expected_account_scope") or "").strip()
        if expected_account_mode:
            parts.append(f"expected_account_mode={safe_text(expected_account_mode, max_len=16)}")
        if expected_account_scope:
            parts.append(f"expected_account_scope={safe_text(expected_account_scope, max_len=128)}")
    thought = payload.get("thought")
    if thought:
        parts.append(f"thought={safe_text(thought)}")
    return ";".join(parts)
