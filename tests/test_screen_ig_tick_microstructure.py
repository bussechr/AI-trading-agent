from __future__ import annotations

import numpy as np
from pathlib import Path
import pytest

from tools import export_legacy_tick_training_snapshot as exporter
from tools import screen_ig_tick_microstructure as screen


def test_symbol_rows_use_delayed_executable_quotes_and_causal_features() -> None:
    n = 240
    epochs = np.arange(n, dtype=np.float64) * 0.5 + 1_700_000_000.0
    latent = np.sin(np.arange(n, dtype=np.float64) / 7.0) * 0.00002
    mid = 1.1 + np.cumsum(latent)
    half_spread = np.full(n, 0.00001)
    rows = screen._build_symbol_rows(
        symbol_index=0,
        symbol_count=22,
        epochs=epochs,
        broker_epochs=epochs - 0.05,
        bid=mid - half_spread,
        ask=mid + half_spread,
        horizon_secs=5.0,
        maximum_event_gap_secs=1.0,
    )

    assert rows["X"].shape[1] == len(screen.FEATURE_NAMES)
    assert rows["X"].shape[0] > 100
    assert np.all(rows["exit_epoch"] > rows["signal_epoch"])
    assert np.all(np.isfinite(rows["mid_bps"]))
    assert np.all(np.isfinite(rows["long_bps"]))
    assert np.all(np.isfinite(rows["short_bps"]))
    assert np.all(rows["long_bps"] + rows["short_bps"] < 0.0)


def test_cross_pair_features_are_backward_asof_and_exclude_target() -> None:
    signals = np.asarray([10.0, 11.0, 12.0])
    target_epochs = np.asarray([9.0, 10.0, 11.0, 12.0])
    target_mid = np.asarray([1.0, 1.1, 1.2, 1.3])
    peer_epochs = np.asarray([8.0, 10.0, 11.5, 13.0])
    peer_mid = np.asarray([2.0, 2.1, 2.2, 9.9])
    columns = screen._cross_pair_columns(
        signal_epochs=signals,
        target_symbol_index=0,
        raw_symbols=[(target_epochs, target_mid), (peer_epochs, peer_mid)],
        maximum_event_gap_secs=2.0,
    )

    assert columns.shape == (3, 6)
    assert np.all(columns[:, :3] == 0.0)
    assert np.all(columns[:, 5] == 1.0)
    # The peer quote at t=13 must not influence any signal at or before t=12.
    assert np.max(np.abs(columns[:, 3:5])) < 1_000.0


def test_triangular_graph_features_are_causal_centered_and_peer_only() -> None:
    epochs = np.arange(1.0, 32.0)
    eurusd_mid = np.full(epochs.shape, 1.1)
    usdjpy_mid = np.full(epochs.shape, 150.0)
    eurjpy_mid = np.full(epochs.shape, 165.0)
    eurjpy_mid[-1] = 164.9
    peer_epochs = np.append(epochs, 100.0)
    peer_mid = np.append(eurusd_mid, 999.0)

    columns = screen._graph_dislocation_columns(
        signal_epochs=epochs,
        target_symbol_index=2,
        symbols=["EURUSD", "USDJPY", "EURJPY"],
        raw_symbols=[
            (peer_epochs, peer_mid),
            (epochs, usdjpy_mid),
            (epochs, eurjpy_mid),
        ],
        maximum_event_gap_secs=2.0,
    )

    assert columns.shape == (31, len(screen.GRAPH_FEATURE_NAMES))
    assert np.all(columns[:20, 3] == 0.0)
    assert np.all(columns[20:, 3] == 1.0)
    assert columns[-1, 0] < 0.0
    assert columns[-1, 1] < 0.0
    assert columns[-1, 2] < 0.0
    # The future EURUSD quote at t=100 cannot influence signals through t=31.
    assert abs(float(columns[-1, 0])) < 100.0

    unavailable = screen._graph_dislocation_columns(
        signal_epochs=epochs,
        target_symbol_index=0,
        symbols=["BTCUSD"],
        raw_symbols=[(epochs, np.full(epochs.shape, 100_000.0))],
        maximum_event_gap_secs=2.0,
    )
    assert np.all(unavailable == 0.0)


