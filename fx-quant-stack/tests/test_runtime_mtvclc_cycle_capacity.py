from __future__ import annotations

from dataclasses import replace

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime.mtvclc_cycle_capacity import plan_mtvclc_cycle_capacity
from fxstack.runtime.mtvclc_proposal_batch import (
    MTVCLC_RUNTIME_PROFILE_ID,
    MTVCLCProposalBatchDiagnostics,
    MTVCLCProposalBatchResult,
    MTVCLCSymbolProposalDiagnostic,
)
from fxstack.strategy.mtvclc import MTVCLCTradeCandidate


_ENTRY_EPOCH = 1_800_000_000


def _candidate(
    symbol: str,
    *,
    side: str = "BUY",
    p_star: float = 0.75,
    volume: int = 200,
    v90: float = 100.0,
    spread: float = 1.0,
) -> MTVCLCTradeCandidate:
    instrument = get_ig_mt4_instrument(symbol)
    assert instrument is not None
    return MTVCLCTradeCandidate(
        symbol=symbol,
        instrument_id=instrument.instrument_id,
        venue_id=IG_MT4_VENUE_ID,
        allowed=True,
        reasons=(),
        side=side,  # type: ignore[arg-type]
        expected_entry_epoch=_ENTRY_EPOCH,
        entry_deadline_epoch=_ENTRY_EPOCH + 5,
        p_star=p_star,
        signal_tick_volume=volume,
        volume_v90=v90,
        live_spread_bps=spread,
    )


def _batch(
    proposals: tuple[MTVCLCTradeCandidate, ...],
) -> MTVCLCProposalBatchResult:
    proposal_symbols = {proposal.symbol for proposal in proposals}
    diagnostics = tuple(
        MTVCLCSymbolProposalDiagnostic(
            symbol=symbol,
            structural_ready=True,
            structural_reasons=(),
            raw_bar_count=242,
            filtered_current_bar_count=1,
            finalized_bar_count=241,
            selected_history_count=241,
            latest_finalized_minute_epoch=_ENTRY_EPOCH - 60,
            raw_quote_count=1,
            selected_quote_count=1,
            quote_transport_received_at_epochs=(float(_ENTRY_EPOCH),),
            cost_calibration_id="cost-policy",
            cost_calibration_source_sha256="a" * 64,
            cost_calibration_row_sha256="b" * 64,
            evaluation_allowed=symbol in proposal_symbols,
            evaluation_reasons=(
                () if symbol in proposal_symbols else ("no_signal",)
            ),
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    return MTVCLCProposalBatchResult(
        proposals=proposals,
        diagnostics=MTVCLCProposalBatchDiagnostics(
            accepted=True,
            reasons=(),
            strategy_profile=MTVCLC_RUNTIME_PROFILE_ID,
            as_of_epoch=float(_ENTRY_EPOCH),
            current_minute_epoch=_ENTRY_EPOCH,
            common_closed_minute_epoch=_ENTRY_EPOCH - 60,
            expected_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_bar_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_quote_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_cost_symbols=IG_MT4_SCALP_SYMBOLS,
            market_source_id="bridge-source",
            producer_instance_id="terminal-instance",
            symbol_diagnostics=diagnostics,
        ),
    )


def _plan(
    batch: MTVCLCProposalBatchResult,
    *,
    open_symbols: tuple[str, ...] = (),
    queued_symbols: tuple[str, ...] = (),
    exits: tuple[str, ...] = (),
    total_cap: int = 3,
    cycle_cap: int = 2,
):
    return plan_mtvclc_cycle_capacity(
        proposal_batch=batch,
        authoritative_open_symbols=open_symbols,
        active_queued_entry_symbols=queued_symbols,
        projected_broker_confirmed_exit_symbols=exits,
        max_total_positions=total_cap,
        max_pair_positions=1,
        max_new_entries_per_cycle=cycle_cap,
    )


def test_capacity_preserves_existing_mtvclc_rank_order() -> None:
    first = _candidate("EURUSD", p_star=0.74, volume=210)
    second = _candidate("USDJPY", p_star=0.75, volume=220)
    plan = _plan(_batch((first, second)), cycle_cap=1)

    assert plan.diagnostics.input_valid is True
    assert plan.selected_proposals == (first,)
    by_symbol = {
        row.symbol: row for row in plan.diagnostics.symbol_diagnostics
    }
    assert by_symbol["EURUSD"].selected is True
    assert by_symbol["USDJPY"].refusal_reasons == (
        "cycle_entry_capacity_exhausted",
    )


def test_confirmed_exit_frees_capacity_but_unconfirmed_open_does_not() -> None:
    candidate = _candidate("USDJPY")
    blocked = _plan(
        _batch((candidate,)),
        open_symbols=("EURUSD",),
        total_cap=1,
        cycle_cap=1,
    )
    assert blocked.selected_proposals == ()

    released = _plan(
        _batch((candidate,)),
        open_symbols=("EURUSD",),
        exits=("EURUSD",),
        total_cap=1,
        cycle_cap=1,
    )
    assert released.selected_proposals == (candidate,)


def test_open_or_queued_symbol_cannot_be_selected_again() -> None:
    first = _candidate("EURUSD", p_star=0.74)
    second = _candidate("USDJPY", p_star=0.75)
    plan = _plan(
        _batch((first, second)),
        open_symbols=("EURUSD",),
        queued_symbols=("AUDUSD",),
        total_cap=4,
        cycle_cap=2,
    )
    assert plan.selected_proposals == (second,)


def test_scope_drift_fails_the_whole_capacity_plan_closed() -> None:
    batch = _batch((_candidate("EURUSD"),))
    drifted = replace(
        batch,
        diagnostics=replace(
            batch.diagnostics,
            observed_cost_symbols=IG_MT4_SCALP_SYMBOLS[:-1],
        ),
    )
    plan = _plan(drifted)
    assert plan.selected_proposals == ()
    assert plan.diagnostics.input_valid is False
    assert "proposal_batch_scope_invalid" in (
        plan.diagnostics.global_refusal_reasons
    )


def test_rank_reordering_and_executable_state_are_rejected() -> None:
    first = _candidate("EURUSD", p_star=0.74)
    second = _candidate("USDJPY", p_star=0.75)
    reordered = _plan(_batch((second, first)))
    assert reordered.selected_proposals == ()
    assert "proposal_batch_rank_order_invalid" in (
        reordered.diagnostics.global_refusal_reasons
    )

    object.__setattr__(first, "broker_trade_authorized", True)
    executable = _plan(_batch((first,)))
    assert executable.selected_proposals == ()
    assert "proposal_qualification_invalid:EURUSD" in (
        executable.diagnostics.global_refusal_reasons
    )
