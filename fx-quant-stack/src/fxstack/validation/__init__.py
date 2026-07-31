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

from fxstack.validation.certificate import (
    CERTIFICATE_SCHEMA_VERSION,
    AcceptanceThresholds,
    ValidationCertificate,
    build_certificate,
    evaluate_acceptance,
    load_certificate,
)
from fxstack.validation.mcpt import (
    block_permutation_test,
    cost_stress_curve,
    rotation_permutation_test,
    strategy_returns,
)
from fxstack.validation.metrics import (
    PERIODS_PER_YEAR,
    max_drawdown,
    sharpe_ratio,
    summarize,
)
from fxstack.validation.overfitting import (
    deannualize_sharpe,
    deflated_sharpe_ratio,
    expected_max_sharpe,
    probabilistic_sharpe_ratio,
    probability_of_backtest_overfitting,
    sharpe_variance_across_trials,
)
from fxstack.validation.resampling import (
    bootstrap_statistic,
    optimal_block_length,
    trade_bootstrap,
)

__all__ = [
    "CERTIFICATE_SCHEMA_VERSION",
    "PERIODS_PER_YEAR",
    "AcceptanceThresholds",
    "ValidationCertificate",
    "block_permutation_test",
    "bootstrap_statistic",
    "build_certificate",
    "cost_stress_curve",
    "deannualize_sharpe",
    "deflated_sharpe_ratio",
    "evaluate_acceptance",
    "expected_max_sharpe",
    "load_certificate",
    "max_drawdown",
    "optimal_block_length",
    "probabilistic_sharpe_ratio",
    "probability_of_backtest_overfitting",
    "rotation_permutation_test",
    "sharpe_ratio",
    "sharpe_variance_across_trials",
    "strategy_returns",
    "summarize",
    "trade_bootstrap",
]
