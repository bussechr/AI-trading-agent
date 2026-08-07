# AGENT: ROLE: Pure cycle-capacity planner for ranked MTVCLC candidates.
# AGENT: ENTRYPOINT: `plan_mtvclc_cycle_capacity`.
# AGENT: PRIMARY INPUTS: exact-scope MTVCLC batch, authoritative occupancy, confirmed exits, hard caps.
# AGENT: PRIMARY OUTPUTS: immutable selected candidates and per-symbol refusal diagnostics.
# AGENT: STATE / SIDE EFFECTS: none; no sizing, persistence, queue, broker, certificate, or settings access.
"""Reserve advisory cycle capacity without changing MTVCLC rank order.

The strategy batch already owns deterministic ranking.  This layer only joins
that ordered candidate list to durable open/queued occupancy and broker-
confirmed exits.  A selected candidate is still unqualified for risk, sizing,
queueing, and broker execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, fields
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import IG_MT4_SCALP_SYMBOLS
from fxstack.runtime.mtvclc_proposal_batch import (
    MTVCLC_PROPOSAL_BATCH_SCHEMA_VERSION,
    MTVCLC_RUNTIME_PROFILE_ID,
    MTVCLCProposalBatchResult,
)
from fxstack.strategy.mtvclc import MTVCLCTradeCandidate


MTVCLC_CYCLE_CAPACITY_SCHEMA_VERSION = (
    "fxstack.runtime.mtvclc_cycle_capacity.v1"
)


@dataclass(frozen=True, slots=True)
class MTVCLCCycleSymbolDiagnostic:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            name: getattr(self, name)
            for name in _CYCLE_SYMBOL_DIAGNOSTIC_FIELD_ORDER
        }


_CYCLE_SYMBOL_DIAGNOSTIC_FIELD_ORDER = (
    "symbol",
    "proposal_rank",
    "had_allowed_proposal",
    "selected",
    "refusal_reasons",
    "open_before_exits",
    "confirmed_exit_applied",
    "open_after_exits",
    "queued_entry_active",
    "occupies_projected_capacity",
)
if tuple(item.name for item in fields(MTVCLCCycleSymbolDiagnostic)) != (
    _CYCLE_SYMBOL_DIAGNOSTIC_FIELD_ORDER
):
    raise RuntimeError("MTVCLC cycle symbol diagnostic field order drifted")


@dataclass(frozen=True, slots=True)
class MTVCLCCycleCapacityDiagnostics:
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
    symbol_diagnostics: tuple[MTVCLCCycleSymbolDiagnostic, ...]
    schema_version: str = MTVCLC_CYCLE_CAPACITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        payload = {item.name: getattr(self, item.name) for item in fields(self)}
        payload["symbol_diagnostics"] = tuple(
            diagnostic.to_dict() for diagnostic in self.symbol_diagnostics
        )
        return payload

    def to_cycle_summary(self) -> dict[str, Any]:
        """Return capacity totals; per-symbol evidence lives in decisions."""

        payload = {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name != "symbol_diagnostics"
        }
        payload["symbol_diagnostic_count"] = len(self.symbol_diagnostics)
        return payload


@dataclass(frozen=True, slots=True)
class MTVCLCCycleCapacityPlan:
    selected_proposals: tuple[MTVCLCTradeCandidate, ...]
    diagnostics: MTVCLCCycleCapacityDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_proposals": tuple(
                proposal.to_dict() for proposal in self.selected_proposals
            ),
            "diagnostics": self.diagnostics.to_dict(),
        }

    def to_cycle_summary(self) -> dict[str, Any]:
        """Return selection identity without duplicating full proposals."""

        return {
            "selected_symbols": tuple(
                proposal.symbol for proposal in self.selected_proposals
            ),
            "diagnostics": self.diagnostics.to_cycle_summary(),
        }


def _strict_nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return int(value)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalize_symbol_input(
    values: Sequence[str],
    *,
    label: str,
) -> tuple[set[str], tuple[str, ...]]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        return set(), (f"{label}_symbols_invalid",)
    normalized: list[str] = []
    reasons: list[str] = []
    for raw in values:
        symbol = str(raw or "").strip().upper()
        if not symbol:
            reasons.append(f"unknown_{label}_symbol")
            continue
        normalized.append(symbol)
    duplicates = {
        symbol for symbol in normalized if normalized.count(symbol) > 1
    }
    reasons.extend(
        f"duplicate_{label}_symbol:{symbol}" for symbol in sorted(duplicates)
    )
    observed = set(normalized)
    supported = set(IG_MT4_SCALP_SYMBOLS)
    reasons.extend(
        f"unsupported_{label}_symbol:{symbol}"
        for symbol in sorted(observed - supported)
    )
    return observed, tuple(reasons)


def _proposal_rank_key(
    proposal: MTVCLCTradeCandidate,
) -> tuple[float, float, float, str, str] | None:
    p_star = _finite(proposal.p_star)
    volume = _finite(proposal.signal_tick_volume)
    v90 = _finite(proposal.volume_v90)
    spread = _finite(proposal.live_spread_bps)
    symbol = str(proposal.symbol or "").strip().upper()
    side = str(proposal.side or "").strip().upper()
    if (
        p_star is None
        or not 0.0 <= p_star <= 1.0
        or volume is None
        or volume < 0.0
        or v90 is None
        or v90 <= 0.0
        or spread is None
        or spread < 0.0
        or not symbol
        or side not in {"BUY", "SELL"}
    ):
        return None
    return (p_star, -(volume / v90), spread, symbol, side)


def _batch_reasons(batch: MTVCLCProposalBatchResult) -> tuple[str, ...]:
    reasons: list[str] = []
    diagnostics = batch.diagnostics
    if diagnostics.schema_version != MTVCLC_PROPOSAL_BATCH_SCHEMA_VERSION:
        reasons.append("proposal_batch_schema_invalid")
    if diagnostics.strategy_profile != MTVCLC_RUNTIME_PROFILE_ID:
        reasons.append("proposal_batch_strategy_profile_invalid")
    if not diagnostics.accepted or diagnostics.reasons:
        reasons.append("proposal_batch_not_accepted")
    exact_scope = tuple(IG_MT4_SCALP_SYMBOLS)
    if (
        diagnostics.expected_symbols != exact_scope
        or diagnostics.observed_bar_symbols != exact_scope
        or diagnostics.observed_quote_symbols != exact_scope
        or diagnostics.observed_cost_symbols != exact_scope
    ):
        reasons.append("proposal_batch_scope_invalid")
    diagnostic_symbols = tuple(
        str(item.symbol or "").strip().upper()
        for item in diagnostics.symbol_diagnostics
    )
    if diagnostic_symbols != exact_scope:
        reasons.append("proposal_batch_symbol_diagnostics_invalid")

    diagnostic_by_symbol = {
        str(item.symbol or "").strip().upper(): item
        for item in diagnostics.symbol_diagnostics
    }
    for symbol in exact_scope:
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
        if symbol not in set(exact_scope):
            reasons.append(f"unsupported_proposal_symbol:{symbol or 'UNKNOWN'}")
        if not proposal.allowed or proposal.reasons:
            reasons.append(f"proposal_not_allowed:{symbol or 'UNKNOWN'}")
        if (
            proposal.qualification != "candidate_unqualified"
            or proposal.execution_qualified
            or proposal.win_probability is not None
            or proposal.release_authorized
            or proposal.activation_authorized
            or proposal.queue_authorized
            or proposal.broker_trade_authorized
        ):
            reasons.append(f"proposal_qualification_invalid:{symbol or 'UNKNOWN'}")
        if (
            proposal.execution_type != "market"
            or proposal.immediate_market_trade is not True
            or proposal.pending_orders_forbidden is not True
        ):
            reasons.append(f"proposal_execution_contract_invalid:{symbol or 'UNKNOWN'}")
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
    for symbol in exact_scope:
        diagnostic = diagnostic_by_symbol.get(symbol)
        if diagnostic is None:
            continue
        if symbol in proposal_symbol_set and (
            not diagnostic.structural_ready
            or diagnostic.evaluation_allowed is not True
        ):
            reasons.append(
                f"proposal_batch_proposal_diagnostic_mismatch:{symbol}"
            )
    return tuple(dict.fromkeys(reasons))


def _plan(
    *,
    input_valid: bool,
    global_reasons: tuple[str, ...],
    selected: tuple[MTVCLCTradeCandidate, ...],
    proposal_by_symbol: dict[str, MTVCLCTradeCandidate],
    proposal_rank: dict[str, int],
    proposal_refusals: dict[str, tuple[str, ...]],
    upstream_by_symbol: dict[str, Any],
    open_symbols: set[str],
    exit_symbols: set[str],
    queued_symbols: set[str],
    occupied_before: int,
    occupied_after: set[str],
    total_cap: int | None,
    pair_cap: int | None,
    cycle_cap: int | None,
) -> MTVCLCCycleCapacityPlan:
    open_after = open_symbols - exit_symbols
    selected_symbols = {
        str(proposal.symbol).strip().upper() for proposal in selected
    }
    symbol_diagnostics: list[MTVCLCCycleSymbolDiagnostic] = []
    for symbol in IG_MT4_SCALP_SYMBOLS:
        has_proposal = symbol in proposal_by_symbol
        is_selected = symbol in selected_symbols
        if not input_valid:
            refusal_reasons = ("planner_input_invalid",)
        elif is_selected:
            refusal_reasons = ()
        elif has_proposal:
            refusal_reasons = proposal_refusals.get(
                symbol, ("capacity_refusal",)
            )
        elif symbol in open_after:
            refusal_reasons = ("active_open_position",)
        elif symbol in queued_symbols:
            refusal_reasons = ("active_queued_entry",)
        else:
            upstream = upstream_by_symbol.get(symbol)
            if upstream is not None and not upstream.structural_ready:
                refusal_reasons = tuple(
                    f"structural_unready:{reason}"
                    for reason in upstream.structural_reasons
                ) or ("structural_unready",)
            else:
                refusal_reasons = ("no_allowed_proposal",)
        symbol_diagnostics.append(
            MTVCLCCycleSymbolDiagnostic(
                symbol=symbol,
                proposal_rank=proposal_rank.get(symbol),
                had_allowed_proposal=has_proposal,
                selected=is_selected,
                refusal_reasons=refusal_reasons,
                open_before_exits=symbol in open_symbols,
                confirmed_exit_applied=symbol in exit_symbols,
                open_after_exits=symbol in open_after,
                queued_entry_active=symbol in queued_symbols,
                occupies_projected_capacity=symbol in occupied_after,
            )
        )
    available = max(0, (total_cap or 0) - occupied_before)
    return MTVCLCCycleCapacityPlan(
        selected_proposals=selected,
        diagnostics=MTVCLCCycleCapacityDiagnostics(
            input_valid=input_valid,
            global_refusal_reasons=global_reasons,
            max_total_positions=total_cap,
            max_pair_positions=pair_cap,
            max_new_entries_per_cycle=cycle_cap,
            open_symbols_before_exits=tuple(sorted(open_symbols)),
            projected_broker_confirmed_exit_symbols=tuple(sorted(exit_symbols)),
            open_symbols_after_exits=tuple(sorted(open_after)),
            active_queued_entry_symbols=tuple(sorted(queued_symbols)),
            occupied_slots_before_selection=occupied_before,
            total_slots_available_before_selection=available,
            selected_count=len(selected),
            projected_total_positions=len(occupied_after),
            symbol_diagnostics=tuple(symbol_diagnostics),
        ),
    )


def plan_mtvclc_cycle_capacity(
    *,
    proposal_batch: MTVCLCProposalBatchResult,
    authoritative_open_symbols: Sequence[str],
    active_queued_entry_symbols: Sequence[str],
    projected_broker_confirmed_exit_symbols: Sequence[str],
    max_total_positions: int,
    max_pair_positions: int,
    max_new_entries_per_cycle: int,
) -> MTVCLCCycleCapacityPlan:
    """Apply confirmed exits, then reserve the batch's existing rank order."""

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

    open_after = open_symbols - exit_symbols
    occupied = set(open_after) | set(queued_symbols)
    occupied_before = len(occupied)
    proposal_by_symbol = {
        str(proposal.symbol).strip().upper(): proposal
        for proposal in proposal_batch.proposals
    }
    proposal_rank = {
        str(proposal.symbol).strip().upper(): rank
        for rank, proposal in enumerate(proposal_batch.proposals, start=1)
    }
    upstream_by_symbol = {
        str(item.symbol or "").strip().upper(): item
        for item in proposal_batch.diagnostics.symbol_diagnostics
    }
    if reasons:
        return _plan(
            input_valid=False,
            global_reasons=tuple(dict.fromkeys(reasons)),
            selected=(),
            proposal_by_symbol=proposal_by_symbol,
            proposal_rank=proposal_rank,
            proposal_refusals={},
            upstream_by_symbol=upstream_by_symbol,
            open_symbols=open_symbols,
            exit_symbols=exit_symbols,
            queued_symbols=queued_symbols,
            occupied_before=occupied_before,
            occupied_after=occupied,
            total_cap=total_cap,
            pair_cap=pair_cap,
            cycle_cap=cycle_cap,
        )

    assert total_cap is not None
    assert pair_cap == 1
    assert cycle_cap is not None
    selected: list[MTVCLCTradeCandidate] = []
    refusals: dict[str, tuple[str, ...]] = {}
    for proposal in proposal_batch.proposals:
        symbol = str(proposal.symbol).strip().upper()
        if symbol in open_after:
            refusals[symbol] = ("active_open_position",)
            continue
        if symbol in queued_symbols:
            refusals[symbol] = ("active_queued_entry",)
            continue
        if len(selected) >= cycle_cap:
            refusals[symbol] = ("cycle_entry_capacity_exhausted",)
            continue
        if len(occupied) >= total_cap:
            refusals[symbol] = ("total_position_capacity_exhausted",)
            continue
        selected.append(proposal)
        occupied.add(symbol)

    return _plan(
        input_valid=True,
        global_reasons=(),
        selected=tuple(selected),
        proposal_by_symbol=proposal_by_symbol,
        proposal_rank=proposal_rank,
        proposal_refusals=refusals,
        upstream_by_symbol=upstream_by_symbol,
        open_symbols=open_symbols,
        exit_symbols=exit_symbols,
        queued_symbols=queued_symbols,
        occupied_before=occupied_before,
        occupied_after=occupied,
        total_cap=total_cap,
        pair_cap=pair_cap,
        cycle_cap=cycle_cap,
    )


__all__ = [
    "MTVCLC_CYCLE_CAPACITY_SCHEMA_VERSION",
    "MTVCLCCycleCapacityDiagnostics",
    "MTVCLCCycleCapacityPlan",
    "MTVCLCCycleSymbolDiagnostic",
    "plan_mtvclc_cycle_capacity",
]
