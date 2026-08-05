from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
import fxstack.runtime.scalp_proposal_batch as batch_module
from fxstack.runtime.scalp_proposal_batch import (
    BRIDGE_M1_BAR_SOURCE_ID,
    BRIDGE_M1_BAR_SOURCE_VERSION,
    evaluate_dislocation_batch,
)
from fxstack.schemas.entry import EntryEvaluationRequest, EntryProposal
from fxstack.strategy.scalp_dislocation import (
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    DislocationPolicy,
)


CURRENT_MINUTE = 1_800_000_000
COMMON_CLOSED_MINUTE = CURRENT_MINUTE - 60
AS_OF_EPOCH = CURRENT_MINUTE + 30.0


def _row(
    *,
    symbol: str,
    minute_epoch: int,
    open_px: float,
    close_px: float,
    spread_bps: float = 0.6,
) -> dict[str, Any]:
    half_spread = close_px * spread_bps / 1e4 / 2.0
    return {
        "time": minute_epoch,
        "open": open_px,
        "high": max(open_px, close_px) + 0.00002,
        "low": min(open_px, close_px) - 0.00002,
        "close": close_px,
        "mid_open": open_px,
        "mid_high": max(open_px, close_px) + 0.00002,
        "mid_low": min(open_px, close_px) - 0.00002,
        "mid_close": close_px,
        "bid_close": close_px - half_spread,
        "ask_close": close_px + half_spread,
        "provider": "mt4_bridge",
        "provenance": "mt4_bridge",
        "canonical_symbol": symbol,
        "venue": IG_MT4_VENUE_ID,
        "timeframe": "M1",
        "quality_flags": [],
        # The adapter must derive finality from timestamp/as-of, not this field.
        "closed": False,
    }


def _symbol_rows(
    *,
    symbol: str,
    count: int,
    dislocated: bool = True,
    include_current: bool = True,
) -> list[dict[str, Any]]:
    start = COMMON_CLOSED_MINUTE - (count - 1) * 60
    rows: list[dict[str, Any]] = []
    for index in range(count):
        wiggle = 0.0001 if index % 2 else -0.0001
        open_px = 1.1 + wiggle
        close_px = open_px + (0.00001 if index % 2 else -0.00001)
        rows.append(
            _row(
                symbol=symbol,
                minute_epoch=start + index * 60,
                open_px=open_px,
                close_px=close_px,
            )
        )
    if dislocated:
        rows[-1] = _row(
            symbol=symbol,
            minute_epoch=COMMON_CLOSED_MINUTE,
            open_px=1.1044,
            close_px=1.10396,
        )
    if include_current:
        rows.append(
            _row(
                symbol=symbol,
                minute_epoch=CURRENT_MINUTE,
                open_px=1.1039,
                close_px=1.1038,
            )
        )
    return rows


