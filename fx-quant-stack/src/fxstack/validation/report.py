"""One call that produces a decision-grade validation report.

Callers should not have to remember which six tests constitute honest evidence,
nor which statistic key the acceptance gate expects. ``validate_strategy`` runs
the full battery and emits exactly the keys ``evaluate_acceptance`` requires, so
a missing test surfaces as an explicit ``missing_*`` failure rather than as a
silently absent check.
"""

from __future__ import annotations

from typing import Any

import numpy as np

from fxstack.validation.certificate import AcceptanceThresholds, build_certificate
from fxstack.validation.mcpt import (
    block_permutation_test,
    cost_stress_curve,
    rotation_permutation_test,
    strategy_returns,
)
from fxstack.validation.metrics import (
    DEFAULT_PERIODS_PER_YEAR,
    kurtosis,
    sharpe_ratio,
    skewness,
    summarize,
)
from fxstack.validation.overfitting import (
    deannualize_sharpe,
    deflated_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_variance_across_trials,
)
from fxstack.validation.resampling import bootstrap_statistic, trade_bootstrap


def validate_strategy(
    *,
    positions: np.ndarray | list[float],
    bar_returns: np.ndarray | list[float],
    trade_pnl: np.ndarray | list[float] | None = None,
    cost_per_turn: float = 0.0,
    periods_per_year: float = DEFAULT_PERIODS_PER_YEAR,
    n_permutations: int = 999,
    n_bootstrap: int = 1000,
    trial_sharpes: np.ndarray | list[float] | None = None,
    per_config_returns: np.ndarray | None = None,
    seed: int = 12345,
) -> dict[str, Any]:
    """Run the full battery and return tests plus a flat acceptance statistics dict.

    ``trial_sharpes`` must cover EVERY configuration the search evaluated, not
    just the survivors -- it sets the deflated Sharpe's null. Omitting it means
    the DSR cannot be computed, and the acceptance gate will refuse for
    ``missing_dsr`` rather than quietly passing.
    """

    net = strategy_returns(positions, bar_returns, cost_per_turn=cost_per_turn)
    perf = summarize(net, periods_per_year=periods_per_year)

    rotation = rotation_permutation_test(
        positions, bar_returns, statistic="sharpe", n_permutations=n_permutations,
        cost_per_turn=cost_per_turn, seed=seed,
    )
    blocks = block_permutation_test(
        positions, bar_returns, statistic="sharpe", n_permutations=min(n_permutations, 499),
        cost_per_turn=cost_per_turn, seed=seed + 1,
    )
    boot = bootstrap_statistic(
        net, lambda x: sharpe_ratio(x, periods_per_year=periods_per_year),
        n_resamples=n_bootstrap, method="stationary", seed=seed + 2,
    )
    costs = cost_stress_curve(
        positions, bar_returns, base_cost_per_turn=cost_per_turn, periods_per_year=periods_per_year,
    ) if cost_per_turn > 0.0 else {"survives_2x_costs": 0.0, "cost_model_absent": 1.0}

    trades = np.asarray(trade_pnl, dtype=float).ravel() if trade_pnl is not None else np.zeros(0)
    trade_stats = trade_bootstrap(trades, seed=seed + 3) if trades.size else {"n_trades": 0.0, "insufficient_data": 1.0}

    n_obs = int(np.asarray(net).size)
    sr_annual = float(perf.get("sharpe", 0.0))
    sr_period = deannualize_sharpe(sr_annual, periods_per_year=periods_per_year)
    trials = np.asarray(trial_sharpes, dtype=float).ravel() if trial_sharpes is not None else None
    dsr: dict[str, float] = {}
    if trials is not None and trials.size >= 2:
        dsr = deflated_sharpe_ratio(
            sharpe_per_period=sr_period,
            n_obs=n_obs,
            n_trials=int(trials.size),
            sharpe_variance_across_trials=sharpe_variance_across_trials(
                [deannualize_sharpe(float(s), periods_per_year=periods_per_year) for s in trials]
            ),
            skew=skewness(net),
            kurtosis=kurtosis(net),
        )

    pbo: dict[str, float] = {}
    if per_config_returns is not None:
        pbo = probability_of_backtest_overfitting(per_config_returns)

    n_trades = float(trade_stats.get("n_trades", 0.0))
    statistics: dict[str, Any] = {
        "sharpe_annualized": sr_annual,
        "max_drawdown": float(perf.get("max_drawdown", 0.0)),
        "n_obs": float(n_obs),
        "n_trades": n_trades,
        "mcpt_p_value": float(rotation.get("p_value")) if not rotation.get("insufficient_data") else None,
        "mcpt_block_p_value": float(blocks.get("p_value")) if not blocks.get("insufficient_data") else None,
        "bootstrap_sharpe_ci_lower": float(boot.get("ci_lower_05")) if not boot.get("insufficient_data") else None,
        "bootstrap_prob_positive": boot.get("prob_positive"),
        "pbo": pbo.get("pbo") if pbo and not pbo.get("insufficient_data") else None,
        "dsr": dsr.get("dsr") if dsr else None,
        "expected_max_sharpe": dsr.get("expected_max_sharpe") if dsr else None,
        "survives_2x_costs": costs.get("survives_2x_costs"),
        "p99_max_drawdown_trades": trade_stats.get("p99_max_drawdown"),
        "prob_final_negative": trade_stats.get("prob_final_negative"),
    }
    return {
        "performance": perf,
        "rotation_test": rotation,
        "block_test": blocks,
        "bootstrap": {key: value for key, value in boot.items() if key != "distribution"},
        "cost_stress": costs,
        "trade_bootstrap": trade_stats,
        "deflated_sharpe": dsr,
        "overfitting": pbo,
        "statistics": statistics,
    }


def certify_strategy(
    *,
    strategy_id: str,
    pair: str,
    model_payload_sha256: str,
    dataset_fingerprint: str,
    created_at: str,
    n_trials: int,
    thresholds: AcceptanceThresholds | None = None,
    **validate_kwargs: Any,
) -> tuple[dict[str, Any], Any]:
    """Validate, then seal the result into an activation-checkable certificate."""

    report = validate_strategy(**validate_kwargs)
    certificate = build_certificate(
        strategy_id=strategy_id,
        pair=pair,
        model_payload_sha256=model_payload_sha256,
        dataset_fingerprint=dataset_fingerprint,
        created_at=created_at,
        n_trials=n_trials,
        statistics=report["statistics"],
        thresholds=thresholds,
    )
    return report, certificate
