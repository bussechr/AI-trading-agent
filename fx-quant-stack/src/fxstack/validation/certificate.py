"""Validation certificates: the enforcement bridge for statistical evidence.

This codebase's characteristic failure is a sophisticated layer that computes a
verdict nobody consumes. Statistics are especially prone to it -- a Monte Carlo
report written to an artifacts directory changes nothing. So the certificate is
designed to be *checkable at activation time* and to fail closed:

  * every acceptance statistic is REQUIRED; a missing or non-finite value is a
    failure, never a pass-by-default;
  * the certificate is cryptographically bound to the exact model payload digest
    and dataset fingerprint it was computed from, so it cannot be silently
    reused for a retrained model or a different data window;
  * ``verify`` recomputes the binding hash, so editing a statistic after the
    fact invalidates the certificate.

The thresholds are defaults, not laws, but each one has a reason recorded next to
it. Raising a threshold should be a deliberate, reviewed act -- which is why they
live in a frozen dataclass rather than in scattered env vars.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
from typing import Any

CERTIFICATE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AcceptanceThresholds:
    """The statistical bar a strategy must clear to trade real size.

    Rationale for each default:

    ``mcpt_p_value_max`` 0.05 -- conventional, and the rotation test is a genuine
    timing test rather than a data-mined comparison, so 5% is meaningful here.

    ``bootstrap_sharpe_ci_lower_min`` 0.0 -- the 5th percentile of the block
    bootstrap Sharpe must be positive. A point-estimate Sharpe with a CI
    straddling zero is not evidence of anything.

    ``pbo_max`` 0.40 -- CSCV probability of overfitting. 0.5 is coin-flip
    selection; anything at or above 0.5 means the search learned nothing
    generalizable. 0.40 leaves headroom while still rejecting pure noise.

    ``dsr_min`` 0.95 -- deflated Sharpe, i.e. P(real given the search size).

    ``min_trades`` 100 -- below roughly a hundred completed trades neither the
    bootstrap nor the drawdown distribution is informative for FX.

    ``max_drawdown_max`` 0.25 -- a validated strategy may still be untradeable
    if its historical path is unacceptable.

    ``require_cost_survival`` True -- Sharpe must remain positive at 2x the
    quoted round-trip spread. Retail FX slippage and news widening make 1x an
    optimistic assumption, and cost sensitivity is the most common way a
    backtested FX edge fails live.
    """

    mcpt_p_value_max: float = 0.05
    bootstrap_sharpe_ci_lower_min: float = 0.0
    pbo_max: float = 0.40
    dsr_min: float = 0.95
    min_trades: int = 100
    max_drawdown_max: float = 0.25
    require_cost_survival: bool = True


def _finite(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def evaluate_acceptance(
    statistics: dict[str, Any],
    *,
    thresholds: AcceptanceThresholds | None = None,
) -> tuple[bool, list[str]]:
    """Fail-closed acceptance decision.

    Returns ``(passed, reasons)``. ``reasons`` is empty only on a clean pass;
    every missing statistic produces its own explicit ``missing_*`` reason so an
    operator can tell "not measured" from "measured and failed".
    """

    bar = thresholds or AcceptanceThresholds()
    reasons: list[str] = []
    stats = dict(statistics or {})

    p_value = _finite(stats.get("mcpt_p_value"))
    if p_value is None:
        reasons.append("missing_mcpt_p_value")
    elif p_value > bar.mcpt_p_value_max:
        reasons.append(f"mcpt_p_value_too_high:{p_value:.4f}>{bar.mcpt_p_value_max}")

    ci_lower = _finite(stats.get("bootstrap_sharpe_ci_lower"))
    if ci_lower is None:
        reasons.append("missing_bootstrap_sharpe_ci_lower")
    elif ci_lower <= bar.bootstrap_sharpe_ci_lower_min:
        reasons.append(f"bootstrap_sharpe_ci_includes_zero:{ci_lower:.4f}")

    pbo = _finite(stats.get("pbo"))
    if pbo is None:
        reasons.append("missing_pbo")
    elif pbo > bar.pbo_max:
        reasons.append(f"pbo_too_high:{pbo:.4f}>{bar.pbo_max}")

    dsr = _finite(stats.get("dsr"))
    if dsr is None:
        reasons.append("missing_dsr")
    elif dsr < bar.dsr_min:
        reasons.append(f"dsr_too_low:{dsr:.4f}<{bar.dsr_min}")

    n_trades = _finite(stats.get("n_trades"))
    if n_trades is None:
        reasons.append("missing_n_trades")
    elif n_trades < float(bar.min_trades):
        reasons.append(f"too_few_trades:{int(n_trades)}<{bar.min_trades}")

    max_dd = _finite(stats.get("max_drawdown"))
    if max_dd is None:
        reasons.append("missing_max_drawdown")
    elif max_dd > bar.max_drawdown_max:
        reasons.append(f"max_drawdown_too_deep:{max_dd:.4f}>{bar.max_drawdown_max}")

    if bar.require_cost_survival:
        survives = stats.get("survives_2x_costs")
        if survives is None:
            reasons.append("missing_survives_2x_costs")
        elif not bool(float(survives)) if isinstance(survives, (int, float)) else not bool(survives):
            reasons.append("fails_2x_cost_stress")

    return (not reasons), reasons


@dataclass(frozen=True)
class ValidationCertificate:
    """Statistical evidence bound to the artifact it was computed from."""

    strategy_id: str
    pair: str
    model_payload_sha256: str
    dataset_fingerprint: str
    created_at: str
    n_trials: int
    statistics: dict[str, Any] = field(default_factory=dict)
    thresholds: dict[str, Any] = field(default_factory=dict)
    passed: bool = False
    reasons: list[str] = field(default_factory=list)
    schema_version: int = CERTIFICATE_SCHEMA_VERSION
    certificate_sha256: str = ""

    def binding_payload(self) -> dict[str, Any]:
        """The exact fields the binding hash covers."""

        return {
            "schema_version": int(self.schema_version),
            "strategy_id": str(self.strategy_id),
            "pair": str(self.pair).upper(),
            "model_payload_sha256": str(self.model_payload_sha256),
            "dataset_fingerprint": str(self.dataset_fingerprint),
            "created_at": str(self.created_at),
            "n_trials": int(self.n_trials),
            "statistics": self.statistics,
            "thresholds": self.thresholds,
            "passed": bool(self.passed),
            "reasons": list(self.reasons),
        }

    def compute_hash(self) -> str:
        blob = json.dumps(self.binding_payload(), sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["certificate_sha256"] = self.certificate_sha256 or self.compute_hash()
        return out

    def verify(
        self,
        *,
        expected_model_payload_sha256: str | None = None,
        expected_dataset_fingerprint: str | None = None,
    ) -> tuple[bool, list[str]]:
        """Re-check the seal and, optionally, what it is sealed to."""

        problems: list[str] = []
        if not self.certificate_sha256:
            problems.append("certificate_unsigned")
        elif self.certificate_sha256 != self.compute_hash():
            problems.append("certificate_hash_mismatch")
        if not self.passed:
            problems.append("certificate_not_passing")
        if expected_model_payload_sha256 is not None and str(expected_model_payload_sha256) != str(
            self.model_payload_sha256
        ):
            problems.append("model_payload_mismatch")
        if expected_dataset_fingerprint is not None and str(expected_dataset_fingerprint) != str(
            self.dataset_fingerprint
        ):
            problems.append("dataset_fingerprint_mismatch")
        return (not problems), problems


def build_certificate(
    *,
    strategy_id: str,
    pair: str,
    model_payload_sha256: str,
    dataset_fingerprint: str,
    created_at: str,
    n_trials: int,
    statistics: dict[str, Any],
    thresholds: AcceptanceThresholds | None = None,
) -> ValidationCertificate:
    """Evaluate acceptance and return a sealed certificate."""

    bar = thresholds or AcceptanceThresholds()
    passed, reasons = evaluate_acceptance(statistics, thresholds=bar)
    draft = ValidationCertificate(
        strategy_id=str(strategy_id),
        pair=str(pair).upper(),
        model_payload_sha256=str(model_payload_sha256),
        dataset_fingerprint=str(dataset_fingerprint),
        created_at=str(created_at),
        n_trials=int(n_trials),
        statistics=dict(statistics or {}),
        thresholds=asdict(bar),
        passed=bool(passed),
        reasons=list(reasons),
    )
    return ValidationCertificate(**{**asdict(draft), "certificate_sha256": draft.compute_hash()})


def load_certificate(payload: dict[str, Any]) -> ValidationCertificate:
    data = dict(payload or {})
    known = {
        "strategy_id", "pair", "model_payload_sha256", "dataset_fingerprint", "created_at",
        "n_trials", "statistics", "thresholds", "passed", "reasons", "schema_version",
        "certificate_sha256",
    }
    filtered = {key: value for key, value in data.items() if key in known}
    filtered.setdefault("strategy_id", "")
    filtered.setdefault("pair", "")
    filtered.setdefault("model_payload_sha256", "")
    filtered.setdefault("dataset_fingerprint", "")
    filtered.setdefault("created_at", "")
    filtered.setdefault("n_trials", 0)
    return ValidationCertificate(**filtered)
