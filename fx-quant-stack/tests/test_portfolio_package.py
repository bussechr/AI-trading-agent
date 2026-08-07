from __future__ import annotations

from dataclasses import asdict

import numpy as np
import pandas as pd
import pytest

import fxstack.portfolio.allocator as portfolio_allocator
import fxstack.portfolio.book as portfolio_book
import fxstack.portfolio.telemetry as portfolio_telemetry
from fxstack._serialization import copy_flat_mapping, json_safe
from fxstack.portfolio import (
    build_portfolio_book,
    build_portfolio_telemetry,
    compute_concentration_snapshot,
    compute_correlation_snapshot,
    evaluate_book_stress,
    evaluate_portfolio_allocation,
)
from fxstack.portfolio.book import compose_portfolio_book, prepare_portfolio_book
from fxstack.portfolio.budgeting import compute_allocator_budget
from fxstack.strategy.allocator import (
    allocate_candidates,
    build_allocator_candidate,
    playbook_to_sleeve,
)
from fxstack.strategy.allocator_types import (
    AllocatorConfig,
    AllocatorOpenPosition,
    SleeveHealthSnapshot,
)


def _positions() -> list[dict[str, object]]:
    return [
        {
            "symbol": "EURUSD",
            "side": "BUY",
            "lots": 1.0,
            "mark_price": 1.10,
            "contract_size": 100000,
            "session_bucket": "london",
            "sleeve": "trend",
        },
        {
            "symbol": "GBPUSD",
            "side": "SELL",
            "lots": 0.5,
            "mark_price": 1.25,
            "contract_size": 100000,
            "session_bucket": "london",
            "sleeve": "trend",
        },
        {
            "symbol": "BTCUSDT",
            "side": "BUY",
            "lots": 2.0,
            "mark_price": 62000.0,
            "contract_size": 1.0,
            "session_bucket": "newyork",
            "sleeve": "breakout",
        },
    ]


def test_build_portfolio_book_tracks_exposure_units_and_buckets() -> None:
    book = build_portfolio_book(
        positions=_positions(), pending_entries=[{"pair": "AUDUSD"}]
    )

    assert book.exposure_unit == "notional_units"
    assert book.gross_exposure == pytest.approx(296500.0)
    assert book.net_exposure == pytest.approx(171500.0)
    assert book.gross_lot_exposure == pytest.approx(3.5)
    assert book.net_lot_exposure == pytest.approx(2.5)
    assert book.open_position_count == 3
    assert book.pending_entry_count == 1
    assert book.per_symbol_exposure["BTCUSDT"] == pytest.approx(124000.0)
    assert book.per_symbol_exposure["EURUSD"] == pytest.approx(110000.0)
    assert book.per_symbol_exposure["GBPUSD"] == pytest.approx(62500.0)
    assert book.per_currency_exposure["EUR"] == pytest.approx(110000.0)
    assert book.per_currency_exposure["GBP"] == pytest.approx(62500.0)
    assert book.per_currency_exposure["USD"] == pytest.approx(172500.0)
    assert book.per_currency_exposure["USDT"] == pytest.approx(124000.0)
    assert book.per_asset_class_exposure == {"crypto": 124000.0, "fx": 172500.0}
    assert book.session_counts == {"london": 2, "new_york": 1}
    assert book.sleeve_counts == {"breakout": 1, "trend": 2}


def test_book_serialization_matches_dataclass_contract_and_is_copy_isolated() -> None:
    positions = _positions()
    positions[0]["nested"] = {"labels": ["source"]}
    book = build_portfolio_book(positions=positions)
    expected = portfolio_book._json_safe(asdict(book))

    payload = book.to_dict()

    assert payload == expected
    payload["positions"][0]["metadata"]["nested"]["labels"].append("changed")
    assert book.positions[0].metadata["nested"]["labels"] == ["source"]


def test_flat_snapshot_serialization_matches_dataclass_contract_and_copies_fields() -> (
    None
):
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    )
    snapshots = (
        decision.concentration,
        decision.correlation,
        decision.budget,
        decision.stress,
    )

    for snapshot in snapshots:
        assert snapshot.to_dict() == asdict(snapshot)

    correlation_payload = decision.correlation.to_dict()
    correlation_payload["correlated_symbols"]["EURUSD"] = 99.0
    assert decision.correlation.correlated_symbols.get("EURUSD") != 99.0
    budget_payload = decision.budget.to_dict()
    budget_payload["numeric_input_errors"].append("changed")
    assert "changed" not in decision.budget.numeric_input_errors


def test_allocation_serialization_reuses_safe_telemetry_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    )

    def _unexpected_reserialization(_self):
        raise AssertionError("allocation reserialized an existing snapshot")

    for snapshot in (
        decision.concentration,
        decision.correlation,
        decision.budget,
        decision.stress,
    ):
        monkeypatch.setattr(type(snapshot), "to_dict", _unexpected_reserialization)

    decision.telemetry["correlation"]["correlated_symbols"]["tampered"] = 42.0
    payload = decision.to_dict()

    assert payload["budget"] == decision.telemetry["budget"]
    assert "tampered" not in payload["correlation"]["correlated_symbols"]
    payload["correlation"]["correlated_symbols"]["changed"] = 99.0
    assert "changed" not in decision.correlation.correlated_symbols


