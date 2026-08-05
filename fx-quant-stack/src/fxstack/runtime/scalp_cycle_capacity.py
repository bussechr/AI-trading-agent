# AGENT: ROLE: Pure cycle-capacity planner for ranked production scalp proposals.
# AGENT: ENTRYPOINT: `plan_scalp_cycle_capacity`.
# AGENT: PRIMARY INPUTS: ranked proposal batch, authoritative occupancy, confirmed exits, hard caps.
# AGENT: PRIMARY OUTPUTS: immutable selected proposals and refusal diagnostics.
# AGENT: STATE / SIDE EFFECTS: none; no sizing, risk, settings, I/O, service, store, runner, or commands.
"""Reserve advisory cycle capacity for ranked scalp proposals.

This planner applies already broker-confirmed exits to the occupancy snapshot,
then walks the batch's existing rank order.  Selection reserves capacity only
inside the returned immutable plan; it does not size, persist, enqueue, or
authorize any trade.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.scalp_proposal_batch import (
    SCALP_PROPOSAL_BATCH_SCHEMA_VERSION,
    ScalpProposalBatchResult,
)
from fxstack.schemas.entry import EntryProposal


SCALP_CYCLE_CAPACITY_SCHEMA_VERSION = "fxstack.runtime.scalp_cycle_capacity.v1"


@dataclass(frozen=True, slots=True)
class ScalpCycleSymbolDiagnostic:
    symbol: str
    proposal_rank: int | None
    had_allowed_proposal: bool
    selected: bool
    refusal_reasons: tuple[str, ...]
    open_before_exits: bool
    confirmed_exit_applied: bool
    open_after_exits: bool
    queued_entry_active: bool
    occupies_projected_capacity: bool


@dataclass(frozen=True, slots=True)
class ScalpCycleCapacityDiagnostics:
    input_valid: bool
    global_refusal_reasons: tuple[str, ...]
    max_total_positions: int | None
    max_pair_positions: int | None
    max_new_entries_per_cycle: int | None
    open_symbols_before_exits: tuple[str, ...]
    projected_broker_confirmed_exit_symbols: tuple[str, ...]
    open_symbols_after_exits: tuple[str, ...]
    active_queued_entry_symbols: tuple[str, ...]
    occupied_slots_before_selection: int
    total_slots_available_before_selection: int
    selected_count: int
    projected_total_positions: int
    symbol_diagnostics: tuple[ScalpCycleSymbolDiagnostic, ...]
    schema_version: str = SCALP_CYCLE_CAPACITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpCycleCapacityPlan:
    selected_proposals: tuple[EntryProposal, ...]
    diagnostics: ScalpCycleCapacityDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _normalize_symbol_input(
    values: Sequence[str],
    *,
    label: str,
) -> tuple[set[str], tuple[str, ...]]:
    reasons: list[str] = []
    normalized: list[str] = []
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return set(), (f"{label}_symbols_invalid",)
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if not symbol:
            reasons.append(f"unknown_{label}_symbol")
            continue
        normalized.append(symbol)
    seen: set[str] = set()
    duplicates: set[str] = set()
    for symbol in normalized:
        if symbol in seen:
            duplicates.add(symbol)
        seen.add(symbol)
    reasons.extend(
        f"duplicate_{label}_symbol:{symbol}" for symbol in sorted(duplicates)
    )
    supported = set(IG_MT4_SCALP_SYMBOLS)
    reasons.extend(
        f"unsupported_{label}_symbol:{symbol}"
        for symbol in sorted(seen - supported)
    )
    return seen, tuple(reasons)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _proposal_rank_key(
    proposal: EntryProposal,
) -> tuple[float, float, float, str, str] | None:
    p_star = _finite_float(proposal.p_star)
    disp_z = _finite_float(proposal.disp_z)
    spread_bps = _finite_float(proposal.spread_bps)
    symbol = str(proposal.symbol or "").strip().upper()
    side = str(proposal.side or "").strip().upper()
    if (
        p_star is None
        or not 0.0 <= p_star <= 1.0
        or disp_z is None
        or spread_bps is None
        or spread_bps < 0.0
        or not symbol
        or side not in {"BUY", "SELL"}
    ):
        return None
    return (p_star, -abs(disp_z), spread_bps, symbol, side)


def _batch_reasons(batch: ScalpProposalBatchResult) -> tuple[str, ...]:
    reasons: list[str] = []
    diagnostics = batch.diagnostics
    if diagnostics.schema_version != SCALP_PROPOSAL_BATCH_SCHEMA_VERSION:
        reasons.append("proposal_batch_schema_invalid")
    if not diagnostics.accepted or diagnostics.reasons:
        reasons.append("proposal_batch_not_accepted")
    if (
        diagnostics.expected_symbols != IG_MT4_SCALP_SYMBOLS
        or len(diagnostics.observed_symbols) != len(IG_MT4_SCALP_SYMBOLS)
        or set(diagnostics.observed_symbols) != set(IG_MT4_SCALP_SYMBOLS)
    ):
        reasons.append("proposal_batch_scope_invalid")
    diagnostic_symbols = tuple(
        str(item.symbol or "").strip().upper()
        for item in diagnostics.symbol_diagnostics
    )
    if (
        diagnostic_symbols != IG_MT4_SCALP_SYMBOLS
    ):
        reasons.append("proposal_batch_symbol_diagnostics_invalid")

    diagnostic_by_symbol = {
        str(item.symbol or "").strip().upper(): item
        for item in diagnostics.symbol_diagnostics
    }
    for symbol in IG_MT4_SCALP_SYMBOLS:
        diagnostic = diagnostic_by_symbol.get(symbol)
        if diagnostic is None:
            continue
        structurally_consistent = bool(diagnostic.structural_ready) == (
            not diagnostic.structural_reasons
        )
        evaluation_consistent = (
            diagnostic.evaluation_allowed is None
            and not diagnostic.evaluation_reasons
            if not diagnostic.structural_ready
            else isinstance(diagnostic.evaluation_allowed, bool)
        )
        if not structurally_consistent or not evaluation_consistent:
            reasons.append(
                f"proposal_batch_symbol_availability_invalid:{symbol}"
            )

    proposal_symbols: list[str] = []
    rank_keys: list[tuple[float, float, float, str, str]] = []
    for proposal in batch.proposals:
        symbol = str(proposal.symbol or "").strip().upper()
        proposal_symbols.append(symbol)
        if symbol not in set(IG_MT4_SCALP_SYMBOLS):
            reasons.append(f"unsupported_proposal_symbol:{symbol or 'UNKNOWN'}")
        if not proposal.allowed or proposal.reasons:
            reasons.append(f"proposal_not_allowed:{symbol or 'UNKNOWN'}")
        if (
            proposal.qualification != "candidate_unqualified"
            or proposal.execution_qualified
            or proposal.win_probability is not None
        ):
            reasons.append(f"proposal_qualification_invalid:{symbol or 'UNKNOWN'}")
        rank_key = _proposal_rank_key(proposal)
        if rank_key is None:
            reasons.append(f"proposal_rank_fields_invalid:{symbol or 'UNKNOWN'}")
        else:
            rank_keys.append(rank_key)
    duplicate_proposals = {
        symbol for symbol in proposal_symbols if proposal_symbols.count(symbol) > 1
    }
    reasons.extend(
        f"duplicate_proposal_symbol:{symbol or 'UNKNOWN'}"
        for symbol in sorted(duplicate_proposals)
    )
    if len(rank_keys) == len(batch.proposals) and rank_keys != sorted(rank_keys):
        reasons.append("proposal_batch_rank_order_invalid")
    proposal_symbol_set = set(proposal_symbols)
    for symbol in IG_MT4_SCALP_SYMBOLS:
        diagnostic = diagnostic_by_symbol.get(symbol)
        if diagnostic is None:
            continue
        has_proposal = symbol in proposal_symbol_set
        if has_proposal and (
            not diagnostic.structural_ready
            or diagnostic.evaluation_allowed is not True
        ):
            reasons.append(f"proposal_batch_proposal_diagnostic_mismatch:{symbol}")
    return tuple(dict.fromkeys(reasons))


def _invalid_plan(
    *,
    reasons: tuple[str, ...],
    max_total_positions: int | None,
    max_pair_positions: int | None,
    max_new_entries_per_cycle: int | None,
    open_symbols: set[str],
    queued_symbols: set[str],
    exit_symbols: set[str],
) -> ScalpCycleCapacityPlan:
    open_after = open_symbols - exit_symbols
    occupied = open_after | queued_symbols
    symbol_diagnostics = tuple(
        ScalpCycleSymbolDiagnostic(
            symbol=symbol,
            proposal_rank=None,
            had_allowed_proposal=False,
            selected=False,
            refusal_reasons=("planner_input_invalid",),
            open_before_exits=symbol in open_symbols,
            confirmed_exit_applied=symbol in exit_symbols,
            open_after_exits=symbol in open_after,
            queued_entry_active=symbol in queued_symbols,
            occupies_projected_capacity=symbol in occupied,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    total_cap = max_total_positions if max_total_positions is not None else 0
    return ScalpCycleCapacityPlan(
        selected_proposals=(),
        diagnostics=ScalpCycleCapacityDiagnostics(
            input_valid=False,
            global_refusal_reasons=reasons,
            max_total_positions=max_total_positions,
            max_pair_positions=max_pair_positions,
            max_new_entries_per_cycle=max_new_entries_per_cycle,
            open_symbols_before_exits=tuple(sorted(open_symbols)),
            projected_broker_confirmed_exit_symbols=tuple(sorted(exit_symbols)),
            open_symbols_after_exits=tuple(sorted(open_after)),
            active_queued_entry_symbols=tuple(sorted(queued_symbols)),
            occupied_slots_before_selection=len(occupied),
            total_slots_available_before_selection=max(0, total_cap - len(occupied)),
            selected_count=0,
            projected_total_positions=len(occupied),
            symbol_diagnostics=symbol_diagnostics,
        ),
    )


def plan_scalp_cycle_capacity(
    *,
    proposal_batch: ScalpProposalBatchResult,
    authoritative_open_symbols: Sequence[str],
    active_queued_entry_symbols: Sequence[str],
    projected_broker_confirmed_exit_symbols: Sequence[str],
    max_total_positions: int,
    max_pair_positions: int,
    max_new_entries_per_cycle: int,
) -> ScalpCycleCapacityPlan:
    """Apply confirmed exits, then reserve ranked advisory entry capacity."""

    total_cap = _strict_nonnegative_int(max_total_positions)
    pair_cap = _strict_nonnegative_int(max_pair_positions)
    cycle_cap = _strict_nonnegative_int(max_new_entries_per_cycle)
    open_symbols, open_reasons = _normalize_symbol_input(
        authoritative_open_symbols,
        label="open_position",
    )
    queued_symbols, queued_reasons = _normalize_symbol_input(
        active_queued_entry_symbols,
        label="queued_entry",
    )
    exit_symbols, exit_reasons = _normalize_symbol_input(
        projected_broker_confirmed_exit_symbols,
        label="projected_exit",
    )

    reasons: list[str] = []
    if total_cap is None:
        reasons.append("max_total_positions_invalid")
    if pair_cap != 1:
        reasons.append("max_pair_positions_must_equal_one")
    if cycle_cap is None:
        reasons.append("max_new_entries_per_cycle_invalid")
    reasons.extend(open_reasons)
    reasons.extend(queued_reasons)
    reasons.extend(exit_reasons)
    reasons.extend(
        f"unknown_projected_exit_position:{symbol}"
        for symbol in sorted(exit_symbols - open_symbols)
        if symbol in set(IG_MT4_SCALP_SYMBOLS)
    )
    reasons.extend(
        f"open_and_queued_symbol_overlap:{symbol}"
        for symbol in sorted(open_symbols & queued_symbols)
    )
    reasons.extend(_batch_reasons(proposal_batch))
    if reasons:
        return _invalid_plan(
            reasons=tuple(dict.fromkeys(reasons)),
            max_total_positions=total_cap,
            max_pair_positions=pair_cap,
            max_new_entries_per_cycle=cycle_cap,
            open_symbols=open_symbols,
            queued_symbols=queued_symbols,
            exit_symbols=exit_symbols,
        )

    assert total_cap is not None
    assert pair_cap == 1
    assert cycle_cap is not None
    open_after = open_symbols - exit_symbols
    occupied = set(open_after) | set(queued_symbols)
    occupied_before_selection = len(occupied)
    proposal_by_symbol = {
        str(proposal.symbol).strip().upper(): proposal
        for proposal in proposal_batch.proposals
    }
    proposal_rank = {
        str(proposal.symbol).strip().upper(): rank
        for rank, proposal in enumerate(proposal_batch.proposals, start=1)
    }
    upstream_diagnostic_by_symbol = {
        str(item.symbol or "").strip().upper(): item
        for item in proposal_batch.diagnostics.symbol_diagnostics
    }
    selected: list[EntryProposal] = []
    proposal_refusals: dict[str, tuple[str, ...]] = {}

    for proposal in proposal_batch.proposals:
        symbol = str(proposal.symbol).strip().upper()
        if symbol in open_after:
            proposal_refusals[symbol] = ("active_open_position",)
            continue
        if symbol in queued_symbols:
            proposal_refusals[symbol] = ("active_queued_entry",)
            continue
        if len(selected) >= cycle_cap:
            proposal_refusals[symbol] = ("cycle_entry_capacity_exhausted",)
            continue
        if len(occupied) >= total_cap:
            proposal_refusals[symbol] = ("total_position_capacity_exhausted",)
            continue
        selected.append(proposal)
        occupied.add(symbol)

    selected_symbols = {
        str(proposal.symbol).strip().upper() for proposal in selected
    }
    symbol_diagnostics: list[ScalpCycleSymbolDiagnostic] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        has_proposal = symbol in proposal_by_symbol
        is_selected = symbol in selected_symbols
        if is_selected:
            refusal_reasons: tuple[str, ...] = ()
        elif has_proposal:
            refusal_reasons = proposal_refusals.get(symbol, ("capacity_refusal",))
        elif symbol in open_after:
            refusal_reasons = ("active_open_position",)
        elif symbol in queued_symbols:
            refusal_reasons = ("active_queued_entry",)
        else:
            upstream = upstream_diagnostic_by_symbol[symbol]
            if not upstream.structural_ready:
                refusal_reasons = tuple(
                    f"structural_unready:{reason}"
                    for reason in upstream.structural_reasons
                ) or ("structural_unready",)
            else:
                refusal_reasons = ("no_allowed_proposal",)
        symbol_diagnostics.append(
            ScalpCycleSymbolDiagnostic(
                symbol=symbol,
                proposal_rank=proposal_rank.get(symbol),
                had_allowed_proposal=has_proposal,
                selected=is_selected,
                refusal_reasons=refusal_reasons,
                open_before_exits=symbol in open_symbols,
                confirmed_exit_applied=symbol in exit_symbols,
                open_after_exits=symbol in open_after,
                queued_entry_active=symbol in queued_symbols,
                occupies_projected_capacity=symbol in occupied,
            )
        )

    diagnostics = ScalpCycleCapacityDiagnostics(
        input_valid=True,
        global_refusal_reasons=(),
        max_total_positions=total_cap,
        max_pair_positions=pair_cap,
        max_new_entries_per_cycle=cycle_cap,
        open_symbols_before_exits=tuple(sorted(open_symbols)),
        projected_broker_confirmed_exit_symbols=tuple(sorted(exit_symbols)),
        open_symbols_after_exits=tuple(sorted(open_after)),
        active_queued_entry_symbols=tuple(sorted(queued_symbols)),
        occupied_slots_before_selection=occupied_before_selection,
        total_slots_available_before_selection=max(
            0,
            total_cap - occupied_before_selection,
        ),
        selected_count=len(selected),
        projected_total_positions=len(occupied),
        symbol_diagnostics=tuple(symbol_diagnostics),
    )
    return ScalpCycleCapacityPlan(
        selected_proposals=tuple(selected),
        diagnostics=diagnostics,
    )


__all__ = [
    "SCALP_CYCLE_CAPACITY_SCHEMA_VERSION",
    "ScalpCycleCapacityDiagnostics",
    "ScalpCycleCapacityPlan",
    "ScalpCycleSymbolDiagnostic",
    "plan_scalp_cycle_capacity",
]
