"""Tests for the statistical validation layer.

The important tests here are not the arithmetic ones -- they are the CALIBRATION
tests. A permutation test that reports significance for random signals is worse
than no test at all, because it launders noise into confidence. So the null
behaviour is asserted directly: p-values must be roughly uniform when the signal
carries no information, and small when it does.
"""

from __future__ import annotations

import numpy as np
import pytest

from fxstack.validation.certificate import (
    AcceptanceThresholds,
    build_certificate,
    evaluate_acceptance,
    load_certificate,
)
from fxstack.validation.mcpt import cost_stress_curve, rotation_permutation_test, strategy_returns
from fxstack.validation.metrics import max_drawdown, sharpe_ratio, summarize
from fxstack.validation.overfitting import (
    deannualize_sharpe,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probability_of_backtest_overfitting,
)
from fxstack.validation.report import validate_strategy
from fxstack.validation.resampling import (
    bootstrap_statistic,
    lag1_autocorrelation,
    optimal_block_length,
    trade_bootstrap,
)


def _ar1(n: int, rho: float, seed: int, scale: float = 0.001) -> np.ndarray:
    rng = np.random.default_rng(seed)
    out = np.zeros(n, dtype=float)
    for i in range(1, n):
        out[i] = rho * out[i - 1] + rng.normal(0.0, scale)
    return out


# --------------------------------------------------------------------------- metrics


def test_sharpe_zero_variance_is_zero_not_infinite():
    assert sharpe_ratio([0.01] * 50) == 0.0


def test_sharpe_scales_with_annualization():
    rng = np.random.default_rng(7)
    # Mean must be several standard errors above zero or the realized sample
    # Sharpe can land negative and the ordering assertion becomes meaningless.
    r = rng.normal(0.002, 0.01, 500)
    daily = sharpe_ratio(r, periods_per_year=252.0)
    hourly = sharpe_ratio(r, periods_per_year=252.0 * 24.0)
    assert hourly > daily > 0.0
    assert hourly == pytest.approx(daily * np.sqrt(24.0), rel=1e-9)


def test_max_drawdown_is_positive_fraction():
    # +10% then -20% of peak.
    returns = [0.10, -0.22]
    assert max_drawdown(returns) == pytest.approx(0.2, abs=0.01)


def test_summarize_reports_all_keys():
    keys = set(summarize(_ar1(200, 0.1, 3)).keys())
    assert {"sharpe", "max_drawdown", "cagr", "profit_factor", "win_rate", "skew", "kurtosis"} <= keys


# ----------------------------------------------------------------------- resampling


def test_optimal_block_length_grows_with_persistence():
    weak = optimal_block_length(_ar1(4000, 0.02, 11))
    strong = optimal_block_length(_ar1(4000, 0.85, 11))
    assert strong > weak
    assert weak >= 1


def test_lag1_autocorrelation_recovers_ar1_sign():
    assert lag1_autocorrelation(_ar1(5000, 0.7, 5)) > 0.4
    assert abs(lag1_autocorrelation(_ar1(5000, 0.0, 5))) < 0.15


def test_bootstrap_ci_brackets_observed_and_is_ordered():
    r = _ar1(1500, 0.2, 13)
    out = bootstrap_statistic(r, sharpe_ratio, n_resamples=300, seed=1)
    assert out["insufficient_data"] == 0.0
    assert out["ci_lower_025"] <= out["ci_lower_05"] <= out["median"] <= out["ci_upper_975"]
    assert out["n_resamples"] == 300.0


def test_bootstrap_refuses_tiny_samples():
    assert bootstrap_statistic([0.1, 0.2, 0.3], sharpe_ratio)["insufficient_data"] == 1.0


def test_trade_bootstrap_drawdown_ordering():
    rng = np.random.default_rng(9)
    pnl = rng.normal(1.0, 10.0, 400)
    out = trade_bootstrap(pnl, n_resamples=400, seed=2)
    assert out["median_max_drawdown"] <= out["p95_max_drawdown"] <= out["p99_max_drawdown"]
    assert 0.0 <= out["prob_final_negative"] <= 1.0


