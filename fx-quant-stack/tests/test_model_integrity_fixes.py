"""Regression tests for three defects found 2026-07-31, fixed before the 4-pair retrain.

C. ProbabilityCalibrator saturation: the shipped swing calibrator mapped 398
   distinct raw scores to 14 values, 97% of them EXACTLY 0.0 or 1.0. Downstream,
   a constant swing_prob=0.0 became side=short at confidence 1.0 (1,036 SELL /
   0 BUY in 24h) and fabricated a 0.46 model disagreement (real spread 0.063).

D. reversal labels: `opposite = -future` made the opportunity predicate
   ALGEBRAICALLY IDENTICAL to the failure predicate, so both heads trained on
   the same (X, y, w) and XGB wrote byte-identical model.json files in every
   bundle ever built.

E. evaluate_book_stress read `book.per_symbol_stop_risk` / `book.capital_at_risk`
   -- fields PortfolioBook never defined -- so worst-case loss was always 0.0
   and the capital tail-loss gate never bound.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from fxstack.labels.reversal_labels import ReversalLabelConfig, build_reversal_labels
from fxstack.live.policy import compute_model_disagreement_score, directional_swing_confidence
from fxstack.portfolio.book import build_portfolio_book
from fxstack.portfolio.stress import evaluate_book_stress
from fxstack.training.calibration import ProbabilityCalibrator


# --------------------------------------------------------------------------- #
# C. calibrator
# --------------------------------------------------------------------------- #
def _fit_isotonic_calibrator(n: int = 5000, seed: int = 7) -> tuple[ProbabilityCalibrator, np.ndarray]:
    rng = np.random.default_rng(seed)
    p_raw = rng.uniform(0.0, 1.0, size=n)
    # Degenerate tails on purpose: all-0 outcomes below 0.3, all-1 above 0.7 --
    # the shape that makes isotonic emit exact 0.0 / 1.0 plateaus.
    y = np.where(p_raw < 0.3, 0.0, np.where(p_raw > 0.7, 1.0, (rng.uniform(size=n) < p_raw)))
    cal = ProbabilityCalibrator()
    cal.fit(p_raw, y)
    assert cal.method == "isotonic", "test requires the isotonic path"
    return cal, p_raw


def test_calibrated_probability_is_never_exactly_zero_or_one() -> None:
    cal, _ = _fit_isotonic_calibrator()
    grid = np.linspace(0.0, 1.0, 2001)
    out = cal.transform(grid)
    assert float(out.min()) > 0.0, "exact 0.0 is a saturation artifact, not evidence"
    assert float(out.max()) < 1.0, "exact 1.0 is a saturation artifact, not evidence"
    # Laplace bound: with n fit rows the clamp is ~1/(n+2).
    eps = 1.0 / (5000 + 2.0)
    assert float(out.min()) >= eps * 0.99
    assert float(out.max()) <= 1.0 - (eps * 0.99)


def test_calibration_preserves_the_models_ranking() -> None:
    cal, _ = _fit_isotonic_calibrator()
    grid = np.linspace(0.001, 0.999, 999)
    out = cal.transform(grid)
    # Strictly increasing input -> strictly increasing output. The old behaviour
    # collapsed whole ranges to one constant (398 raw -> 14 calibrated live).
    assert np.all(np.diff(out) > 0.0), "isotonic plateaus must not erase the model's ordering"
    assert len(set(np.round(out, 9))) == len(grid)


def test_calibration_shift_from_the_tiebreak_is_negligible() -> None:
    cal, p_raw = _fit_isotonic_calibrator()
    inner = np.asarray(cal._iso.transform(np.clip(p_raw, 0.0, 1.0)), dtype=float)  # noqa: SLF001
    out = cal.transform(p_raw)
    # Away from the clamp region the blend may only move probabilities by ~1e-3.
    interior = (inner > 0.01) & (inner < 0.99)
    assert float(np.max(np.abs(out[interior] - inner[interior]))) <= 2e-3


def test_legacy_pickles_without_fit_rows_still_get_a_clamp() -> None:
    cal, _ = _fit_isotonic_calibrator()
    # Simulate an artifact serialized before _fit_rows existed.
    delattr(cal, "_fit_rows")
    out = cal.transform(np.linspace(0.0, 1.0, 501))
    assert float(out.min()) >= 1e-3 * 0.99
    assert float(out.max()) <= 1.0 - (1e-3 * 0.99)


def test_small_samples_use_sigmoid_not_isotonic() -> None:
    rng = np.random.default_rng(11)
    p_raw = rng.uniform(0.0, 1.0, size=715)  # the live swing model's train size
    y = (rng.uniform(size=715) < p_raw).astype(float)
    cal = ProbabilityCalibrator()
    cal.fit(p_raw, y)
    assert cal.method == "sigmoid", "715 rows must not fit an isotonic step function"
    out = cal.transform(np.linspace(0.0, 1.0, 501))
    assert float(out.min()) > 0.0 and float(out.max()) < 1.0


# --------------------------------------------------------------------------- #
# C. disagreement units
# --------------------------------------------------------------------------- #
def test_agreeing_short_stack_reports_low_disagreement() -> None:
    """Every model favours SHORT; the metric must not read that as conflict.

    Raw model values are swing P(up)=0.10 and intraday P(up)=0.15. At the
    scorer boundary those become short support 0.90 and 0.85; meta support is
    0.80. The disagreement metric receives those like-unit policy values.
    """
    swing_conf = directional_swing_confidence(swing_prob=0.10, side="short")
    score = compute_model_disagreement_score(
        directional_swing_confidence_value=swing_conf,
        entry_prob=0.85,
        trade_prob=0.80,
        side="short",
    )
    assert score == pytest.approx((0.05 + 0.10 + 0.05) / 3.0, abs=1e-9)
    assert score < 0.10


def test_genuinely_split_stack_reports_high_disagreement() -> None:
    """Swing says short hard, intraday says long hard -> real conflict."""
    swing_conf = directional_swing_confidence(swing_prob=0.05, side="short")  # 0.95
    score = compute_model_disagreement_score(
        directional_swing_confidence_value=swing_conf,
        entry_prob=0.10,
        trade_prob=0.50,
        side="short",
    )
    assert score > 0.5


def test_regime_probability_no_longer_manufactures_disagreement() -> None:
    """The live failure shape: regime pinned at 1.0 contributed two of four diff
    terms. The metric now has no regime input at all -- directional opinions only."""
    import inspect

    sig = inspect.signature(compute_model_disagreement_score)
    assert "regime_prob" not in sig.parameters


# --------------------------------------------------------------------------- #
# D. reversal labels
# --------------------------------------------------------------------------- #
def _frame_from_path(prices: list[float], *, side: str = "long") -> pd.DataFrame:
    return pd.DataFrame(
        {
            "mid_close": [100.0] + prices,
            "atr_14": [1.0] * (len(prices) + 1),
            "side": [side] * (len(prices) + 1),
            "spread_bps": [0.5] * (len(prices) + 1),
            "ts": pd.date_range("2026-01-01", periods=len(prices) + 1, freq="5min", tz="UTC"),
        }
    )


def test_failure_and_opportunity_are_distinct_predicates() -> None:
    """+1.2R spike, then -1.5R crash: the thesis fails (stop touched), but the
    FLIP's stop (entry +1R) was hit first, so the reversal never pays. The old
    excursion predicate called this an opportunity; algebraically it called
    EVERY failure an opportunity (opposite == -future)."""
    cfg = ReversalLabelConfig(horizon_bars=4, timing_window=6)
    frame = _frame_from_path([101.2, 98.5, 98.4, 98.4, 98.4], side="long")
    out = build_reversal_labels(frame, cfg)
    assert int(out["thesis_failure"].iloc[0]) == 1
    assert int(out["opposite_opportunity"].iloc[0]) == 0, (
        "the flip's stop was hit before its target -- flipping did NOT pay"
    )


def test_clean_reversal_labels_both_heads_positive() -> None:
    cfg = ReversalLabelConfig(horizon_bars=4, timing_window=6)
    frame = _frame_from_path([99.4, 98.5, 98.4, 98.4, 98.4], side="long")
    out = build_reversal_labels(frame, cfg)
    assert int(out["thesis_failure"].iloc[0]) == 1
    assert int(out["opposite_opportunity"].iloc[0]) == 1
    assert int(out["reversal_timing_quality"].iloc[0]) == 1


def test_reversal_label_columns_are_not_identical_on_noisy_paths() -> None:
    """The defect that produced byte-identical models: both label columns equal
    on EVERY row. On whipsaw paths the fixed semantics must disagree somewhere."""
    rng = np.random.default_rng(3)
    n = 400
    prices = 100.0 + np.cumsum(rng.normal(0.0, 0.8, size=n))
    frame = pd.DataFrame(
        {
            "mid_close": prices,
            "atr_14": np.full(n, 1.0),
            "side": ["long"] * n,
            "spread_bps": np.full(n, 0.5),
            "ts": pd.date_range("2026-01-01", periods=n, freq="5min", tz="UTC"),
        }
    )
    out = build_reversal_labels(frame, ReversalLabelConfig(horizon_bars=24))
    fail = out["thesis_failure"].to_numpy()
    opp = out["opposite_opportunity"].to_numpy()
    assert (fail != opp).any(), (
        "identical label columns -> identical training targets -> byte-identical models"
    )
    # And the direction of the difference is one-sided by construction:
    # opportunity (first-touch win) implies failure (excursion), never the reverse.
    assert not ((opp == 1) & (fail == 0)).any()


# --------------------------------------------------------------------------- #
# E. stress
# --------------------------------------------------------------------------- #
def _position_row(symbol: str, *, lots: float, open_price: float, sl: float, vpu: float | None) -> dict:
    row = {
        "symbol": symbol,
        "type": 1,
        "side": "SELL",
        "lots": lots,
        "open_price": open_price,
        "sl": sl,
    }
    if vpu is not None:
        row["value_per_price_unit"] = vpu
    return row


def test_book_prices_stop_risk_from_annotated_rows() -> None:
    book = build_portfolio_book(
        positions=[
            # 0.06 lots, 14.5 pips to the stop, EURUSD vpu 100k -> $87 at risk.
            _position_row("EURUSD", lots=0.06, open_price=1.1499, sl=1.15135, vpu=100_000.0),
            _position_row("USDCAD", lots=0.10, open_price=1.3800, sl=1.3850, vpu=72_463.77),
        ],
        pending_entries=[],
    )
    assert book.per_symbol_stop_risk["EURUSD"] == pytest.approx(0.00145 * 100_000.0 * 0.06, rel=1e-6)
    assert book.per_symbol_stop_risk["USDCAD"] == pytest.approx(0.005 * 72_463.77 * 0.10, rel=1e-6)
    assert book.capital_at_risk == pytest.approx(
        sum(book.per_symbol_stop_risk.values()), rel=1e-12
    )


def test_stress_reports_the_all_stops_hit_loss() -> None:
    book = build_portfolio_book(
        positions=[_position_row("EURUSD", lots=0.06, open_price=1.1499, sl=1.15135, vpu=100_000.0)],
        pending_entries=[],
    )
    stress = evaluate_book_stress(book)
    expected = 0.00145 * 100_000.0 * 0.06
    assert stress.worst_case_loss_proxy == pytest.approx(expected, rel=1e-6)
    assert stress.dominant_scenario == "all_stops_hit"
    assert stress.scenario_losses["all_stops_hit"] == pytest.approx(expected, rel=1e-6)


def test_unpriceable_rows_stay_honestly_unmeasured() -> None:
    """No vpu attached (rate unresolvable) -> no invented number, stress 0.0."""
    book = build_portfolio_book(
        positions=[_position_row("EURJPY", lots=0.06, open_price=171.20, sl=171.60, vpu=None)],
        pending_entries=[],
    )
    assert book.per_symbol_stop_risk == {}
    assert book.capital_at_risk == 0.0
    stress = evaluate_book_stress(book)
    assert stress.worst_case_loss_proxy == 0.0
    assert stress.dominant_scenario == ""