def test_runtime_allocation_payload_uses_canonical_telemetry_without_duplicates() -> (
    None
):
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
        prepared_book=prepare_portfolio_book(_positions()),
    )

    full_payload = decision.to_dict()
    runtime_payload = decision.to_runtime_dict()

    assert full_payload["book"]["positions"]
    assert "book" not in runtime_payload
    assert not {"concentration", "correlation", "stress"} & runtime_payload.keys()
    assert runtime_payload["budget"] == full_payload["budget"]
    assert "budget" not in runtime_payload["telemetry"]
    assert {
        **runtime_payload["telemetry"],
        "budget": runtime_payload["budget"],
    } == full_payload["telemetry"]
    assert (
        runtime_payload["telemetry"]["gross_lot_exposure"]
        == full_payload["book"]["gross_lot_exposure"]
    )


def test_allocation_snapshot_views_are_materialized_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    serialized_types: list[str] = []
    original_serializer = portfolio_allocator.flat_dataclass_dict

    def _recording_serializer(value):
        serialized_types.append(type(value).__name__)
        return original_serializer(value)

    monkeypatch.setattr(
        portfolio_allocator,
        "flat_dataclass_dict",
        _recording_serializer,
    )
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
        prepared_book=prepare_portfolio_book(_positions()),
    )

    assert serialized_types == ["CorrelationSnapshot", "AllocatorBudget"]
    serialized_types.clear()
    decision.to_runtime_dict()
    assert serialized_types == ["AllocatorBudget"]

    serialized_types.clear()
    decision.to_dict()
    assert serialized_types == [
        "ConcentrationSnapshot",
        "CorrelationSnapshot",
        "AllocatorBudget",
        "StressResult",
    ]


def test_runtime_read_only_allocation_reuses_prepared_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_portfolio_book(_positions())
    common = {
        "symbol": "EURUSD",
        "session_bucket": "london",
        "expected_edge_bps": 12.0,
        "uncertainty_score": 0.1,
        "positions": _positions(),
        "pending_entries": [],
        "max_total_positions": 6,
        "max_pair_positions": 2,
        "governance": {"mode": "normal"},
        "prepared_book": prepared,
    }
    expected = evaluate_portfolio_allocation(**common).to_runtime_dict()
    evaluate_portfolio_allocation(**common, _runtime_read_only=True)

    def _unexpected_clone(_value):
        raise AssertionError("read-only runtime allocation cloned a cached snapshot")

    monkeypatch.setattr(
        portfolio_allocator,
        "clone_flat_dataclass",
        _unexpected_clone,
    )
    monkeypatch.setattr(
        portfolio_telemetry,
        "flat_dataclass_dict",
        _unexpected_clone,
    )
    actual = evaluate_portfolio_allocation(
        **common,
        _runtime_read_only=True,
    ).to_runtime_dict()

    assert actual == expected
    assert "budget" not in actual["telemetry"]
    actual["budget"]["budget_scale"] = 0.0
    actual["telemetry"]["concentration"]["numeric_input_errors"].append(
        "changed"
    )
    repeated = evaluate_portfolio_allocation(
        **common,
        _runtime_read_only=True,
    ).to_runtime_dict()
    assert repeated == expected


def test_flat_mapping_copy_fast_path_preserves_container_contract() -> None:
    class DictSubclass(dict[str, float]):
        pass

    class ListSubclass(list[str]):
        pass

    class TupleSubclass(tuple[str, ...]):
        pass

    numeric_scalar = np.float64(1.25)
    source = {
        "mapping": {"EURUSD": 0.8},
        "sequence": ["numeric_error"],
        "tuple": ("EURUSD", "GBPUSD"),
        "mapping_subclass": DictSubclass({"GBPUSD": 0.6}),
        "sequence_subclass": ListSubclass(["subclass_error"]),
        "tuple_subclass": TupleSubclass(("AUDUSD", "USDJPY")),
        "numeric_scalar": numeric_scalar,
    }

    copied = copy_flat_mapping(source)

    assert copied == source
    assert copied["mapping"] is not source["mapping"]
    assert copied["sequence"] is not source["sequence"]
    assert type(copied["mapping_subclass"]) is dict
    assert type(copied["sequence_subclass"]) is list
    assert type(copied["tuple_subclass"]) is tuple
    assert copied["numeric_scalar"] is numeric_scalar

    source["mapping"]["EURUSD"] = 0.1
    source["sequence"].append("later_error")
    assert copied["mapping"] == {"EURUSD": 0.8}
    assert copied["sequence"] == ["numeric_error"]


def test_json_safe_exact_dispatch_preserves_subclass_fallbacks() -> None:
    class DictSubclass(dict[str, object]):
        pass

    class ListSubclass(list[object]):
        pass

    class TupleSubclass(tuple[object, ...]):
        pass

    payload = json_safe(
        DictSubclass(
            {
                "mapping": DictSubclass({"invalid": float("nan")}),
                "list": ListSubclass([np.int64(2), np.float64("inf")]),
                "tuple": TupleSubclass((np.float64(1.5),)),
            }
        )
    )

    assert payload == {
        "mapping": {"invalid": None},
        "list": [2, None],
        "tuple": [1.5],
    }


def test_book_reuses_private_static_instrument_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = portfolio_book.infer_instrument_ref
    calls = 0

    def _counted(symbol: str):
        nonlocal calls
        calls += 1
        return original(symbol)

    portfolio_book._book_instrument_ref.cache_clear()
    monkeypatch.setattr(portfolio_book, "infer_instrument_ref", _counted)
    try:
        for _ in range(2):
            build_portfolio_book(
                positions=[
                    {
                        "symbol": "EURUSD",
                        "side": "BUY",
                        "lots": 0.1,
                        "mark_price": 1.1,
                        "contract_size": 100000,
                    }
                ]
            )
        assert calls == 1
    finally:
        portfolio_book._book_instrument_ref.cache_clear()