def test_nonoverlapping_metrics_enforce_abstention_and_side_coverage() -> None:
    dataset = {
        "symbol_index": np.asarray([0, 0, 0, 1, 1], dtype=np.int16),
        "signal_epoch": np.asarray([0.0, 1.0, 3.0, 0.0, 3.0]),
        "exit_epoch": np.asarray([2.0, 3.0, 5.0, 2.0, 5.0]),
        "long_bps": np.asarray([1.0, 9.0, -1.0, 0.5, -0.5]),
        "short_bps": np.asarray([-1.0, -9.0, 1.0, -0.5, 0.5]),
    }
    pred_long = np.asarray([0.8, 8.0, 0.0, 0.7, 0.0])
    pred_short = np.asarray([0.0, 0.0, 0.8, 0.0, 0.7])
    metrics = screen._nonoverlapping_metrics(
        dataset,
        np.arange(5),
        pred_long,
        pred_short,
        threshold_bps=0.5,
        extra_round_trip_cost_bps=0.1,
    )

    # The overlapping high-looking second signal for symbol 0 is skipped.
    assert metrics["trades"] == 4
    assert metrics["buy_trades"] == 2
    assert metrics["sell_trades"] == 2
    assert metrics["mean_bps"] > 0.0


def test_graph_pair_panel_is_validation_only_and_symbol_partitioned() -> None:
    dataset = {
        "symbol_index": np.asarray([0, 0, 1, 1], dtype=np.int16),
        "signal_epoch": np.asarray([0.0, 3.0, 0.0, 3.0]),
        "exit_epoch": np.asarray([2.0, 5.0, 2.0, 5.0]),
        "long_bps": np.asarray([1.0, -1.0, 0.5, -0.5]),
        "short_bps": np.asarray([-1.0, 1.0, -0.5, 0.5]),
    }
    predictions = (
        np.asarray([0.8, 0.0, 0.7, 0.0]),
        np.asarray([0.0, 0.8, 0.0, 0.7]),
    )
    rows = screen._symbol_validation_trials(
        dataset=dataset,
        validation_indices=np.arange(4),
        prediction_families={
            "triangular_basis_reversion": predictions,
            "triangular_basis_reversion_confirmed": predictions,
            "triangular_basis_continuation": predictions,
        },
        threshold_grid_bps=(0.5,),
        symbols=["EURUSD", "USDJPY"],
        extra_round_trip_cost_bps=0.0,
    )

    assert len(rows) == 6
    assert {row["symbol"] for row in rows} == {"EURUSD", "USDJPY"}
    assert all(row["trades"] == 2 for row in rows)


def test_confirmed_graph_reversion_is_backward_asof_and_spread_net() -> None:
    epochs = np.asarray([0.0, 1.0, 2.0, 3.0, 4.0])
    dataset = {
        "symbol_index": np.zeros(5, dtype=np.int16),
        "signal_epoch": epochs,
        "exit_epoch": epochs + 2.0,
        "long_bps": np.ones(5),
        "short_bps": -np.ones(5),
        "graph_centered_residual_bps": np.asarray(
            [-2.0, -2.0, -1.5, -1.6, 9.0]
        ),
        "graph_fresh": np.ones(5),
        "X": np.zeros((5, len(screen.FEATURE_NAMES)), dtype=np.float32),
    }
    spread_index = screen.BASE_FEATURE_NAMES.index("spread_bps")
    dataset["X"][:, spread_index] = 0.25

    long_score, short_score = screen._confirmed_graph_reversion_predictions(
        dataset,
        np.asarray([2, 3], dtype=np.int64),
        confirmation_secs=1.0,
        maximum_event_gap_secs=0.1,
        extra_round_trip_cost_bps=0.1,
    )

    assert long_score[2] == pytest.approx(1.15)
    assert not np.isfinite(long_score[3])
    assert not np.any(np.isfinite(short_score))


