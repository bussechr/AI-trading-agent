"""Deterministic scalar objective + hard guardrail gate for candidate configs.

The proposer suggests; this module judges. A candidate is only eligible for
promotion if it clears the guardrails (enough trades, bounded drawdown, finite
objective). Among eligible candidates, a single scalar ``objective`` (a
Sharpe-like risk-adjusted score) orders them. Keeping judgement here -- never in
the proposer -- is what makes the loop trustworthy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class CandidateScore:
    objective: float
    passed_guardrails: bool
    metrics: dict[str, float]
    guardrail_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "objective": float(self.objective),
            "passed_guardrails": bool(self.passed_guardrails),
            "metrics": {k: float(v) for k, v in self.metrics.items()},
            "guardrail_failures": list(self.guardrail_failures),
        }


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return float(default)
    return num if math.isfinite(num) else float(default)


def score_metrics(
    metrics: dict[str, Any],
    *,
    min_trades: int = 30,
    max_drawdown_pct: float = 12.0,
) -> CandidateScore:
    """Scalarize backtest ``metrics`` and apply guardrails."""

    # Capture finiteness of the RAW inputs before _finite() coerces NaN/inf to 0,
    # otherwise the non-finite guardrail below could never fire.
    def _raw_finite(value: Any) -> bool:
        try:
            return math.isfinite(float(value))
        except (TypeError, ValueError):
            return False

    sharpe_is_finite = _raw_finite(metrics.get("sharpe"))
    drawdown_is_finite = _raw_finite(metrics.get("max_drawdown_pct"))

    m = {
        "trades": _finite(metrics.get("trades"), 0.0),
        "win_rate": _finite(metrics.get("win_rate"), 0.0),
        "mean_net_bps": _finite(metrics.get("mean_net_bps"), 0.0),
        "total_net_bps": _finite(metrics.get("total_net_bps"), 0.0),
        "sharpe": _finite(metrics.get("sharpe"), 0.0),
        "max_drawdown_pct": _finite(metrics.get("max_drawdown_pct"), 0.0),
    }
    trades = int(m["trades"])
    dd = float(m["max_drawdown_pct"])

    failures: list[str] = []
    if trades < int(min_trades):
        failures.append(f"too_few_trades({trades}<{int(min_trades)})")
    if dd > float(max_drawdown_pct):
        failures.append(f"drawdown_too_deep({dd:.2f}>{float(max_drawdown_pct):.2f})")
    if not sharpe_is_finite:
        failures.append("non_finite_sharpe")
    if not drawdown_is_finite:
        failures.append("non_finite_drawdown")

    # Risk-adjusted objective: Sharpe is primary; a small, bounded total-edge term
    # breaks ties without letting trade-count dominate. Drawdown is already gated.
    objective = m["sharpe"] + 0.05 * math.tanh(m["total_net_bps"] / 500.0)
    objective = objective if math.isfinite(objective) else -1e9

    return CandidateScore(
        objective=float(objective),
        passed_guardrails=not failures,
        metrics=m,
        guardrail_failures=failures,
    )