def test_prepared_book_composition_matches_full_rebuild_and_snapshots_input() -> None:
    positions = _positions()
    pending_entries = [
        {
            "pair": "AUDUSD",
            "payload": {
                "cmd": "BUY",
                "symbol": "AUDUSD",
                "lots": 0.25,
                "mark_price": 0.70,
                "contract_size": 100000,
                "session_bucket": "asia",
                "sleeve": "trend",
            },
        },
        {
            "pair": "EURJPY",
            "payload": {
                "cmd": "SELL",
                "symbol": "EURJPY",
                "lots": np.nan,
                "mark_price": 162.0,
                "contract_size": 100000,
            },
        },
    ]
    expected = build_portfolio_book(
        positions=_positions(),
        pending_entries=pending_entries,
    )
    prepared = prepare_portfolio_book(positions)
    positions[0]["lots"] = 999.0

    assert compose_portfolio_book(prepared) is compose_portfolio_book(prepared)
    actual = compose_portfolio_book(
        prepared,
        pending_entries=pending_entries,
    )

    assert actual.to_dict() == expected.to_dict()


def test_allocator_prepared_book_matches_full_rebuild() -> None:
    positions = _positions()
    pending_entries = [
        {
            "pair": "AUDUSD",
            "payload": {
                "cmd": "BUY",
                "symbol": "AUDUSD",
                "lots": 0.25,
                "mark_price": 0.70,
                "contract_size": 100000,
                "session_bucket": "asia",
                "sleeve": "trend",
            },
        }
    ]
    common = {
        "symbol": "EURUSD",
        "session_bucket": "london",
        "expected_edge_bps": 12.0,
        "uncertainty_score": 0.1,
        "positions": positions,
        "pending_entries": pending_entries,
        "max_total_positions": 6,
        "max_pair_positions": 2,
        "governance": {"mode": "normal"},
    }

    rebuilt = evaluate_portfolio_allocation(**common)
    composed = evaluate_portfolio_allocation(
        **common,
        prepared_book=prepare_portfolio_book(positions),
    )

    assert composed.to_dict() == rebuilt.to_dict()


def test_prepared_allocator_reuses_base_derivations_with_isolated_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    positions = _positions()
    common = {
        "symbol": "EURUSD",
        "session_bucket": "london",
        "expected_edge_bps": 12.0,
        "uncertainty_score": 0.1,
        "positions": positions,
        "pending_entries": [],
        "max_total_positions": 6,
        "max_pair_positions": 2,
        "governance": {"mode": "normal"},
    }
    expected = evaluate_portfolio_allocation(**common).to_dict()
    prepared = prepare_portfolio_book(positions)
    concentration_calls = 0
    stress_calls = 0
    correlation_calls = 0
    budget_calls = 0
    original_concentration = portfolio_allocator.compute_concentration_snapshot
    original_stress = portfolio_allocator.evaluate_book_stress
    original_correlation = portfolio_allocator.compute_correlation_snapshot
    original_budget = portfolio_allocator.compute_allocator_budget

    def _concentration(*args, **kwargs):
        nonlocal concentration_calls
        concentration_calls += 1
        return original_concentration(*args, **kwargs)

    def _stress(*args, **kwargs):
        nonlocal stress_calls
        stress_calls += 1
        return original_stress(*args, **kwargs)

    def _correlation(*args, **kwargs):
        nonlocal correlation_calls
        correlation_calls += 1
        return original_correlation(*args, **kwargs)

    def _budget(*args, **kwargs):
        nonlocal budget_calls
        budget_calls += 1
        return original_budget(*args, **kwargs)

    monkeypatch.setattr(
        portfolio_allocator,
        "compute_concentration_snapshot",
        _concentration,
    )
    monkeypatch.setattr(portfolio_allocator, "evaluate_book_stress", _stress)
    monkeypatch.setattr(
        portfolio_allocator,
        "compute_correlation_snapshot",
        _correlation,
    )
    monkeypatch.setattr(portfolio_allocator, "compute_allocator_budget", _budget)

    first = evaluate_portfolio_allocation(**common, prepared_book=prepared)
    first.concentration.numeric_input_errors.append("changed")
    first.stress.scenario_losses["changed"] = 99.0
    first.budget.budget_scale = 0.0
    first.budget.numeric_input_errors.append("changed")
    first.telemetry["per_symbol_exposure"]["changed"] = 99.0
    first.telemetry["concentration"]["numeric_input_errors"].append("changed")
    first.telemetry["stress"]["scenario_losses"]["changed"] = 99.0
    first_payload = first.to_dict()
    first_payload["book"]["positions"][0]["metadata"]["changed"] = True
    repeated = evaluate_portfolio_allocation(**common, prepared_book=prepared)

    assert concentration_calls == 1
    assert stress_calls == 1
    assert correlation_calls == 1
    assert budget_calls == 1
    assert repeated.to_dict() == expected
    assert "changed" not in repeated.concentration.numeric_input_errors
    assert "changed" not in repeated.stress.scenario_losses
    assert repeated.budget.budget_scale == expected["budget"]["budget_scale"]
    assert "changed" not in repeated.budget.numeric_input_errors
    assert "changed" not in repeated.telemetry["per_symbol_exposure"]
    assert "changed" not in repeated.telemetry["concentration"]["numeric_input_errors"]
    assert "changed" not in repeated.telemetry["stress"]["scenario_losses"]
    assert "changed" not in repeated.to_dict()["book"]["positions"][0]["metadata"]


