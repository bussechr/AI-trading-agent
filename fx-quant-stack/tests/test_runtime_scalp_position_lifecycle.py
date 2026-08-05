from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError, fields
from datetime import datetime, timezone
from itertools import permutations
from pathlib import Path
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.scalp_position_lifecycle import (
    SCALP_POSITION_LIFECYCLE_SCHEMA_VERSION,
    TICKET_OWNER_CONTRACT,
    ScalpTimeStopCloseDecision,
    evaluate_scalp_position_lifecycle,
)
from fxstack.runtime.scalp_rollover_guard import (
    evaluate_production_scalp_rollover_guard,
)


BASE_MINUTE = 1_800_000_000
EXPECTED_MAGIC = 246_810


def _position(
    symbol: str = "EURUSD",
    *,
    ticket: int = 101,
    open_time: Any = BASE_MINUTE + 59,
    owner_token: str = "fxs-owner-101",
    **overrides: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "symbol": symbol,
        "side": "BUY",
        "ticket": ticket,
        "lots": 0.12,
        "magic": EXPECTED_MAGIC,
        "owner_token": owner_token,
        "order_comment": owner_token,
        "ownership_contract": TICKET_OWNER_CONTRACT,
        "open_time": open_time,
        "sl": 1.09,
        "tp": 1.13,
    }
    row.update(overrides)
    return row


def _evaluate(
    positions: Any = None,
    **overrides: Any,
):
    kwargs: dict[str, Any] = {
        "authoritative_owned_positions": (
            [_position()] if positions is None else positions
        ),
        "finalized_common_minute_epoch": BASE_MINUTE + 19 * 60,
        "as_of_epoch": BASE_MINUTE + 20 * 60,
        "expected_magic": EXPECTED_MAGIC,
        "expected_ownership_contract": TICKET_OWNER_CONTRACT,
        "time_stop_bars": 20,
    }
    kwargs.update(overrides)
    return evaluate_scalp_position_lifecycle(**kwargs)


def test_closed_entry_minute_counts_as_bar_one_and_emits_exact_close() -> None:
    result = _evaluate()

    assert result.diagnostics.accepted is True
    assert result.diagnostics.reasons == ()
    assert result.close_decisions == (
        ScalpTimeStopCloseDecision(
            symbol="EURUSD",
            target_ticket=101,
            lots=0.12,
            magic=EXPECTED_MAGIC,
            owner_token="fxs-owner-101",
            ownership_contract=TICKET_OWNER_CONTRACT,
            reason="time_stop",
            bars_held=20,
        ),
    )
    diagnostic = result.diagnostics.position_diagnostics[0]
    assert diagnostic.open_time_epoch == BASE_MINUTE + 59
    assert diagnostic.open_minute_epoch == BASE_MINUTE
    assert diagnostic.bars_held == 20
    assert diagnostic.time_stop_due is True


def test_horizon_is_not_reached_one_closed_bar_early() -> None:
    result = _evaluate(
        [_position(open_time=BASE_MINUTE + 60)],
    )

    assert result.diagnostics.accepted is True
    assert result.close_decisions == ()
    assert result.diagnostics.position_diagnostics[0].bars_held == 19


def test_position_in_unfinalized_minute_has_zero_closed_bars() -> None:
    finalized = BASE_MINUTE + 19 * 60
    result = _evaluate(
        [_position(open_time=finalized + 60)],
        as_of_epoch=finalized + 75.5,
    )

    assert result.diagnostics.accepted is True
    assert result.close_decisions == ()
    assert result.diagnostics.position_diagnostics[0].bars_held == 0


def test_broker_native_sl_and_tp_are_not_inferred() -> None:
    result = _evaluate(
        [
            _position(
                open_time=BASE_MINUTE + 18 * 60,
                sl=999_999.0,
                tp=0.000_001,
            )
        ],
    )

    assert result.diagnostics.accepted is True
    assert result.diagnostics.position_diagnostics[0].bars_held == 2
    assert result.close_decisions == ()


def test_complete_ig_mt4_scope_is_accepted_and_sorted_deterministically() -> None:
    rows = [
        _position(
            symbol,
            ticket=10_000 + index,
            owner_token=f"fxs-owner-{index}",
        )
        for index, symbol in enumerate(reversed(IG_MT4_SCALP_SYMBOLS), start=1)
    ]

    result = _evaluate(rows)

    expected_symbols = tuple(sorted(IG_MT4_SCALP_SYMBOLS))
    assert result.diagnostics.accepted is True
    assert len(result.close_decisions) == 22
    assert tuple(item.symbol for item in result.close_decisions) == expected_symbols
    assert tuple(
        item.symbol for item in result.diagnostics.position_diagnostics
    ) == expected_symbols


