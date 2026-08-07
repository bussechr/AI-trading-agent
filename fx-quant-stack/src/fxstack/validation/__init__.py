# AGENT: ROLE: Statistical validation of strategy edge -- resampling, permutation tests, selection-bias corrections, and enforceable acceptance certificates.
# AGENT: ENTRYPOINT: `fxstack/validation/report.py` (`validate_strategy`).
# AGENT: PRIMARY INPUTS: per-period strategy returns, position series, trade PnL lists, and the improve loop's per-config return matrix.
# AGENT: PRIMARY OUTPUTS: `ValidationCertificate` (sealed, fail-closed) plus per-test statistic dicts.
# AGENT: DEPENDS ON: numpy, scipy.
# AGENT: CALLED BY: `fx-quant-stack/src/fxstack/training/activation.py`, `fx-quant-stack/src/fxstack/improve/loop.py`.
# AGENT: STATE / SIDE EFFECTS: pure computation; certificate persistence is the caller's concern.
# AGENT: HANDSHAKES: validation certificate -> model activation gate.
# AGENT: SEE: `fx-quant-stack/src/fxstack/validation/certificate.py` -> `fx-quant-stack/src/fxstack/training/activation.py`
"""Statistical validation layer.

Answers the only question that matters before risking money: *is this edge real,
or is it the best of a lucky search?* Nothing else in this stack asked it -- the
previous robustness surface was a one-step knob perturbation, which detects a
curve-fit spike but cannot distinguish skill from luck.
"""

from __future__ import annotations

from fxstack._lazy import bind_lazy_exports


_EXPORTS = {
    "CERTIFICATE_SCHEMA_VERSION": "fxstack.validation.certificate",
    "PERIODS_PER_YEAR": "fxstack.validation.metrics",
    "AcceptanceThresholds": "fxstack.validation.certificate",
    "ValidationCertificate": "fxstack.validation.certificate",
    "block_permutation_test": "fxstack.validation.mcpt",
    "bootstrap_statistic": "fxstack.validation.resampling",
    "build_certificate": "fxstack.validation.certificate",
    "cost_stress_curve": "fxstack.validation.mcpt",
    "deannualize_sharpe": "fxstack.validation.overfitting",
    "deflated_sharpe_ratio": "fxstack.validation.overfitting",
    "evaluate_acceptance": "fxstack.validation.certificate",
    "expected_max_sharpe": "fxstack.validation.overfitting",
    "load_certificate": "fxstack.validation.certificate",
    "max_drawdown": "fxstack.validation.metrics",
    "optimal_block_length": "fxstack.validation.resampling",
    "probabilistic_sharpe_ratio": "fxstack.validation.overfitting",
    "probability_of_backtest_overfitting": "fxstack.validation.overfitting",
    "rotation_permutation_test": "fxstack.validation.mcpt",
    "sharpe_ratio": "fxstack.validation.metrics",
    "sharpe_variance_across_trials": "fxstack.validation.overfitting",
    "strategy_returns": "fxstack.validation.mcpt",
    "summarize": "fxstack.validation.metrics",
    "trade_bootstrap": "fxstack.validation.resampling",
}

__getattr__, __dir__ = bind_lazy_exports(__name__, globals(), _EXPORTS)