def test_prepared_budget_cache_separates_inputs_and_bypasses_invalid_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_portfolio_book(_positions())
    common = {
        "symbol": "EURUSD",
        "session_bucket": "london",
        "expected_edge_bps": 12.0,
        "uncertainty_score": 0.1,
        "positions": _positions(),
        "pending_entries": [],
        "max_total_positions": 6,
        "max_pair_positions": 2,
        "governance": {"mode": "normal"},
        "prepared_book": prepared,
    }
    calls = 0
    original = portfolio_allocator.compute_allocator_budget

    def _counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(portfolio_allocator, "compute_allocator_budget", _counted)
    evaluate_portfolio_allocation(**common)
    evaluate_portfolio_allocation(**common)
    evaluate_portfolio_allocation(**{**common, "expected_edge_bps": 24.0})
    evaluate_portfolio_allocation(**{**common, "uncertainty_score": float("nan")})
    evaluate_portfolio_allocation(**{**common, "uncertainty_score": float("nan")})
    evaluate_portfolio_allocation(
        **{**common, "uncertainty_score": float("nan")},
        _runtime_read_only=True,
    )
    evaluate_portfolio_allocation(
        **{**common, "uncertainty_score": float("nan")},
        _runtime_read_only=True,
    )

    assert calls == 6


def test_build_portfolio_book_counts_pending_entry_exposure() -> None:
    pending_entries = [
        {
            "index": 0,
            "pair": "AUDUSD",
            "payload": {
                "cmd": "BUY",
                "symbol": "AUDUSD",
                "lots": 0.25,
                "mark_price": 0.70,
                "contract_size": 100000,
                "session_bucket": "asia",
                "sleeve": "trend",
            },
        }
    ]

    book = build_portfolio_book(positions=_positions(), pending_entries=pending_entries)

    assert book.pending_entry_count == 1
    assert len(book.pending_positions) == 1
    assert book.pending_positions[0].symbol == "AUDUSD"
    assert book.pending_positions[0].side == "BUY"
    assert book.gross_exposure == pytest.approx(314000.0)
    assert book.net_exposure == pytest.approx(189000.0)
    assert book.pending_gross_exposure == pytest.approx(17500.0)
    assert book.pending_net_exposure == pytest.approx(17500.0)
    assert book.gross_lot_exposure == pytest.approx(3.75)
    assert book.net_lot_exposure == pytest.approx(2.75)
    assert book.pending_gross_lot_exposure == pytest.approx(0.25)
    assert book.pending_net_lot_exposure == pytest.approx(0.25)
    assert book.per_symbol_exposure["AUDUSD"] == pytest.approx(17500.0)
    assert book.per_currency_exposure["AUD"] == pytest.approx(17500.0)
    assert book.per_currency_exposure["USD"] == pytest.approx(190000.0)
    assert book.per_currency_net_exposure["USD"] == pytest.approx(-65000.0)


def test_build_portfolio_book_tracks_signed_net_exposure_buckets() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "GBPUSD",
                "side": "SELL",
                "lots": 0.5,
                "mark_price": 2.0,
                "contract_size": 100000,
                "session_bucket": "newyork",
            },
        ]
    )

    assert book.gross_exposure == pytest.approx(200000.0)
    assert book.net_exposure == pytest.approx(0.0)
    assert book.per_symbol_net_exposure == {"EURUSD": 100000.0, "GBPUSD": -100000.0}
    assert book.per_currency_net_exposure == {
        "EUR": 100000.0,
        "GBP": -100000.0,
        "USD": 0.0,
    }
    assert book.per_asset_class_net_exposure == {"fx": 0.0}


def test_concentration_snapshot_is_deterministic() -> None:
    book = build_portfolio_book(positions=_positions())

    first = compute_concentration_snapshot(book)
    second = compute_concentration_snapshot(book)

    assert first.to_dict() == second.to_dict()
    assert first.top_symbol == "BTCUSDT"
    assert first.top_symbol_share == pytest.approx(124000.0 / 296500.0)
    assert first.top_currency == "USD"
    assert first.top_currency_share == pytest.approx(
        172500.0 / (110000.0 + 62500.0 + 172500.0 + 124000.0)
    )
    assert first.session_peak_share == pytest.approx(2 / 3)
    assert first.sleeve_peak_share == pytest.approx(2 / 3)


def test_pending_entries_contribute_to_session_concentration() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.10,
                "contract_size": 100000,
                "session_bucket": "london",
                "sleeve": "trend",
            },
            {
                "symbol": "GBPUSD",
                "side": "SELL",
                "lots": 0.5,
                "mark_price": 1.25,
                "contract_size": 100000,
                "session_bucket": "london",
                "sleeve": "trend",
            },
        ],
        pending_entries=[
            {
                "pair": "AUDUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "AUDUSD",
                    "lots": 0.25,
                    "mark_price": 0.70,
                    "contract_size": 100000,
                    "session_bucket": "london",
                    "sleeve": "trend",
                },
            }
        ],
    )

    concentration = compute_concentration_snapshot(book)

    assert book.session_counts == {"london": 3}
    assert book.sleeve_counts == {"trend": 3}
    assert concentration.session_peak_share == pytest.approx(1.0)
    assert concentration.sleeve_peak_share == pytest.approx(1.0)