def test_funding_guard_closes_every_symbol_before_time_stop_including_crypto() -> None:
    guard_now = datetime(2026, 8, 3, 20, 50, tzinfo=timezone.utc).timestamp()
    rows = [
        _position(
            symbol,
            ticket=20_000 + index,
            owner_token=f"fxs-rollover-{index}",
            open_time=int(guard_now - 30.0),
        )
        for index, symbol in enumerate(IG_MT4_SCALP_SYMBOLS, start=1)
    ]

    result = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=rows,
        finalized_common_minute_epoch=int(guard_now - 60.0),
        as_of_epoch=guard_now,
        expected_magic=EXPECTED_MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
        rollover_guard_decision=(
            evaluate_production_scalp_rollover_guard(guard_now)
        ),
    )

    assert result.diagnostics.accepted is True
    assert result.diagnostics.rollover_force_close_active is True
    assert len(result.close_decisions) == len(IG_MT4_SCALP_SYMBOLS)
    assert {item.symbol for item in result.close_decisions} == set(
        IG_MT4_SCALP_SYMBOLS
    )
    assert {"BTCUSD", "ETHUSD"}.issubset(
        {item.symbol for item in result.close_decisions}
    )
    assert all(
        item.reason == "rollover_funding_guard"
        for item in result.close_decisions
    )
    assert all(item.bars_held == 1 for item in result.close_decisions)


def test_funding_guard_close_survives_missing_common_bar_but_not_bad_ownership() -> None:
    guard_now = datetime(2026, 1, 15, 21, 55, tzinfo=timezone.utc).timestamp()
    guard = evaluate_production_scalp_rollover_guard(guard_now)

    valid = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=[
            _position(open_time=int(guard_now - 30.0))
        ],
        finalized_common_minute_epoch=0,
        as_of_epoch=guard_now,
        expected_magic=EXPECTED_MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
        rollover_guard_decision=guard,
    )
    invalid = evaluate_scalp_position_lifecycle(
        authoritative_owned_positions=[
            _position(
                open_time=int(guard_now - 30.0),
                order_comment="foreign-owner",
            )
        ],
        finalized_common_minute_epoch=0,
        as_of_epoch=guard_now,
        expected_magic=EXPECTED_MAGIC,
        expected_ownership_contract=TICKET_OWNER_CONTRACT,
        time_stop_bars=20,
        rollover_guard_decision=guard,
    )

    assert valid.diagnostics.accepted is True
    assert valid.diagnostics.time_stop_ready is False
    assert valid.close_decisions[0].reason == "rollover_funding_guard"
    assert valid.close_decisions[0].bars_held is None
    assert invalid.diagnostics.accepted is False
    assert invalid.close_decisions == ()


def test_input_permutation_cannot_change_decisions_or_diagnostics() -> None:
    rows = (
        _position("USDJPY", ticket=302, owner_token="fxs-owner-302"),
        _position("EURUSD", ticket=301, owner_token="fxs-owner-301"),
        _position(
            "BTCUSD",
            ticket=303,
            owner_token="fxs-owner-303",
            side="SELL",
            open_time=BASE_MINUTE + 60,
        ),
    )
    expected = _evaluate(list(rows)).to_dict()

    for variant in permutations(rows):
        assert _evaluate(list(variant)).to_dict() == expected


