from __future__ import annotations

import ast
from dataclasses import fields, replace
from pathlib import Path
from typing import Any, Literal

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    get_ig_mt4_instrument,
)
from fxstack.runtime.scalp_cycle_capacity import (
    ScalpCycleCapacityPlan,
    plan_scalp_cycle_capacity,
)
from fxstack.runtime.scalp_proposal_batch import (
    BRIDGE_M1_BAR_SOURCE_ID,
    BRIDGE_M1_BAR_SOURCE_VERSION,
    ScalpProposalBatchDiagnostics,
    ScalpProposalBatchResult,
    ScalpSymbolProposalDiagnostic,
)
from fxstack.schemas.entry import EntryProposal


MINUTE_EPOCH = 1_800_000_000


def _proposal(
    symbol: str,
    *,
    p_star: float = 0.30,
    disp_z: float = 2.0,
    spread_bps: float = 0.6,
    side: Literal["BUY", "SELL"] = "SELL",
    allowed: bool = True,
) -> EntryProposal:
    identity = get_ig_mt4_instrument(symbol)
    return EntryProposal(
        strategy_id="scalp_dislocation",
        strategy_version="v1",
        config_sha256="a" * 64,
        symbol=symbol,
        instrument_id=(
            identity.instrument_id if identity is not None else f"fx:ig_mt4:{symbol}"
        ),
        venue_id=identity.venue if identity is not None else "ig_mt4",
        source_id=BRIDGE_M1_BAR_SOURCE_ID,
        source_version=BRIDGE_M1_BAR_SOURCE_VERSION,
        allowed=allowed,
        reasons=() if allowed else ("upstream_refusal",),
        side=side if allowed else None,
        minute_epoch=MINUTE_EPOCH if allowed else None,
        ref_mid=1.1 if allowed else None,
        entry_price=1.1 if allowed else None,
        sl_price=1.101 if allowed else None,
        tp_price=1.099 if allowed else None,
        atr_bps=4.0 if allowed else None,
        stop_bps=4.5 if allowed else None,
        target_bps=6.0 if allowed else None,
        disp_z=disp_z if allowed else None,
        spread_bps=spread_bps if allowed else None,
        p_star=p_star if allowed else None,
        time_stop_bars=20 if allowed else None,
        entry_deadline_epoch=MINUTE_EPOCH + 5 if allowed else None,
    )


def _ranked_proposals(
    symbols: tuple[str, ...] = IG_MT4_SCALP_SYMBOLS,
) -> tuple[EntryProposal, ...]:
    return tuple(
        _proposal(symbol, p_star=0.20 + index / 1000.0)
        for index, symbol in enumerate(symbols)
    )