def test_allocator_budget_does_not_double_count_pending_session_entries() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.10,
                "contract_size": 100000,
                "session_bucket": "london",
                "sleeve": "trend",
            }
        ],
        pending_entries=[
            {
                "pair": "AUDUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "AUDUSD",
                    "lots": 0.25,
                    "mark_price": 0.70,
                    "contract_size": 100000,
                    "session_bucket": "london",
                    "sleeve": "trend",
                },
            }
        ],
    )
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(
        symbol="EURUSD", active_symbols=["AUDUSD"], mode="heuristic"
    )

    budget = compute_allocator_budget(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        book=book,
        concentration=concentration,
        correlation=correlation,
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert book.session_counts == {"london": 2}
    assert concentration.session_peak_share == pytest.approx(1.0)
    assert budget.session_penalty == pytest.approx(0.1)
    assert budget.session_stress == pytest.approx(1.0)
    assert budget.budget_scale > 0.35


def test_correlation_snapshot_uses_deterministic_heuristics() -> None:
    snapshot = compute_correlation_snapshot(
        symbol="EURUSD", active_symbols=["GBPUSD", "AUDNZD", "BTCUSDT"]
    )

    assert snapshot.max_abs_corr == pytest.approx(0.6)
    assert snapshot.avg_abs_corr == pytest.approx((0.6 + 0.15 + 0.1) / 3.0)
    assert snapshot.correlated_symbols == {
        "AUDNZD": 0.15,
        "BTCUSDT": 0.1,
        "GBPUSD": 0.6,
    }


def test_correlation_snapshot_uses_realized_returns_and_metadata() -> None:
    realized = {
        "EURUSD": pd.Series(
            [1.0, 2.0, 3.0, 4.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
        "GBPUSD": pd.Series(
            [2.0, 4.0, 6.0, 8.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
        "AUDNZD": pd.Series(
            [-1.0, -2.0, -3.0, -4.0],
            index=pd.date_range("2026-04-08T00:00:00Z", periods=4, freq="min"),
        ),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD", "AUDNZD"],
        mode="realized",
        realized_returns_by_pair=realized,
        window_bars=4,
        min_obs=3,
    )

    assert snapshot.method == "realized"
    assert snapshot.window_bars == 4
    assert snapshot.min_obs == 3
    assert snapshot.sample_count == 4
    assert snapshot.freshness_secs is not None
    assert snapshot.max_abs_corr == pytest.approx(1.0)
    assert snapshot.avg_abs_corr == pytest.approx(1.0)
    assert snapshot.correlated_symbols == {"AUDNZD": -1.0, "GBPUSD": 1.0}


def test_correlation_snapshot_hybrid_blends_realized_and_heuristic_scores() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0]),
        "GBPUSD": pd.Series([2.0, 4.0]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        mode="hybrid",
        realized_returns_by_pair=realized,
        window_bars=8,
        min_obs=2,
    )

    assert snapshot.method == "hybrid"
    assert snapshot.sample_count == 2
    assert snapshot.max_abs_corr == pytest.approx(0.7)
    assert snapshot.avg_abs_corr == pytest.approx(0.7)
    assert snapshot.correlated_symbols["GBPUSD"] == pytest.approx(0.7)


def test_correlation_snapshot_hybrid_shrinks_magnitude_without_signed_cancellation() -> (
    None
):
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "USDJPY": pd.Series([-1.0, -2.0, -3.0, -4.0]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["USDJPY"],
        mode="hybrid",
        realized_returns_by_pair=realized,
        window_bars=8,
        min_obs=2,
    )

    assert snapshot.method == "hybrid"
    assert snapshot.sample_count == 4
    assert snapshot.correlated_symbols["USDJPY"] == pytest.approx(-0.8)
    assert snapshot.max_abs_corr == pytest.approx(0.8)


def test_correlation_snapshot_aligns_each_peer_without_sparse_universe_deletion() -> (
    None
):
    now = pd.Timestamp.now(tz="UTC").floor("s")
    index = pd.date_range(end=now, periods=6, freq="min")
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], index=index),
        "GBPUSD": pd.Series([2.0, 4.0, 6.0, 8.0], index=index[:4]),
        "AUDNZD": pd.Series([9.0, -9.0], index=index[-2:]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD", "AUDNZD"],
        mode="realized",
        realized_returns_by_pair=realized,
        window_bars=6,
        min_obs=3,
    )

    assert snapshot.method == "realized"
    assert snapshot.estimator == "winsorized_pearson_with_heuristic_fallback"
    assert snapshot.correlated_symbols == {
        "AUDNZD": pytest.approx(0.15),
        "GBPUSD": pytest.approx(1.0),
    }
    assert snapshot.sample_count == 4
    assert snapshot.pair_sample_counts == {"AUDNZD": 2, "GBPUSD": 4}
    assert snapshot.pair_observation_coverage == {
        "AUDNZD": pytest.approx(2 / 6),
        "GBPUSD": pytest.approx(4 / 6),
    }
    assert snapshot.active_pair_count == 2
    assert snapshot.realized_pair_count == 1
    assert snapshot.coverage_ratio == pytest.approx(0.5)
    assert snapshot.freshness_secs is not None
    assert snapshot.freshness_secs >= 110.0


def test_realized_correlation_retains_missing_peer_structural_risk_prior() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, -1.0, 1.0, -1.0]),
        "AUDNZD": pd.Series([1.0, 1.0, -1.0, -1.0]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["AUDNZD", "GBPUSD"],
        mode="realized",
        realized_returns_by_pair=realized,
        window_bars=4,
        min_obs=2,
    )

    assert snapshot.coverage_ratio == pytest.approx(0.5)
    assert snapshot.correlated_symbols["AUDNZD"] == pytest.approx(0.0)
    assert snapshot.correlated_symbols["GBPUSD"] == pytest.approx(0.6)
    assert snapshot.max_abs_corr == pytest.approx(0.6)