# ----------------------------------------------------------------- permutation tests


def test_permutation_pvalues_are_uniform_under_the_null():
    """No-information signals must NOT look significant.

    This is the test that catches a mis-specified null. With random signals the
    p-value distribution should be roughly uniform, so the mean sits near 0.5 and
    only a small minority fall below 5%.
    """

    p_values = []
    for trial in range(24):
        rng = np.random.default_rng(1000 + trial)
        returns = _ar1(600, 0.05, 2000 + trial)
        # Random but structured positions: persistent runs, like a real signal.
        raw = rng.normal(size=600)
        positions = np.sign(np.convolve(raw, np.ones(10) / 10.0, mode="same"))
        out = rotation_permutation_test(positions, returns, n_permutations=199, seed=trial)
        p_values.append(out["p_value"])

    arr = np.asarray(p_values)
    assert 0.2 < float(arr.mean()) < 0.8, f"null p-values not centred: mean={arr.mean():.3f}"
    assert float(np.mean(arr < 0.05)) <= 0.25, f"too many false positives: {np.mean(arr < 0.05):.2f}"


def test_permutation_detects_a_genuinely_predictive_signal():
    """A signal with real (noisy) foresight must be flagged as significant."""

    returns = _ar1(800, 0.05, 77)
    rng = np.random.default_rng(77)
    # Per the alignment contract, positions[i] earns returns[i]; so a signal with
    # real (noisy) knowledge of the bar it trades is NOT rolled. Rolling here
    # would align the signal to the wrong bar and correctly measure no edge.
    noisy_view = returns + rng.normal(0.0, 0.0008, returns.size)
    positions = np.sign(noisy_view)
    out = rotation_permutation_test(positions, returns, n_permutations=499, seed=3)
    assert out["p_value"] < 0.01, f"failed to detect real edge, p={out['p_value']}"
    assert out["observed"] > out["null_p95"]


def test_permutation_is_exact_when_permutations_exceed_universe():
    returns = _ar1(60, 0.0, 4)
    positions = np.sign(np.sin(np.arange(60) / 3.0))
    out = rotation_permutation_test(positions, returns, n_permutations=10_000, seed=4)
    assert out["exact"] == 1.0
    assert out["n_permutations"] == 59.0  # n-1 non-trivial rotations


def test_pvalue_never_zero():
    returns = _ar1(300, 0.0, 6)
    positions = np.sign(np.roll(returns, -1))  # perfect foresight
    out = rotation_permutation_test(positions, returns, n_permutations=99, seed=5)
    assert out["p_value"] > 0.0


def test_costs_reduce_strategy_returns_via_turnover():
    positions = np.asarray([0.0, 1.0, 1.0, 0.0, -1.0, 0.0])
    returns = np.asarray([0.0, 0.01, 0.01, 0.01, -0.01, 0.0])
    gross = strategy_returns(positions, returns, cost_per_turn=0.0)
    net = strategy_returns(positions, returns, cost_per_turn=0.001)
    assert net.sum() < gross.sum()
    # 4 unit position changes: 0->1, 1->0, 0->-1, -1->0
    assert (gross.sum() - net.sum()) == pytest.approx(4 * 0.001, rel=1e-9)


def test_cost_stress_flags_edge_that_dies_at_2x():
    returns = _ar1(600, 0.05, 21)
    rng = np.random.default_rng(21)
    positions = np.sign(np.roll(returns + rng.normal(0, 0.002, 600), -1))
    out = cost_stress_curve(positions, returns, base_cost_per_turn=0.005)
    assert out["sharpe_x1"] >= out["sharpe_x3"]
    assert "survives_2x_costs" in out


# ------------------------------------------------------------------ selection bias


def test_expected_max_sharpe_grows_with_trials():
    v = 0.01
    assert expected_max_sharpe(n_trials=2, sharpe_variance=v) < expected_max_sharpe(
        n_trials=100, sharpe_variance=v
    ) < expected_max_sharpe(n_trials=10_000, sharpe_variance=v)


