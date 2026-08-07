"""Pins the complementarity gate: two sleeves that win together are one bet, twice.

The gate must demote a redundant sleeve, keep the stronger side of every pair,
treat negative correlation as the diversification it is, and refuse to make any
claim on thin evidence.
"""

from __future__ import annotations

from fxstack.strategy.allocator_types import SleeveHealthSnapshot
from fxstack.strategy.complementarity import (
    MIN_SHARED_BUCKETS,
    MIN_SLEEVE_TRADES,
    VERDICT_ADMITTED,
    VERDICT_DEMOTED,
    VERDICT_INSUFFICIENT,
    evaluate_sleeve_complementarity,
)
from fxstack.strategy.sleeve_governance import SleeveGovernanceTracker


# Eight distinct pairs so every recorded trade lands in its own
# (pair, session) bucket -- the bucket vector is then exactly the input
# series, which keeps the expected correlations obvious.
PAIRS = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "AUDUSD",
    "USDCAD",
    "USDCHF",
    "NZDUSD",
    "EURJPY",
]


def _health(sleeve: str, *, expectancy: float, profit_factor: float = 1.0) -> SleeveHealthSnapshot:
    return SleeveHealthSnapshot(
        sleeve=sleeve,
        score=0.6,
        state="healthy",
        trades=MIN_SLEEVE_TRADES,
        win_rate=0.5,
        expectancy_usd=float(expectancy),
        profit_factor=float(profit_factor),
        avg_holding_bars=10.0,
        partial_frequency=0.0,
        replacement_exit_share=0.0,
        drawdown_contribution_usd=0.0,
        session_pnl_mix={},
        pair_contribution={},
    )


def _tracker(series_by_sleeve: dict[str, list[float]]) -> SleeveGovernanceTracker:
    """Record one closed trade per pair per sleeve, in a fixed pair order."""
    tracker = SleeveGovernanceTracker(sleeves=sorted(series_by_sleeve), max_trades=64)
    for sleeve, values in series_by_sleeve.items():
        for index, pnl in enumerate(values):
            tracker.record_trade(
                sleeve=sleeve,
                realized_pnl_usd=float(pnl),
                holding_bars=10.0,
                partial_exit_events=0,
                close_reason="tp",
                session_bucket="london_open",
                pair=PAIRS[index % len(PAIRS)],
            )
    return tracker


def test_positively_correlated_sleeves_demote_the_weaker_one() -> None:
    # Identical condition-by-condition outcomes: the same bet under two names.
    shared = [120.0, -80.0, 60.0, -40.0, 95.0, -55.0, 70.0, -35.0]
    tracker = _tracker({"range_mean_reversion": shared, "failed_breakout_reversal": shared})
    health = {
        "range_mean_reversion": _health("range_mean_reversion", expectancy=25.0, profit_factor=1.4),
        "failed_breakout_reversal": _health("failed_breakout_reversal", expectancy=5.0, profit_factor=1.05),
    }

    snapshot = evaluate_sleeve_complementarity(
        governance_state=tracker.export_state(),
        health=health,
    )

    assert snapshot.verdicts["failed_breakout_reversal"].verdict == VERDICT_DEMOTED
    assert snapshot.verdicts["failed_breakout_reversal"].redundant_with == "range_mean_reversion"
    # The stronger sleeve keeps trading -- the gate never empties the book.
    assert snapshot.verdicts["range_mean_reversion"].verdict == VERDICT_ADMITTED
    assert snapshot.admits("range_mean_reversion") is True
    assert snapshot.admits("failed_breakout_reversal") is False
    assert snapshot.block_reason("failed_breakout_reversal") == "sleeve_redundant_with:range_mean_reversion"
    assert snapshot.block_reason("range_mean_reversion") == ""


def test_negatively_correlated_sleeves_are_both_admitted() -> None:
    """Anti-correlated sleeves are the diversification the gate exists to protect."""
    tracker = _tracker(
        {
            "trend_pullback": [120.0, -80.0, 60.0, -40.0, 95.0, -55.0, 70.0, -35.0],
            "range_mean_reversion": [-120.0, 80.0, -60.0, 40.0, -95.0, 55.0, -70.0, 35.0],
        }
    )

    snapshot = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    assert all(item.correlation < 0.0 for item in snapshot.correlations)
    assert all(item.redundant is False for item in snapshot.correlations)
    assert snapshot.verdicts["trend_pullback"].verdict == VERDICT_ADMITTED
    assert snapshot.verdicts["range_mean_reversion"].verdict == VERDICT_ADMITTED