def test_correlation_snapshot_winsorization_resists_single_extreme_pair() -> None:
    values = np.linspace(-1.0, 1.0, 101)
    candidate = values.copy()
    peer = values.copy()
    candidate[-1] = 1_000_000.0
    peer[-1] = -1_000_000.0
    raw_corr = pd.Series(candidate).corr(pd.Series(peer))

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD"],
        mode="realized",
        realized_returns_by_pair={"EURUSD": candidate, "GBPUSD": peer},
        window_bars=101,
        min_obs=24,
    )

    assert raw_corr < -0.99
    assert snapshot.estimator == "winsorized_pearson"
    assert snapshot.correlated_symbols["GBPUSD"] > 0.90


def test_correlation_snapshot_is_invariant_to_peer_and_mapping_order() -> None:
    index = pd.date_range("2026-04-08T00:00:00Z", periods=8, freq="min")
    candidate = pd.Series([1.0, 2.0, 1.5, 3.0, 2.5, 4.0, 3.5, 5.0], index=index)
    gbp = candidate * 2.0
    aud = candidate * -3.0

    first = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD", "AUDNZD"],
        mode="realized",
        realized_returns_by_pair={"EURUSD": candidate, "GBPUSD": gbp, "AUDNZD": aud},
        window_bars=8,
        min_obs=4,
    )
    second = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["AUDNZD", "GBPUSD"],
        mode="realized",
        realized_returns_by_pair={"AUDNZD": aud, "GBPUSD": gbp, "EURUSD": candidate},
        window_bars=8,
        min_obs=4,
    )

    assert first.correlated_symbols == second.correlated_symbols
    assert first.pair_sample_counts == second.pair_sample_counts
    assert first.pair_observation_coverage == second.pair_observation_coverage
    assert first.sample_count == second.sample_count
    assert first.coverage_ratio == pytest.approx(second.coverage_ratio)
    assert first.max_abs_corr == pytest.approx(second.max_abs_corr)
    assert first.avg_abs_corr == pytest.approx(second.avg_abs_corr)


def test_correlation_snapshot_hybrid_retains_heuristic_only_peers() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([2.0, 4.0, 6.0, 8.0]),
    }

    snapshot = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=["GBPUSD", "AUDNZD"],
        mode="hybrid",
        realized_returns_by_pair=realized,
        window_bars=4,
        min_obs=2,
    )

    assert snapshot.method == "hybrid"
    assert snapshot.correlated_symbols["GBPUSD"] == pytest.approx(1.0)
    assert snapshot.correlated_symbols["AUDNZD"] == pytest.approx(0.15)
    assert snapshot.pair_sample_counts == {"AUDNZD": 0, "GBPUSD": 4}
    assert snapshot.active_pair_count == 2
    assert snapshot.realized_pair_count == 1
    assert snapshot.coverage_ratio == pytest.approx(0.5)


def test_stress_result_reports_only_computable_tail_loss() -> None:
    """The five invented scenario percentages were removed.

    They were hardcoded fractions of gross exposure (5%, 8%, 4%, ...) derived
    from nothing -- not the stops the orders carry, not realized gaps. A
    fabricated risk number is worse than none, because it gets budgeted against.
    What remains is the one tail that is exactly knowable: every open position
    hits its own stop.
    """

    book = build_portfolio_book(positions=_positions())
    concentration = compute_concentration_snapshot(book)
    stress = evaluate_book_stress(book, concentration=concentration)

    assert set(stress.scenario_losses) == {"all_stops_hit"}
    for invented in (
        "gap_open",
        "spread_widening",
        "session_liquidity_shock",
        "correlation_break",
        "stagnation_no_edge",
    ):
        assert invented not in stress.scenario_losses, f"{invented} was re-invented"
    assert stress.worst_case_loss_proxy == pytest.approx(
        stress.scenario_losses["all_stops_hit"]
    )
    assert stress.worst_case_loss_proxy >= 0.0
    # Not measurable from this book -> reports zero, never a guessed percentage.
    assert stress.worst_case_loss_proxy == pytest.approx(0.0)
    assert stress.dominant_scenario in {"", "all_stops_hit"}


def test_stress_result_is_scenario_stable_across_books() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "GBPUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "AUDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
            {
                "symbol": "NZDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.0,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
        ]
    )
    concentration = compute_concentration_snapshot(book)
    stress = evaluate_book_stress(book, concentration=concentration)

    assert concentration.top_symbol_share == pytest.approx(0.25)
    # Invented per-scenario ranking removed: every book reports the same single
    # computable tail (all stops hit), never a promoted guess.
    assert set(stress.scenario_losses) == {"all_stops_hit"}
    assert stress.dominant_scenario in {"", "all_stops_hit"}


def test_build_portfolio_telemetry_flattens_reporting_aliases() -> None:
    book = build_portfolio_book(positions=_positions())
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(
        symbol="EURUSD", active_symbols=["GBPUSD"], mode="heuristic"
    )
    budget = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    ).budget
    telemetry = build_portfolio_telemetry(
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=budget,
        stress=evaluate_book_stress(book, concentration=concentration),
        governance={"mode": "normal", "budget_scale": 0.9},
    )

    assert telemetry["top_symbol"] == concentration.top_symbol
    assert telemetry["top_symbol_share"] == pytest.approx(
        concentration.top_symbol_share
    )
    assert telemetry["session_peak_share"] == pytest.approx(
        concentration.session_peak_share
    )
    assert telemetry["correlation_method"] == "heuristic"
    assert telemetry["correlation_sample_count"] == 0
    assert telemetry["resize_pressure"] >= 0.0
    assert telemetry["flip_pressure"] >= 0.0
    assert telemetry["rebalance_pressure"] >= telemetry["resize_pressure"]
    assert telemetry["currency_stress"] >= telemetry["top_currency_share"]
    assert telemetry["session_stress"] >= telemetry["session_penalty"]
    assert telemetry["budget_scale"] == pytest.approx(budget.budget_scale)
    assert telemetry["governance_mode"] == "normal"
    assert telemetry["governance_budget_scale"] == pytest.approx(0.9)
    assert telemetry["session_penalty"] > 0.0


