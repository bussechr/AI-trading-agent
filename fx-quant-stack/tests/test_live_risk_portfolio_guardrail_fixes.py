from __future__ import annotations

import pandas as pd
import pytest

from fxstack.live.policy import compute_live_uncertainty_score
from fxstack.live.scorer import LiveScorer
from fxstack.portfolio import build_portfolio_book, compute_concentration_snapshot, compute_correlation_snapshot
from fxstack.portfolio.budgeting import compute_allocator_budget
from fxstack.settings import get_settings


class _DummyModel:
    def __init__(self, *, name: str, feature_columns: list[str], out: dict[str, float]) -> None:
        self.name = name
        self.feature_columns = list(feature_columns)
        self._out = dict(out)

    def predict_proba(self, X: pd.DataFrame) -> pd.DataFrame:
        return pd.DataFrame([self._out])


def _build_scorer() -> LiveScorer:
    regime = _DummyModel(name="regime_hmm", feature_columns=["ret_1"], out={"p0": 0.2, "p1": 0.8})
    swing = _DummyModel(name="swing_xgb", feature_columns=["ret_1"], out={"p0": 0.28, "p1": 0.72})
    intraday = _DummyModel(name="intraday_xgb", feature_columns=["ret_1"], out={"p0": 0.36, "p1": 0.64})
    meta = _DummyModel(
        name="meta_filter_xgb",
        feature_columns=["regime_prob", "swing_prob", "entry_prob", "spread_bps"],
        out={"p0": 0.09, "p1": 0.91},
    )
    return LiveScorer(regime_model=regime, swing_model=swing, intraday_model=intraday, meta_model=meta)


def test_live_scorer_recomputes_uncertainty_from_live_probabilities(monkeypatch) -> None:
    monkeypatch.delenv("FXSTACK_BLOCKED_ENTRY_SESSIONS", raising=False)
    get_settings.cache_clear()
    try:
        scorer = _build_scorer()
        row = pd.DataFrame(
            [
                {
                    "pair": "EURUSD",
                    "ts": "2026-03-23T12:00:00Z",
                    "ret_1": 0.001,
                    "spread_bps": 0.8,
                    "uncertainty_score": 0.05,
                    "spread_z20": 1.8,
                    "normalized_spread": 0.7,
                    "vol_term_ratio": 1.4,
                    "bar_imbalance": 0.25,
                    "h1_available": 1.0,
                }
            ]
        )

        signal = scorer.score(
            regime_row=row,
            swing_row=row,
            intraday_row=row,
            meta_row=row,
            spread_bps=0.8,
            expected_edge_bps=4.0,
            spread_unit_source="provided",
        )
    finally:
        get_settings.cache_clear()

    expected = compute_live_uncertainty_score(
        row.iloc[0],
        regime_prob=0.8,
        swing_prob=0.72,
        entry_prob=0.64,
        trade_prob=0.91,
        side="long",
    )

    assert signal.uncertainty_score == pytest.approx(expected)
    assert signal.uncertainty_score != pytest.approx(0.05)


def test_live_scorer_does_not_infer_existing_position_from_generic_side(monkeypatch) -> None:
    monkeypatch.setenv("FXSTACK_STRATEGY_ENGINE_MODE", "rl_primary")
    get_settings.cache_clear()
    try:
        scorer = _build_scorer()
        row = pd.DataFrame(
            [
                {
                    "pair": "EURUSD",
                    "ts": "2026-03-23T12:00:00Z",
                    "ret_1": 0.001,
                    "spread_bps": 0.8,
                    "side": "long",
                    "rl_target_position": 0.5,
                }
            ]
        )

        signal = scorer.score(
            regime_row=row,
            swing_row=row,
            intraday_row=row,
            meta_row=row,
            spread_bps=0.8,
            expected_edge_bps=4.0,
            spread_unit_source="provided",
        )
    finally:
        get_settings.cache_clear()

    assert signal.rl_lifecycle_intent == "entry_intent"
    assert signal.rl_flip_intent is False
    assert signal.rl_rebalance_intent is False


def test_build_portfolio_book_normalizes_new_york_session_alias() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 62000.0,
                "contract_size": 1.0,
                "session_bucket": "newyork",
            }
        ]
    )

    assert book.positions[0].session_bucket == "new_york"
    assert book.session_counts == {"new_york": 1}


def test_allocator_budget_uses_session_family_match_before_busiest_fallback() -> None:
    book = build_portfolio_book(
        positions=[
            {
                "symbol": "EURUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 1.10,
                "contract_size": 100000,
                "session_bucket": "london",
            },
            {
                "symbol": "AUDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 0.70,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
            {
                "symbol": "NZDUSD",
                "side": "BUY",
                "lots": 1.0,
                "mark_price": 0.65,
                "contract_size": 100000,
                "session_bucket": "asia",
            },
        ]
    )
    concentration = compute_concentration_snapshot(book)
    correlation = compute_correlation_snapshot(symbol="USDJPY", active_symbols=["EURUSD", "AUDUSD", "NZDUSD"], mode="heuristic")

    budget = compute_allocator_budget(
        symbol="USDJPY",
        session_bucket="london_open",
        expected_edge_bps=12.0,
        uncertainty_score=0.1,
        book=book,
        concentration=concentration,
        correlation=correlation,
        max_total_positions=6,
        max_pair_positions=2,
    )

    assert book.session_counts == {"asia": 2, "london": 1}
    assert budget.session_penalty == pytest.approx(0.0)
    assert budget.session_stress == pytest.approx(1.0 / 3.0)