def test_uncorrelated_sleeves_are_both_admitted() -> None:
    tracker = _tracker(
        {
            "trend_pullback": [100.0, -50.0, 20.0, -10.0, 75.0, -30.0, 60.0, -25.0],
            "breakout_expansion": [-20.0, -45.0, 130.0, 15.0, -90.0, 40.0, 10.0, 55.0],
        }
    )

    snapshot = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    assert snapshot.verdicts["trend_pullback"].verdict == VERDICT_ADMITTED
    assert snapshot.verdicts["breakout_expansion"].verdict == VERDICT_ADMITTED
    assert all(abs(item.correlation) < 0.70 for item in snapshot.correlations)


def test_thin_history_makes_no_claim_and_demotes_nothing() -> None:
    """Demoting on noise destroys more edge than the redundancy costs."""
    tracker = _tracker(
        {
            "trend_pullback": [50.0, -25.0, 30.0],
            "breakout_expansion": [50.0, -25.0, 30.0],
        }
    )

    snapshot = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    assert snapshot.verdicts["trend_pullback"].verdict == VERDICT_INSUFFICIENT
    assert snapshot.verdicts["breakout_expansion"].verdict == VERDICT_INSUFFICIENT
    assert snapshot.correlations == []
    # Insufficient evidence still admits -- this gate only ever withholds a
    # sleeve it has positively shown to be redundant.
    assert snapshot.admits("trend_pullback") is True
    assert snapshot.admits("breakout_expansion") is True


def test_too_few_shared_conditions_is_not_measured() -> None:
    """Sleeves that never traded the same conditions cannot be compared."""
    tracker = SleeveGovernanceTracker(sleeves=["a", "b"], max_trades=64)
    for index in range(MIN_SLEEVE_TRADES):
        tracker.record_trade(
            sleeve="a",
            realized_pnl_usd=float(10 * (index + 1)),
            holding_bars=5.0,
            partial_exit_events=0,
            close_reason="tp",
            session_bucket="asia",
            pair=PAIRS[index % 3],
        )
        tracker.record_trade(
            sleeve="b",
            realized_pnl_usd=float(10 * (index + 1)),
            holding_bars=5.0,
            partial_exit_events=0,
            close_reason="tp",
            session_bucket="new_york",
            pair=PAIRS[3 + (index % 3)],
        )

    snapshot = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    # Disjoint condition buckets -> zero overlap -> nothing measurable.
    assert snapshot.correlations == []
    assert snapshot.verdicts["a"].verdict == VERDICT_ADMITTED
    assert snapshot.verdicts["b"].verdict == VERDICT_ADMITTED


def test_flat_pnl_sleeve_yields_no_correlation() -> None:
    """A zero-variance series carries no information; numpy would give NaN."""
    tracker = _tracker(
        {
            "flat": [25.0] * 8,
            "varying": [120.0, -80.0, 60.0, -40.0, 95.0, -55.0, 70.0, -35.0],
        }
    )

    snapshot = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    assert snapshot.correlations == []
    assert snapshot.verdicts["flat"].verdict == VERDICT_ADMITTED
    assert snapshot.verdicts["varying"].verdict == VERDICT_ADMITTED


def test_survivor_tiebreak_is_deterministic_without_health() -> None:
    """With no health input both sides rank equal, so the name decides -- stably."""
    shared = [120.0, -80.0, 60.0, -40.0, 95.0, -55.0, 70.0, -35.0]
    tracker = _tracker({"aaa": shared, "zzz": shared})

    first = evaluate_sleeve_complementarity(governance_state=tracker.export_state())
    second = evaluate_sleeve_complementarity(governance_state=tracker.export_state())

    assert first.verdicts["aaa"].verdict == first.verdicts["aaa"].verdict
    assert first.to_dict() == second.to_dict()
    demoted = [name for name, verdict in first.verdicts.items() if verdict.verdict == VERDICT_DEMOTED]
    assert demoted == ["aaa"]


def test_snapshot_is_json_safe_telemetry() -> None:
    shared = [120.0, -80.0, 60.0, -40.0, 95.0, -55.0, 70.0, -35.0]
    tracker = _tracker({"range_mean_reversion": shared, "failed_breakout_reversal": shared})

    payload = evaluate_sleeve_complementarity(governance_state=tracker.export_state()).to_dict()

    import json

    assert json.loads(json.dumps(payload)) == payload
    assert payload["threshold"] == 0.70
    assert payload["evaluated_sleeves"] == ["failed_breakout_reversal", "range_mean_reversion"]
    assert payload["correlations"][0]["shared_buckets"] >= MIN_SHARED_BUCKETS


def test_missing_governance_state_is_inert() -> None:
    snapshot = evaluate_sleeve_complementarity(governance_state=None)
    assert snapshot.verdicts == {}
    assert snapshot.admits("anything") is True
    assert snapshot.block_reason("anything") == ""