def test_public_telemetry_builder_still_normalizes_external_snapshots() -> None:
    book = build_portfolio_book(positions=[])
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(
        symbol="EURUSD",
        active_symbols=[],
        mode="heuristic",
    )
    correlation.max_abs_corr = float("nan")
    allocation = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="",
        expected_edge_bps=0.0,
        uncertainty_score=0.0,
        positions=[],
        pending_entries=[],
        max_total_positions=1,
        max_pair_positions=1,
    )

    telemetry = build_portfolio_telemetry(
        book=book,
        concentration=concentration,
        correlation=correlation,
        budget=allocation.budget,
        stress=evaluate_book_stress(book, concentration=concentration),
    )

    assert telemetry["correlation"]["max_abs_corr"] is None
    assert telemetry["correlation_max_abs"] == 0.0


def test_trusted_telemetry_reads_normalized_snapshot_scalars_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepared = prepare_portfolio_book(_positions())
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal", "budget_scale": 0.9},
        prepared_book=prepared,
    )
    expected = build_portfolio_telemetry(
        book=decision.book,
        concentration=decision.concentration,
        correlation=decision.correlation,
        budget=decision.budget,
        stress=decision.stress,
        governance={"mode": "normal", "budget_scale": 0.9},
    )
    finite_float_calls = 0
    original_finite_float = portfolio_telemetry._finite_float

    def _recording_finite_float(value, default=0.0):
        nonlocal finite_float_calls
        finite_float_calls += 1
        return original_finite_float(value, default)

    monkeypatch.setattr(
        portfolio_telemetry,
        "_finite_float",
        _recording_finite_float,
    )
    actual = build_portfolio_telemetry(
        book=decision.book,
        concentration=decision.concentration,
        correlation=decision.correlation,
        budget=decision.budget,
        stress=decision.stress,
        governance={"mode": "normal", "budget_scale": 0.9},
        _prepared_payloads=prepared._derived["allocation_telemetry_payloads"],
        _trusted_snapshots=True,
    )

    assert actual == expected
    assert finite_float_calls == 1


def test_portfolio_allocator_returns_budget_and_telemetry() -> None:
    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[{"pair": "AUDUSD"}],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
    )

    assert decision.allowed is True
    assert decision.budget.allowed is True
    assert decision.book.exposure_unit == "notional_units"
    assert decision.book.gross_exposure == pytest.approx(296500.0)
    assert decision.budget.budget_scale > 0.35
    assert decision.telemetry["concentration"]["top_symbol"] == "BTCUSDT"
    assert decision.telemetry["budget"]["target_cap"] == 2
    assert decision.telemetry["budget"]["exposure_unit"] == "notional_units"
    assert decision.telemetry["stress"]["dominant_scenario"] in {"", "all_stops_hit"}


def test_portfolio_allocator_consumes_realized_correlation_mode() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }

    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=_positions(),
        pending_entries=[{"pair": "AUDUSD"}],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
        corr_mode="realized",
        realized_returns_by_pair=realized,
        corr_window_bars=4,
        corr_min_obs=2,
    )

    assert decision.correlation.method == "realized"
    assert decision.correlation.sample_count == 4
    assert decision.budget.correlation_method == "realized"
    assert decision.budget.resize_pressure >= 0.0
    assert decision.budget.flip_pressure >= decision.budget.correlation_penalty
    assert decision.budget.rebalance_pressure >= decision.budget.resize_pressure
    assert decision.telemetry["correlation"]["method"] == "realized"
    assert decision.telemetry["correlation"]["sample_count"] == 4


def test_portfolio_allocator_includes_pending_symbols_in_realized_correlation() -> None:
    realized = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }

    decision = evaluate_portfolio_allocation(
        symbol="EURUSD",
        session_bucket="london",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.1,
                "contract_size": 100000,
                "session_bucket": "london",
            }
        ],
        pending_entries=[
            {
                "pair": "GBPUSD",
                "payload": {
                    "cmd": "BUY",
                    "symbol": "GBPUSD",
                    "lots": 0.25,
                    "mark_price": 1.25,
                    "contract_size": 100000,
                    "session_bucket": "london",
                },
            }
        ],
        max_total_positions=6,
        max_pair_positions=2,
        governance={"mode": "normal"},
        corr_mode="realized",
        realized_returns_by_pair=realized,
        corr_window_bars=4,
        corr_min_obs=2,
    )

    assert decision.correlation.method == "realized"
    assert "GBPUSD" in decision.correlation.correlated_symbols
    assert decision.correlation.correlated_symbols["GBPUSD"] == pytest.approx(1.0)
    assert decision.telemetry["pending_gross_exposure"] > 0.0
    assert decision.telemetry["pending_net_exposure"] > 0.0