def test_expected_max_sharpe_zero_without_variance_or_trials():
    assert expected_max_sharpe(n_trials=1, sharpe_variance=0.5) == 0.0
    assert expected_max_sharpe(n_trials=500, sharpe_variance=0.0) == 0.0


def test_deflated_sharpe_falls_as_search_widens():
    common = dict(sharpe_per_period=0.06, n_obs=2000, sharpe_variance_across_trials=0.0016)
    few = deflated_sharpe_ratio(n_trials=3, **common)["dsr"]
    many = deflated_sharpe_ratio(n_trials=5000, **common)["dsr"]
    assert few > many
    assert 0.0 <= many <= 1.0


def test_deannualize_roundtrip():
    assert deannualize_sharpe(sharpe_ratio([0.01, -0.005, 0.02, 0.0] * 30, periods_per_year=252.0),
                              periods_per_year=252.0) == pytest.approx(
        sharpe_ratio([0.01, -0.005, 0.02, 0.0] * 30, periods_per_year=1.0), rel=1e-9)


def test_pbo_is_high_for_pure_noise_configs():
    """Selecting among worthless configs should look like a coin flip."""

    rng = np.random.default_rng(31)
    mat = rng.normal(0.0, 0.01, size=(1200, 20))
    out = probability_of_backtest_overfitting(mat, n_splits=8)
    assert out["insufficient_data"] == 0.0
    assert out["pbo"] > 0.3, f"noise should not generalize, pbo={out['pbo']}"


def test_pbo_is_low_when_one_config_is_genuinely_better():
    rng = np.random.default_rng(32)
    mat = rng.normal(0.0, 0.01, size=(1200, 20))
    mat[:, 7] += 0.004  # a persistently superior config
    out = probability_of_backtest_overfitting(mat, n_splits=8)
    assert out["pbo"] < 0.2, f"real signal should generalize, pbo={out['pbo']}"


def test_pbo_refuses_insufficient_data():
    assert probability_of_backtest_overfitting(np.zeros((5, 3)), n_splits=10)["insufficient_data"] == 1.0


# -------------------------------------------------------------------- certificates


def _good_stats() -> dict[str, float]:
    return {
        "mcpt_p_value": 0.01,
        "bootstrap_sharpe_ci_lower": 0.4,
        "pbo": 0.15,
        "dsr": 0.99,
        "n_trades": 250.0,
        "max_drawdown": 0.08,
        "survives_2x_costs": 1.0,
    }


def test_acceptance_passes_on_strong_evidence():
    passed, reasons = evaluate_acceptance(_good_stats())
    assert passed and reasons == []


@pytest.mark.parametrize("missing", sorted(_good_stats().keys()))
def test_acceptance_fails_closed_on_any_missing_statistic(missing):
    stats = _good_stats()
    stats.pop(missing)
    passed, reasons = evaluate_acceptance(stats)
    assert not passed
    assert any(reason.startswith("missing_") for reason in reasons)


def test_acceptance_rejects_each_threshold_breach():
    for key, bad in (
        ("mcpt_p_value", 0.4),
        ("bootstrap_sharpe_ci_lower", -0.1),
        ("pbo", 0.8),
        ("dsr", 0.2),
        ("n_trades", 10.0),
        ("max_drawdown", 0.9),
        ("survives_2x_costs", 0.0),
    ):
        stats = _good_stats()
        stats[key] = bad
        passed, reasons = evaluate_acceptance(stats)
        assert not passed, f"{key}={bad} should fail"
        assert reasons


def test_acceptance_treats_nan_as_missing():
    stats = _good_stats()
    stats["dsr"] = float("nan")
    passed, reasons = evaluate_acceptance(stats)
    assert not passed
    assert "missing_dsr" in reasons