def test_pair_panel_summary_never_opens_test_or_grants_selection_authority() -> None:
    results = [
        {
            "horizon_secs": 600.0,
            "symbol_validation_trials": [
                {
                    "model_family": "triangular_basis_reversion",
                    "symbol": "USDJPY",
                    "threshold_bps": 0.2,
                    "trades": 31,
                    "buy_trades": 22,
                    "sell_trades": 9,
                    "mean_bps": 0.13,
                    "total_bps": 4.02,
                    "profit_factor": 1.12,
                }
            ],
        }
    ]

    summary = screen._symbol_validation_summary(results)

    assert summary["trial_count"] == 1
    assert summary["raw_positive_trial_count"] == 1
    assert summary["coverage_sufficient_trial_count"] == 0
    assert summary["eligible_trial_count"] == 0
    assert summary["best_eligible_trial"] is None
    assert summary["held_out_test_opened"] is False
    assert summary["selection_authority"] is False


def test_metrics_return_explicit_empty_abstention() -> None:
    dataset = {
        "symbol_index": np.asarray([0, 0], dtype=np.int16),
        "signal_epoch": np.asarray([0.0, 3.0]),
        "exit_epoch": np.asarray([2.0, 5.0]),
        "long_bps": np.asarray([1.0, 1.0]),
        "short_bps": np.asarray([-1.0, -1.0]),
    }
    metrics = screen._nonoverlapping_metrics(
        dataset,
        np.arange(2),
        np.asarray([0.1, 0.1]),
        np.asarray([0.1, 0.1]),
        threshold_bps=0.5,
        extra_round_trip_cost_bps=0.0,
    )

    assert metrics == {
        "trades": 0,
        "buy_trades": 0,
        "sell_trades": 0,
        "mean_bps": None,
        "total_bps": None,
        "win_rate": None,
        "profit_factor": None,
    }


def test_legacy_training_bundle_must_end_before_authenticated_rows(
    tmp_path: Path,
) -> None:
    epochs = np.arange(120, dtype=np.float64) + 1000.0
    rows = {
        symbol: (epochs, np.full(120, 1.0), np.full(120, 1.00002))
        for symbol in ("EURUSD", "USDJPY")
    }
    bundle = exporter._emit(
        output_dir=tmp_path / "legacy",
        symbols=("EURUSD", "USDJPY"),
        rows=rows,
        requested_start_epoch=900.0,
        authenticated_boundary_epoch=1200.0,
        created_at_epoch=1300.0,
    )
    payload, arrays = screen._strict_legacy_training(
        bundle,
        authenticated_payload={"symbol_scope": ["EURUSD", "USDJPY"]},
        authenticated_arrays={"sample_epoch": np.asarray([1200.0, 1201.0])},
    )

    assert payload["legacy_untrusted"] is True
    assert arrays["sample_epoch"].shape == (240,)


def test_validation_candidate_must_be_balanced_and_economically_positive() -> None:
    passing = {
        "trades": 120,
        "buy_trades": 55,
        "sell_trades": 65,
        "mean_bps": 0.2,
        "total_bps": 24.0,
        "profit_factor": 1.2,
    }
    assert screen._validation_candidate_eligible(passing)
    for field, value in (
        ("sell_trades", 0),
        ("mean_bps", -0.1),
        ("total_bps", -1.0),
        ("profit_factor", 0.9),
    ):
        failing = dict(passing)
        failing[field] = value
        assert not screen._validation_candidate_eligible(failing)


def test_validation_coverage_is_distinct_from_economic_eligibility() -> None:
    losing_but_covered = {
        "trades": 150,
        "buy_trades": 52,
        "sell_trades": 98,
        "mean_bps": -1.7,
        "total_bps": -255.0,
        "profit_factor": 0.2,
    }

    assert screen._validation_coverage_sufficient(losing_but_covered)
    assert not screen._validation_candidate_eligible(losing_but_covered)