def test_portfolio_allocator_ranks_against_crowded_session_and_correlation_pressure() -> (
    None
):
    config = AllocatorConfig(
        max_total_positions=6,
        max_pair_positions=2,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve = playbook_to_sleeve("trend_pullback")
    sleeve_health = SleeveHealthSnapshot(sleeve=sleeve, score=0.56, state="healthy")
    open_positions = [
        AllocatorOpenPosition(
            position_id="open-1",
            pair="EURUSD",
            side="BUY",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.54,
            age_bars=4.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
        AllocatorOpenPosition(
            position_id="open-2",
            pair="GBPUSD",
            side="SELL",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.57,
            age_bars=5.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
    ]
    crowded = build_allocator_candidate(
        candidate_id="crowded",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=sleeve,
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )
    cleaner = build_allocator_candidate(
        candidate_id="cleaner",
        index=1,
        pair="USDJPY",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=playbook_to_sleeve("breakout_expansion"),
        environment_state="PersistentTrend",
        session_bucket="asia",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )

    ranked, summary = allocate_candidates(
        candidates=[crowded, cleaner],
        open_positions=open_positions,
        remaining_slots=1,
        config=config,
    )

    assert summary.selected_count == 1
    assert ranked[0].pair == "USDJPY"
    assert ranked[0].allocator_selected is True
    assert ranked[1].allocator_selected is False
    assert ranked[0].allocator_score > ranked[1].allocator_score
    assert ranked[0].portfolio_risk_pressure < ranked[1].portfolio_risk_pressure
    assert ranked[1].portfolio_session_pressure > ranked[0].portfolio_session_pressure
    assert (
        ranked[1].portfolio_correlation_pressure
        > ranked[0].portfolio_correlation_pressure
    )


def test_portfolio_allocator_blocks_cross_pair_hard_gate() -> None:
    config = AllocatorConfig(
        max_total_positions=4,
        max_pair_positions=2,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve = playbook_to_sleeve("trend_pullback")
    sleeve_health = SleeveHealthSnapshot(sleeve=sleeve, score=0.56, state="healthy")
    strong = build_allocator_candidate(
        candidate_id="strong",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=sleeve,
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        cross_pair_rank_position=1,
        cross_pair_influence_score=0.92,
        cross_pair_recommendation_strength=0.95,
        config=config,
        open_positions=[],
        sleeve_health=sleeve_health,
    )
    blocked = build_allocator_candidate(
        candidate_id="blocked",
        index=1,
        pair="USDJPY",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=playbook_to_sleeve("breakout_expansion"),
        environment_state="PersistentTrend",
        session_bucket="asia",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        cross_pair_rank_position=2,
        cross_pair_influence_score=0.18,
        cross_pair_recommendation_strength=0.22,
        cross_pair_hard_block=True,
        config=config,
        open_positions=[],
        sleeve_health=sleeve_health,
    )

    ranked, summary = allocate_candidates(
        candidates=[strong, blocked],
        open_positions=[],
        remaining_slots=1,
        config=config,
    )

    assert summary.selected_count == 1
    assert ranked[0].candidate_id == "strong"
    assert ranked[0].allocator_selected is True
    assert ranked[1].allocator_selected is False
    assert ranked[1].allocator_rejection_reason == "cross_pair_hard_gate"


def test_build_allocator_candidate_uses_realized_correlation_pressure_when_available() -> (
    None
):
    config = AllocatorConfig(
        max_total_positions=6,
        max_pair_positions=2,
        max_new_entries=1,
        max_spread_bps=2.5,
        min_expected_edge_bps=3.0,
    )
    sleeve = playbook_to_sleeve("trend_pullback")
    sleeve_health = SleeveHealthSnapshot(sleeve=sleeve, score=0.56, state="healthy")
    open_positions = [
        AllocatorOpenPosition(
            position_id="open-1",
            pair="EURUSD",
            side="BUY",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.54,
            age_bars=4.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
        AllocatorOpenPosition(
            position_id="open-2",
            pair="GBPUSD",
            side="SELL",
            sleeve=sleeve,
            session_bucket="london",
            keep_score=0.57,
            age_bars=5.0,
            protected_hold=False,
            replaceable_hold=True,
        ),
    ]
    realized_returns = {
        "EURUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
        "GBPUSD": pd.Series([1.0, 2.0, 3.0, 4.0]),
    }
    heuristic = build_allocator_candidate(
        candidate_id="heuristic",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=sleeve,
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )
    realized = build_allocator_candidate(
        candidate_id="realized",
        index=0,
        pair="EURUSD",
        ts="2026-03-20T10:00:00Z",
        side="BUY",
        sleeve=sleeve,
        environment_state="PersistentTrend",
        session_bucket="london",
        baseline_allowed=True,
        adaptive_allowed=True,
        playbook_score=0.71,
        location_score=0.66,
        trigger_score=0.61,
        adaptive_entry_quality=0.72,
        expected_edge_bps=8.0,
        uncertainty_score=0.10,
        spread_bps=1.0,
        max_spread_bps=2.5,
        macro_coherence_score=0.64,
        currency_crowding_penalty=0.10,
        playbook_diversification_penalty=0.0,
        corr_mode="realized",
        realized_returns_by_pair=realized_returns,
        corr_window_bars=4,
        corr_min_obs=2,
        config=config,
        open_positions=open_positions,
        sleeve_health=sleeve_health,
    )

    assert heuristic.portfolio_correlation_pressure == pytest.approx(0.75)
    assert realized.portfolio_correlation_pressure == pytest.approx(1.0)
    assert (
        realized.portfolio_correlation_pressure
        > heuristic.portfolio_correlation_pressure
    )