def test_certificate_seals_and_verifies():
    cert = build_certificate(
        strategy_id="eurusd_swing_xgb", pair="eurusd", model_payload_sha256="a" * 64,
        dataset_fingerprint="feed:2026-01-01..2026-06-01", created_at="2026-07-30T00:00:00Z",
        n_trials=120, statistics=_good_stats(),
    )
    assert cert.passed
    assert cert.certificate_sha256
    ok, problems = cert.verify(
        expected_model_payload_sha256="a" * 64,
        expected_dataset_fingerprint="feed:2026-01-01..2026-06-01",
    )
    assert ok and problems == []


def test_certificate_detects_tampering():
    cert = build_certificate(
        strategy_id="s", pair="eurusd", model_payload_sha256="b" * 64,
        dataset_fingerprint="fp", created_at="t", n_trials=10, statistics=_good_stats(),
    )
    forged = load_certificate({**cert.to_dict(), "statistics": {**_good_stats(), "pbo": 0.01}})
    ok, problems = forged.verify()
    assert not ok
    assert "certificate_hash_mismatch" in problems


def test_certificate_rejects_wrong_model_or_dataset():
    cert = build_certificate(
        strategy_id="s", pair="eurusd", model_payload_sha256="c" * 64,
        dataset_fingerprint="fp-1", created_at="t", n_trials=10, statistics=_good_stats(),
    )
    ok, problems = cert.verify(expected_model_payload_sha256="d" * 64, expected_dataset_fingerprint="fp-2")
    assert not ok
    assert "model_payload_mismatch" in problems
    assert "dataset_fingerprint_mismatch" in problems


def test_failing_certificate_never_verifies():
    cert = build_certificate(
        strategy_id="s", pair="eurusd", model_payload_sha256="e" * 64,
        dataset_fingerprint="fp", created_at="t", n_trials=10,
        statistics={**_good_stats(), "pbo": 0.95},
    )
    assert not cert.passed
    ok, problems = cert.verify()
    assert not ok
    assert "certificate_not_passing" in problems


def test_stricter_thresholds_are_honoured():
    stats = {**_good_stats(), "dsr": 0.96}
    assert evaluate_acceptance(stats)[0]
    assert not evaluate_acceptance(stats, thresholds=AcceptanceThresholds(dsr_min=0.99))[0]


# ------------------------------------------------------------------- full battery


def test_validate_strategy_end_to_end_requires_trials_for_dsr():
    returns = _ar1(700, 0.05, 41)
    rng = np.random.default_rng(41)
    positions = np.sign(np.roll(returns + rng.normal(0, 0.001, 700), -1))
    report = validate_strategy(
        positions=positions, bar_returns=returns, trade_pnl=rng.normal(1.0, 8.0, 150),
        cost_per_turn=0.0002, n_permutations=199, n_bootstrap=200,
    )
    stats = report["statistics"]
    assert stats["mcpt_p_value"] is not None
    assert stats["bootstrap_sharpe_ci_lower"] is not None
    assert stats["dsr"] is None  # no trial_sharpes supplied
    # Fail-closed: the gate must refuse a report that never measured DSR/PBO.
    passed, reasons = evaluate_acceptance(stats)
    assert not passed
    assert "missing_dsr" in reasons and "missing_pbo" in reasons


def test_validate_strategy_populates_dsr_and_pbo_when_given_search_context():
    rng = np.random.default_rng(52)
    returns = _ar1(900, 0.05, 52)
    positions = np.sign(np.roll(returns + rng.normal(0, 0.001, 900), -1))
    report = validate_strategy(
        positions=positions, bar_returns=returns, trade_pnl=rng.normal(1.0, 5.0, 200),
        cost_per_turn=0.0002, n_permutations=199, n_bootstrap=200,
        trial_sharpes=rng.normal(0.5, 0.8, 60),
        per_config_returns=rng.normal(0.0, 0.01, size=(900, 12)),
    )
    assert report["statistics"]["dsr"] is not None
    assert report["statistics"]["pbo"] is not None
    assert report["deflated_sharpe"]["n_trials"] == 60.0