def _universe(
    *,
    count: int,
    dislocated: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    return {
        symbol: _symbol_rows(
            symbol=symbol,
            count=count,
            dislocated=dislocated,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    }


def _controlled_proposal(
    *,
    symbol: str,
    allowed: bool,
    p_star: float = 0.5,
    disp_z: float = 2.0,
    spread_bps: float = 0.6,
    side: str = "SELL",
    reasons: tuple[str, ...] = (),
) -> EntryProposal:
    identity = get_ig_mt4_instrument(symbol)
    assert identity is not None
    return EntryProposal(
        strategy_id=SCALP_DISLOCATION_STRATEGY_ID,
        strategy_version=SCALP_DISLOCATION_STRATEGY_VERSION,
        config_sha256="a" * 64,
        symbol=symbol,
        instrument_id=identity.instrument_id,
        venue_id=identity.venue,
        source_id=BRIDGE_M1_BAR_SOURCE_ID,
        source_version=BRIDGE_M1_BAR_SOURCE_VERSION,
        allowed=allowed,
        reasons=reasons,
        side=side if allowed else None,
        minute_epoch=COMMON_CLOSED_MINUTE if allowed else None,
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
        entry_deadline_epoch=CURRENT_MINUTE + 5 if allowed else None,
    )


def _evaluate(
    universe: dict[str, list[dict[str, Any]]],
    policy: DislocationPolicy,
    *,
    source_id: str = BRIDGE_M1_BAR_SOURCE_ID,
    source_version: str = BRIDGE_M1_BAR_SOURCE_VERSION,
) -> batch_module.ScalpProposalBatchResult:
    return evaluate_dislocation_batch(
        raw_bars_by_symbol=universe,
        as_of_epoch=AS_OF_EPOCH,
        source_id=source_id,
        source_version=source_version,
        policy=policy,
    )


def test_batch_converts_exact_finalized_history_and_filters_current_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)
    captured: dict[str, EntryEvaluationRequest] = {}

    def fake_evaluate(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        captured[request.symbol] = request
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=False,
            reasons=("no_dislocation",),
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", fake_evaluate)

    result = _evaluate(_universe(count=20), policy)

    assert result.diagnostics.accepted is True
    assert result.diagnostics.reasons == ()
    assert result.diagnostics.common_closed_minute_epoch == COMMON_CLOSED_MINUTE
    assert result.diagnostics.filtered_current_bar_count == 22
    assert result.proposals == ()
    assert tuple(captured) == IG_MT4_SCALP_SYMBOLS
    for request in captured.values():
        assert len(request.bars) == 20
        assert request.bars[-1].minute_epoch == COMMON_CLOSED_MINUTE
        assert all(
            current.minute_epoch - previous.minute_epoch == 60
            for previous, current in zip(request.bars, request.bars[1:])
        )
        assert all(bar.closed is True for bar in request.bars)
        assert all(bar.source_id == BRIDGE_M1_BAR_SOURCE_ID for bar in request.bars)
        assert all(
            bar.source_version == BRIDGE_M1_BAR_SOURCE_VERSION
            for bar in request.bars
        )
        assert request.spread_bps == pytest.approx(0.6)


def test_real_evaluator_runs_for_all_22_and_returns_unqualified_proposals() -> None:
    policy = DislocationPolicy(min_history_bars=20, p_star_max=0.80)

    result = _evaluate(_universe(count=20), policy)

    assert result.diagnostics.accepted is True
    assert len(result.diagnostics.symbol_diagnostics) == 22
    assert len(result.proposals) == 22
    assert {proposal.symbol for proposal in result.proposals} == set(
        IG_MT4_SCALP_SYMBOLS
    )
    assert all(proposal.qualification == "candidate_unqualified" for proposal in result.proposals)
    assert all(proposal.execution_qualified is False for proposal in result.proposals)
    assert all(proposal.win_probability is None for proposal in result.proposals)


def test_allowed_proposals_use_the_exact_deterministic_rank_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = DislocationPolicy(min_history_bars=5)
    metrics: dict[str, tuple[float, float, float, str]] = {
        symbol: (0.80, 1.0, 2.0, "SELL") for symbol in IG_MT4_SCALP_SYMBOLS
    }
    metrics.update(
        {
            "BTCUSD": (0.20, 1.0, 2.0, "BUY"),
            "USDJPY": (0.30, 1.0, 1.0, "SELL"),
            "AUDUSD": (0.30, 3.0, 2.0, "SELL"),
            "GBPUSD": (0.30, -3.0, 1.5, "SELL"),
            "USDCAD": (0.30, 3.0, 1.5, "BUY"),
        }
    )

    def fake_evaluate(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        p_star, disp_z, spread_bps, side = metrics[request.symbol]
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=True,
            p_star=p_star,
            disp_z=disp_z,
            spread_bps=spread_bps,
            side=side,
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", fake_evaluate)

    result = _evaluate(_universe(count=5), policy)
    symbols = tuple(proposal.symbol for proposal in result.proposals)

    assert symbols[:5] == (
        "BTCUSD",  # lower p*
        "GBPUSD",  # equal |z|/spread, symbol before USDCAD
        "USDCAD",
        "AUDUSD",  # same p*/|z|, wider spread
        "USDJPY",  # same p*, weaker |z|
    )
    assert symbols[5:] == tuple(
        sorted(set(IG_MT4_SCALP_SYMBOLS) - set(symbols[:5]))
    )


def test_side_is_the_final_rank_tie_breaker() -> None:
    buy = _controlled_proposal(
        symbol="EURUSD",
        allowed=True,
        p_star=0.30,
        disp_z=2.0,
        spread_bps=0.6,
        side="BUY",
    )
    sell = _controlled_proposal(
        symbol="EURUSD",
        allowed=True,
        p_star=0.30,
        disp_z=2.0,
        spread_bps=0.6,
        side="SELL",
    )

    ranked = sorted((sell, buy), key=batch_module._proposal_rank_key)

    assert tuple(proposal.side for proposal in ranked) == ("BUY", "SELL")


Mutation = Callable[[dict[str, list[dict[str, Any]]]], None]


def _remove_symbol(universe: dict[str, list[dict[str, Any]]]) -> None:
    universe.pop("NZDJPY")


def _add_symbol(universe: dict[str, list[dict[str, Any]]]) -> None:
    universe["NOTIG"] = _symbol_rows(symbol="NOTIG", count=20)


def _gap_history(universe: dict[str, list[dict[str, Any]]]) -> None:
    rows = universe["EURUSD"]
    rows.pop(5)
    oldest = min(int(row["time"]) for row in rows)
    rows.append(
        _row(
            symbol="EURUSD",
            minute_epoch=oldest - 60,
            open_px=1.1,
            close_px=1.10001,
        )
    )


def _future_bar(universe: dict[str, list[dict[str, Any]]]) -> None:
    universe["EURUSD"].append(
        _row(
            symbol="EURUSD",
            minute_epoch=CURRENT_MINUTE + 60,
            open_px=1.1,
            close_px=1.10001,
        )
    )


def _missing_quote_close(universe: dict[str, list[dict[str, Any]]]) -> None:
    universe["EURUSD"][-2]["bid_close"] = None


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        (_remove_symbol, "missing_universe_symbol:NZDJPY"),
        (_add_symbol, "extra_universe_symbol:NOTIG"),
    ),
)
def test_exact_universe_fault_refuses_the_complete_batch_before_evaluation(
    monkeypatch: pytest.MonkeyPatch,
    mutation: Mutation,
    expected_reason: str,
) -> None:
    universe = _universe(count=20)
    mutation(universe)

    def must_not_evaluate(*_args: Any, **_kwargs: Any) -> EntryProposal:
        raise AssertionError("strategy evaluator must not run for an invalid batch")

    monkeypatch.setattr(batch_module, "evaluate_dislocation", must_not_evaluate)
    result = _evaluate(universe, DislocationPolicy(min_history_bars=20))

    assert result.diagnostics.accepted is False
    assert result.proposals == ()
    assert expected_reason in result.diagnostics.reasons


@pytest.mark.parametrize(
    ("mutation", "expected_reason"),
    (
        (_gap_history, "non_consecutive_finalized_history"),
        (_future_bar, "unfinalized_bar_present"),
        (_missing_quote_close, "bar_two_sided_close_missing"),
    ),
)
def test_one_structurally_unready_pair_abstains_without_blocking_the_other_21(
    monkeypatch: pytest.MonkeyPatch,
    mutation: Mutation,
    expected_reason: str,
) -> None:
    universe = _universe(count=20)
    mutation(universe)
    evaluated: list[str] = []

    def allow(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        evaluated.append(request.symbol)
        rank = IG_MT4_SCALP_SYMBOLS.index(request.symbol)
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=True,
            p_star=0.20 + rank / 1000.0,
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", allow)
    result = _evaluate(universe, DislocationPolicy(min_history_bars=20))

    expected_ready = tuple(
        symbol for symbol in IG_MT4_SCALP_SYMBOLS if symbol != "EURUSD"
    )
    assert result.diagnostics.accepted is True
    assert result.diagnostics.reasons == ()
    assert tuple(evaluated) == expected_ready
    assert tuple(proposal.symbol for proposal in result.proposals) == expected_ready
    eurusd = next(
        item
        for item in result.diagnostics.symbol_diagnostics
        if item.symbol == "EURUSD"
    )
    assert eurusd.structural_ready is False
    assert expected_reason in eurusd.structural_reasons
    assert eurusd.evaluation_allowed is None
    assert eurusd.evaluation_reasons == ()


def test_weekend_like_fx_absence_does_not_block_structurally_ready_crypto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    universe = _universe(count=20)
    crypto_symbols = IG_MT4_SCALP_SYMBOLS[-4:]
    for symbol in IG_MT4_SCALP_SYMBOLS[:-4]:
        universe[symbol] = []
    evaluated: list[str] = []

    def allow(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        evaluated.append(request.symbol)
        rank = crypto_symbols.index(request.symbol)
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=True,
            p_star=0.20 + rank / 1000.0,
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", allow)
    result = _evaluate(universe, DislocationPolicy(min_history_bars=20))

    assert result.diagnostics.accepted is True
    assert result.diagnostics.reasons == ()
    assert tuple(evaluated) == crypto_symbols
    assert tuple(proposal.symbol for proposal in result.proposals) == crypto_symbols
    by_symbol = {
        item.symbol: item for item in result.diagnostics.symbol_diagnostics
    }
    assert all(
        not by_symbol[symbol].structural_ready
        and "common_closed_minute_missing" in by_symbol[symbol].structural_reasons
        for symbol in IG_MT4_SCALP_SYMBOLS[:-4]
    )
    assert all(by_symbol[symbol].structural_ready for symbol in crypto_symbols)


@pytest.mark.parametrize(
    ("source_id", "source_version", "expected_reason"),
    (
        ("", BRIDGE_M1_BAR_SOURCE_VERSION, "batch_source_id_missing"),
        (BRIDGE_M1_BAR_SOURCE_ID, "", "batch_source_version_missing"),
        ("other", BRIDGE_M1_BAR_SOURCE_VERSION, "batch_source_id_unsupported"),
        (BRIDGE_M1_BAR_SOURCE_ID, "v2", "batch_source_version_unsupported"),
    ),
)
def test_batch_source_identity_must_be_explicit_and_exactly_versioned(
    source_id: str,
    source_version: str,
    expected_reason: str,
) -> None:
    result = _evaluate(
        _universe(count=5),
        DislocationPolicy(min_history_bars=5),
        source_id=source_id,
        source_version=source_version,
    )

    assert result.diagnostics.accepted is False
    assert result.proposals == ()
    assert expected_reason in result.diagnostics.reasons


def test_strategy_refusals_are_diagnostics_not_structural_batch_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=False,
            reasons=("no_dislocation",),
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", refuse)
    result = _evaluate(_universe(count=5), DislocationPolicy(min_history_bars=5))

    assert result.diagnostics.accepted is True
    assert result.proposals == ()
    assert all(
        diagnostic.evaluation_allowed is False
        and diagnostic.evaluation_reasons == ("no_dislocation",)
        for diagnostic in result.diagnostics.symbol_diagnostics
    )


def test_input_mapping_and_row_order_do_not_change_ranked_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def allow(
        request: EntryEvaluationRequest,
        _policy: DislocationPolicy,
    ) -> EntryProposal:
        rank = IG_MT4_SCALP_SYMBOLS.index(request.symbol)
        return _controlled_proposal(
            symbol=request.symbol,
            allowed=True,
            p_star=0.30 + rank / 1000.0,
            disp_z=2.0,
            spread_bps=0.6,
        )

    monkeypatch.setattr(batch_module, "evaluate_dislocation", allow)
    policy = DislocationPolicy(min_history_bars=5)
    forward = _universe(count=5)
    reversed_input = {
        symbol: list(reversed(rows))
        for symbol, rows in reversed(tuple(forward.items()))
    }

    first = _evaluate(forward, policy)
    second = _evaluate(reversed_input, policy)

    assert tuple(item.symbol for item in first.proposals) == tuple(
        item.symbol for item in second.proposals
    )
    assert first.diagnostics.reasons == second.diagnostics.reasons == ()
    assert first.diagnostics.filtered_current_bar_count == 22
    assert second.diagnostics.filtered_current_bar_count == 22


def test_batch_adapter_imports_no_execution_or_research_surface() -> None:
    path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "fxstack"
        / "runtime"
        / "scalp_proposal_batch.py"
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
        "fxstack.runtime.runner",
        "fxstack.runtime.service",
        "fxstack.runtime.postgres_store",
        "fxstack.settings",
    )
    assert all(
        not any(module == root or module.startswith(f"{root}.") for root in forbidden)
        for module in imported
    )