def test_empty_authoritative_position_set_is_valid() -> None:
    result = _evaluate([])

    assert result.diagnostics.accepted is True
    assert result.diagnostics.position_count == 0
    assert result.diagnostics.close_decision_count == 0
    assert result.close_decisions == ()


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        ({"ticket": 0}, "ticket_invalid"),
        ({"ticket": True}, "ticket_invalid"),
        ({"ticket": "101"}, "ticket_invalid"),
        ({"symbol": "eurusd"}, "symbol_invalid"),
        ({"symbol": "XAUUSD"}, "symbol_invalid"),
        ({"side": "buy"}, "side_invalid"),
        ({"side": "HOLD"}, "side_invalid"),
        ({"lots": 0.0}, "lots_invalid"),
        ({"lots": float("nan")}, "lots_invalid"),
        ({"lots": "0.12"}, "lots_invalid"),
        ({"lots": 10**10_000}, "lots_invalid"),
        ({"magic": 0}, "magic_invalid"),
        ({"magic": EXPECTED_MAGIC + 1}, "magic_mismatch"),
        ({"owner_token": "bad token", "order_comment": "bad token"}, "owner_token_invalid"),
        ({"owner_token": "x" * 32, "order_comment": "x" * 32}, "owner_token_invalid"),
        ({"owner_token": "expected", "order_comment": "observed"}, "owner_token_comment_mismatch"),
        ({"ownership_contract": "legacy_elbridge_v1"}, "ownership_contract_mismatch"),
        ({"open_time": 0}, "open_time_invalid"),
        ({"open_time": BASE_MINUTE + 0.5}, "open_time_invalid"),
        ({"open_time": 10**10_000}, "open_time_invalid"),
        ({"open_time": "not-a-time"}, "open_time_invalid"),
        (
            {"open_time": datetime(2027, 1, 15, 8, 0)},
            "open_time_invalid",
        ),
        ({"open_time": BASE_MINUTE + 20 * 60 + 1}, "open_time_after_as_of"),
    ),
)
def test_invalid_owned_row_fails_the_entire_batch(
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    due = _position("USDJPY", ticket=202, owner_token="fxs-owner-202")
    invalid = _position(**overrides)

    result = _evaluate([due, invalid])

    assert result.diagnostics.accepted is False
    assert result.diagnostics.reasons == ("position_validation_failed",)
    assert result.close_decisions == ()
    assert result.diagnostics.close_decision_count == 0
    assert any(
        expected_reason in item.reasons
        for item in result.diagnostics.position_diagnostics
    )
    assert all(item.time_stop_due is False for item in result.diagnostics.position_diagnostics)


@pytest.mark.parametrize(
    ("rows", "expected_reason"),
    (
        (
            [
                _position("EURUSD", ticket=101),
                _position("USDJPY", ticket=101, owner_token="fxs-owner-2"),
            ],
            "ticket_duplicate",
        ),
        (
            [
                _position("EURUSD", ticket=101),
                _position("EURUSD", ticket=102, owner_token="fxs-owner-2"),
            ],
            "symbol_duplicate",
        ),
    ),
)
def test_duplicate_ticket_or_symbol_fails_closed(
    rows: list[dict[str, Any]],
    expected_reason: str,
) -> None:
    result = _evaluate(rows)

    assert result.diagnostics.accepted is False
    assert result.close_decisions == ()
    assert sum(
        expected_reason in item.reasons
        for item in result.diagnostics.position_diagnostics
    ) == 2


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        ({"expected_magic": 0}, "expected_magic_invalid"),
        ({"expected_magic": True}, "expected_magic_invalid"),
        (
            {"expected_ownership_contract": "legacy_elbridge_v1"},
            "expected_ownership_contract_invalid",
        ),
        ({"time_stop_bars": 0}, "time_stop_bars_invalid"),
        ({"time_stop_bars": True}, "time_stop_bars_invalid"),
        ({"as_of_epoch": 0}, "as_of_invalid"),
        ({"as_of_epoch": float("nan")}, "as_of_invalid"),
        ({"as_of_epoch": 10**10_000}, "as_of_invalid"),
        (
            {"finalized_common_minute_epoch": 0},
            "finalized_common_minute_invalid",
        ),
        (
            {"finalized_common_minute_epoch": BASE_MINUTE + 1},
            "finalized_common_minute_not_m1_aligned",
        ),
        (
            {
                "finalized_common_minute_epoch": BASE_MINUTE + 19 * 60,
                "as_of_epoch": BASE_MINUTE + 20 * 60 - 0.001,
            },
            "finalized_common_minute_not_closed",
        ),
    ),
)
def test_invalid_global_contract_fails_closed(
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    result = _evaluate(**overrides)

    assert result.diagnostics.accepted is False
    assert expected_reason in result.diagnostics.reasons
    assert result.close_decisions == ()


@pytest.mark.parametrize("positions", ("EURUSD", {"symbol": "EURUSD"}, 42))
def test_non_sequence_position_container_fails_closed(positions: Any) -> None:
    result = _evaluate(positions)

    assert result.diagnostics.accepted is False
    assert "authoritative_owned_positions_invalid" in result.diagnostics.reasons
    assert result.close_decisions == ()


def test_non_mapping_position_row_fails_closed() -> None:
    result = _evaluate([_position(), "not-a-position"])

    assert result.diagnostics.accepted is False
    assert "position_row_invalid" in result.diagnostics.reasons
    assert "position_validation_failed" in result.diagnostics.reasons
    assert result.close_decisions == ()


def test_timezone_aware_iso_and_datetime_inputs_are_parsed_causally() -> None:
    finalized = datetime.fromtimestamp(
        BASE_MINUTE + 19 * 60,
        tz=timezone.utc,
    )
    as_of = datetime.fromtimestamp(BASE_MINUTE + 20 * 60, tz=timezone.utc)
    open_time = datetime.fromtimestamp(BASE_MINUTE + 59, tz=timezone.utc).isoformat()

    result = _evaluate(
        [_position(open_time=open_time)],
        finalized_common_minute_epoch=finalized,
        as_of_epoch=as_of,
    )

    assert result.diagnostics.accepted is True
    assert result.close_decisions[0].bars_held == 20


def test_result_and_decisions_are_frozen_and_have_wire_ready_to_dict() -> None:
    result = _evaluate()
    decision = result.close_decisions[0]

    assert tuple(field.name for field in fields(ScalpTimeStopCloseDecision)) == (
        "symbol",
        "target_ticket",
        "lots",
        "magic",
        "owner_token",
        "ownership_contract",
        "reason",
        "bars_held",
    )
    with pytest.raises(FrozenInstanceError):
        decision.lots = 1.0  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.close_decisions = ()  # type: ignore[misc]

    payload = result.to_dict()
    assert payload["close_decisions"][0] == decision.to_dict()
    assert payload["diagnostics"]["schema_version"] == (
        SCALP_POSITION_LIFECYCLE_SCHEMA_VERSION
    )
    assert result.diagnostics.to_dict()["close_decision_count"] == 1
    assert result.diagnostics.position_diagnostics[0].to_dict()["bars_held"] == 20


def test_lifecycle_module_imports_no_research_or_side_effect_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "runtime"
        / "scalp_position_lifecycle.py"
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