def _batch(
    proposals: tuple[EntryProposal, ...] | None = None,
    *,
    structural_unready: dict[str, tuple[str, ...]] | None = None,
) -> ScalpProposalBatchResult:
    selected = _ranked_proposals() if proposals is None else proposals
    unavailable = dict(structural_unready or {})
    proposal_symbols = {proposal.symbol for proposal in selected if proposal.allowed}
    symbol_diagnostics = tuple(
        ScalpSymbolProposalDiagnostic(
            symbol=symbol,
            structural_ready=symbol not in unavailable,
            structural_reasons=unavailable.get(symbol, ()),
            raw_row_count=0 if symbol in unavailable else 21,
            filtered_current_bar_count=0 if symbol in unavailable else 1,
            finalized_row_count=0 if symbol in unavailable else 20,
            selected_history_count=0 if symbol in unavailable else 20,
            latest_finalized_minute_epoch=(
                None if symbol in unavailable else MINUTE_EPOCH
            ),
            evaluation_allowed=(
                None if symbol in unavailable else symbol in proposal_symbols
            ),
            evaluation_reasons=(
                ()
                if symbol in unavailable or symbol in proposal_symbols
                else ("no_dislocation",)
            ),
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    return ScalpProposalBatchResult(
        proposals=selected,
        diagnostics=ScalpProposalBatchDiagnostics(
            accepted=True,
            reasons=(),
            source_id=BRIDGE_M1_BAR_SOURCE_ID,
            source_version=BRIDGE_M1_BAR_SOURCE_VERSION,
            as_of_epoch=MINUTE_EPOCH + 30.0,
            current_minute_epoch=MINUTE_EPOCH,
            common_closed_minute_epoch=MINUTE_EPOCH - 60,
            expected_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_symbols=tuple(sorted(IG_MT4_SCALP_SYMBOLS)),
            filtered_current_bar_count=22,
            symbol_diagnostics=symbol_diagnostics,
        ),
    )


def _plan(
    *,
    proposal_batch: ScalpProposalBatchResult | None = None,
    authoritative_open_symbols: Any = (),
    active_queued_entry_symbols: Any = (),
    projected_broker_confirmed_exit_symbols: Any = (),
    max_total_positions: Any = 6,
    max_pair_positions: Any = 1,
    max_new_entries_per_cycle: Any = 3,
) -> ScalpCycleCapacityPlan:
    return plan_scalp_cycle_capacity(
        proposal_batch=_batch() if proposal_batch is None else proposal_batch,
        authoritative_open_symbols=authoritative_open_symbols,
        active_queued_entry_symbols=active_queued_entry_symbols,
        projected_broker_confirmed_exit_symbols=(
            projected_broker_confirmed_exit_symbols
        ),
        max_total_positions=max_total_positions,
        max_pair_positions=max_pair_positions,
        max_new_entries_per_cycle=max_new_entries_per_cycle,
    )


def _diagnostic(plan: ScalpCycleCapacityPlan, symbol: str) -> Any:
    return next(
        item for item in plan.diagnostics.symbol_diagnostics if item.symbol == symbol
    )


def test_confirmed_exits_apply_first_and_each_selection_reserves_capacity() -> None:
    plan = _plan(
        authoritative_open_symbols=("EURUSD", "USDJPY"),
        active_queued_entry_symbols=("AUDUSD",),
        projected_broker_confirmed_exit_symbols=("EURUSD",),
        max_total_positions=5,
        max_new_entries_per_cycle=3,
    )

    assert tuple(item.symbol for item in plan.selected_proposals) == (
        "EURUSD",
        "GBPUSD",
        "USDCAD",
    )
    assert plan.diagnostics.input_valid is True
    assert plan.diagnostics.global_refusal_reasons == ()
    assert plan.diagnostics.open_symbols_after_exits == ("USDJPY",)
    assert plan.diagnostics.occupied_slots_before_selection == 2
    assert plan.diagnostics.total_slots_available_before_selection == 3
    assert plan.diagnostics.selected_count == 3
    assert plan.diagnostics.projected_total_positions == 5
    assert len(plan.diagnostics.symbol_diagnostics) == 22
    assert tuple(item.symbol for item in plan.diagnostics.symbol_diagnostics) == (
        IG_MT4_SCALP_SYMBOLS
    )

    exited = _diagnostic(plan, "EURUSD")
    assert exited.open_before_exits is True
    assert exited.confirmed_exit_applied is True
    assert exited.open_after_exits is False
    assert exited.selected is True
    assert exited.occupies_projected_capacity is True

    still_open = _diagnostic(plan, "USDJPY")
    assert still_open.selected is False
    assert still_open.refusal_reasons == ("active_open_position",)
    assert still_open.occupies_projected_capacity is True

    pending = _diagnostic(plan, "AUDUSD")
    assert pending.selected is False
    assert pending.refusal_reasons == ("active_queued_entry",)
    assert pending.occupies_projected_capacity is True

    assert not {
        proposal.symbol for proposal in plan.selected_proposals
    } & {"USDJPY", "AUDUSD"}


def test_selection_preserves_the_already_ranked_batch_order() -> None:
    proposals = (
        _proposal("BTCUSD", p_star=0.20),
        _proposal("GBPUSD", p_star=0.30),
        _proposal("EURUSD", p_star=0.40),
    )

    plan = _plan(
        proposal_batch=_batch(proposals),
        max_total_positions=2,
        max_new_entries_per_cycle=2,
    )

    assert tuple(item.symbol for item in plan.selected_proposals) == (
        "BTCUSD",
        "GBPUSD",
    )
    assert _diagnostic(plan, "BTCUSD").proposal_rank == 1
    assert _diagnostic(plan, "GBPUSD").proposal_rank == 2
    assert _diagnostic(plan, "EURUSD").proposal_rank == 3


def test_capacity_accepts_a_downstream_qualification_filtered_batch() -> None:
    evaluated = _batch()
    qualified = replace(
        evaluated,
        proposals=(evaluated.proposals[18], evaluated.proposals[19]),
    )

    plan = _plan(
        proposal_batch=qualified,
        max_total_positions=2,
        max_new_entries_per_cycle=2,
    )

    assert plan.diagnostics.input_valid is True
    assert tuple(item.symbol for item in plan.selected_proposals) == (
        "BTCUSD",
        "ETHUSD",
    )


def test_structural_unready_is_distinct_from_an_ordinary_no_signal() -> None:
    proposals = (
        _proposal("BTCUSD", p_star=0.20),
        _proposal("ETHUSD", p_star=0.30),
    )
    batch = _batch(
        proposals,
        structural_unready={
            "EURUSD": (
                "common_closed_minute_missing",
                "insufficient_finalized_history",
            )
        },
    )

    plan = _plan(
        proposal_batch=batch,
        max_total_positions=2,
        max_new_entries_per_cycle=2,
    )

    assert tuple(item.symbol for item in plan.selected_proposals) == (
        "BTCUSD",
        "ETHUSD",
    )
    assert _diagnostic(plan, "EURUSD").refusal_reasons == (
        "structural_unready:common_closed_minute_missing",
        "structural_unready:insufficient_finalized_history",
    )
    assert _diagnostic(plan, "GBPUSD").refusal_reasons == (
        "no_allowed_proposal",
    )


def test_empty_cycle_can_reserve_the_complete_exact_22_symbol_scope() -> None:
    plan = _plan(
        max_total_positions=len(IG_MT4_SCALP_SYMBOLS),
        max_new_entries_per_cycle=len(IG_MT4_SCALP_SYMBOLS),
    )

    assert tuple(item.symbol for item in plan.selected_proposals) == (
        IG_MT4_SCALP_SYMBOLS
    )
    assert plan.diagnostics.selected_count == 22
    assert plan.diagnostics.projected_total_positions == 22
    assert all(item.selected for item in plan.diagnostics.symbol_diagnostics)


def test_position_input_order_does_not_change_selection_or_diagnostics() -> None:
    forward = _plan(
        authoritative_open_symbols=("EURUSD", "USDJPY", "GBPUSD"),
        active_queued_entry_symbols=("AUDUSD", "USDCAD"),
        projected_broker_confirmed_exit_symbols=("EURUSD", "GBPUSD"),
        max_total_positions=6,
        max_new_entries_per_cycle=3,
    )
    reversed_inputs = _plan(
        authoritative_open_symbols=("GBPUSD", "USDJPY", "EURUSD"),
        active_queued_entry_symbols=("USDCAD", "AUDUSD"),
        projected_broker_confirmed_exit_symbols=("GBPUSD", "EURUSD"),
        max_total_positions=6,
        max_new_entries_per_cycle=3,
    )

    assert forward.to_dict() == reversed_inputs.to_dict()


@pytest.mark.parametrize(
    ("overrides", "expected_reason"),
    (
        (
            {"authoritative_open_symbols": ("EURUSD", "eurusd")},
            "duplicate_open_position_symbol:EURUSD",
        ),
        (
            {"authoritative_open_symbols": ("NOTIG",)},
            "unsupported_open_position_symbol:NOTIG",
        ),
        (
            {"authoritative_open_symbols": ("",)},
            "unknown_open_position_symbol",
        ),
        (
            {"active_queued_entry_symbols": ("AUDUSD", "audusd")},
            "duplicate_queued_entry_symbol:AUDUSD",
        ),
        (
            {"active_queued_entry_symbols": ("NOTIG",)},
            "unsupported_queued_entry_symbol:NOTIG",
        ),
        (
            {"active_queued_entry_symbols": (None,)},
            "unknown_queued_entry_symbol",
        ),
        (
            {
                "authoritative_open_symbols": ("EURUSD",),
                "projected_broker_confirmed_exit_symbols": (
                    "EURUSD",
                    "eurusd",
                ),
            },
            "duplicate_projected_exit_symbol:EURUSD",
        ),
        (
            {"projected_broker_confirmed_exit_symbols": ("NOTIG",)},
            "unsupported_projected_exit_symbol:NOTIG",
        ),
        (
            {"projected_broker_confirmed_exit_symbols": ("EURUSD",)},
            "unknown_projected_exit_position:EURUSD",
        ),
        (
            {
                "authoritative_open_symbols": ("EURUSD",),
                "active_queued_entry_symbols": ("eurusd",),
            },
            "open_and_queued_symbol_overlap:EURUSD",
        ),
        (
            {"authoritative_open_symbols": "EURUSD"},
            "open_position_symbols_invalid",
        ),
        ({"max_total_positions": -1}, "max_total_positions_invalid"),
        ({"max_total_positions": 1.5}, "max_total_positions_invalid"),
        ({"max_total_positions": True}, "max_total_positions_invalid"),
        ({"max_pair_positions": 0}, "max_pair_positions_must_equal_one"),
        ({"max_pair_positions": 2}, "max_pair_positions_must_equal_one"),
        ({"max_pair_positions": True}, "max_pair_positions_must_equal_one"),
        (
            {"max_new_entries_per_cycle": -1},
            "max_new_entries_per_cycle_invalid",
        ),
        (
            {"max_new_entries_per_cycle": 1.5},
            "max_new_entries_per_cycle_invalid",
        ),
        (
            {"max_new_entries_per_cycle": False},
            "max_new_entries_per_cycle_invalid",
        ),
    ),
)
def test_invalid_positions_and_caps_fail_closed(
    overrides: dict[str, Any],
    expected_reason: str,
) -> None:
    plan = _plan(**overrides)

    assert plan.selected_proposals == ()
    assert plan.diagnostics.input_valid is False
    assert expected_reason in plan.diagnostics.global_refusal_reasons
    assert len(plan.diagnostics.symbol_diagnostics) == 22
    assert all(
        item.refusal_reasons == ("planner_input_invalid",)
        for item in plan.diagnostics.symbol_diagnostics
    )


def test_zero_cycle_cap_is_a_valid_hard_refusal() -> None:
    plan = _plan(max_total_positions=6, max_new_entries_per_cycle=0)

    assert plan.diagnostics.input_valid is True
    assert plan.selected_proposals == ()
    assert plan.diagnostics.projected_total_positions == 0
    assert all(
        item.refusal_reasons == ("cycle_entry_capacity_exhausted",)
        for item in plan.diagnostics.symbol_diagnostics
    )


def test_zero_total_cap_is_a_valid_hard_refusal() -> None:
    plan = _plan(max_total_positions=0, max_new_entries_per_cycle=3)

    assert plan.diagnostics.input_valid is True
    assert plan.selected_proposals == ()
    assert plan.diagnostics.total_slots_available_before_selection == 0
    assert all(
        item.refusal_reasons == ("total_position_capacity_exhausted",)
        for item in plan.diagnostics.symbol_diagnostics
    )


def test_existing_occupancy_at_total_cap_blocks_every_new_entry() -> None:
    plan = _plan(
        authoritative_open_symbols=("EURUSD",),
        active_queued_entry_symbols=("USDJPY",),
        max_total_positions=2,
        max_new_entries_per_cycle=22,
    )

    assert plan.selected_proposals == ()
    assert _diagnostic(plan, "EURUSD").refusal_reasons == (
        "active_open_position",
    )
    assert _diagnostic(plan, "USDJPY").refusal_reasons == (
        "active_queued_entry",
    )
    assert _diagnostic(plan, "AUDUSD").refusal_reasons == (
        "total_position_capacity_exhausted",
    )


def test_invalid_batch_variants_fail_closed() -> None:
    valid = _batch()
    duplicate = replace(
        valid,
        proposals=(valid.proposals[0], valid.proposals[0], *valid.proposals[1:]),
    )
    unsupported = replace(
        valid,
        proposals=(_proposal("NOTIG", p_star=0.10), *valid.proposals),
    )
    refused = replace(
        valid,
        proposals=(_proposal("EURUSD", allowed=False),),
    )
    unranked = replace(
        valid,
        proposals=(valid.proposals[1], valid.proposals[0], *valid.proposals[2:]),
    )
    rejected = replace(
        valid,
        diagnostics=replace(
            valid.diagnostics,
            accepted=False,
            reasons=("upstream_batch_refused",),
        ),
    )
    wrong_scope = replace(
        valid,
        diagnostics=replace(
            valid.diagnostics,
            expected_symbols=IG_MT4_SCALP_SYMBOLS[:-1],
        ),
    )
    wrong_scope_order = replace(
        valid,
        diagnostics=replace(
            valid.diagnostics,
            expected_symbols=tuple(reversed(IG_MT4_SCALP_SYMBOLS)),
        ),
    )
    wrong_diagnostics = replace(
        valid,
        diagnostics=replace(
            valid.diagnostics,
            symbol_diagnostics=valid.diagnostics.symbol_diagnostics[:-1],
        ),
    )
    wrong_diagnostic_order = replace(
        valid,
        diagnostics=replace(
            valid.diagnostics,
            symbol_diagnostics=tuple(
                reversed(valid.diagnostics.symbol_diagnostics)
            ),
        ),
    )
    wrong_schema = replace(
        valid,
        diagnostics=replace(valid.diagnostics, schema_version="wrong"),
    )

    cases = (
        (duplicate, "duplicate_proposal_symbol:EURUSD"),
        (unsupported, "unsupported_proposal_symbol:NOTIG"),
        (refused, "proposal_not_allowed:EURUSD"),
        (unranked, "proposal_batch_rank_order_invalid"),
        (rejected, "proposal_batch_not_accepted"),
        (wrong_scope, "proposal_batch_scope_invalid"),
        (wrong_scope_order, "proposal_batch_scope_invalid"),
        (wrong_diagnostics, "proposal_batch_symbol_diagnostics_invalid"),
        (
            wrong_diagnostic_order,
            "proposal_batch_symbol_diagnostics_invalid",
        ),
        (wrong_schema, "proposal_batch_schema_invalid"),
    )
    for batch, expected_reason in cases:
        plan = _plan(proposal_batch=batch)

        assert plan.selected_proposals == ()
        assert plan.diagnostics.input_valid is False
        assert expected_reason in plan.diagnostics.global_refusal_reasons


def test_plan_contract_contains_only_immutable_selection_and_diagnostics() -> None:
    plan = _plan(max_new_entries_per_cycle=1)

    assert tuple(field.name for field in fields(ScalpCycleCapacityPlan)) == (
        "selected_proposals",
        "diagnostics",
    )
    with pytest.raises(AttributeError):
        plan.selected_proposals = ()  # type: ignore[misc]
    assert plan.selected_proposals[0].execution_qualified is False
    assert plan.selected_proposals[0].qualification == "candidate_unqualified"


def test_capacity_planner_imports_no_authority_or_side_effect_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "runtime"
        / "scalp_cycle_capacity.py"
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
        "fxstack.risk",
        "fxstack.settings",
        "fxstack.runtime.runner",
        "fxstack.runtime.service",
        "fxstack.runtime.postgres_store",
        "fxstack.runtime.dto",
        "fxstack.runtime.protocol",
    )
    assert all(
        not any(module == root or module.startswith(f"{root}.") for root in forbidden)
        for module in imported
    )
