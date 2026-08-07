# AGENT: ROLE: Measure whether concurrently-running sleeves actually diversify, and demote the redundant ones.
# AGENT: ENTRYPOINT: `evaluate_sleeve_complementarity` from runtime adaptive portfolio paths and isolated research.
# AGENT: PRIMARY INPUTS: `SleeveGovernanceTracker.export_state()` trade events and `SleeveHealthSnapshot` health.
# AGENT: PRIMARY OUTPUTS: `ComplementaritySnapshot` with per-sleeve admit/demote verdicts and reasons.
# AGENT: DEPENDS ON: `fxstack/strategy/allocator_types.py`, `fxstack/strategy/sleeve_governance.py`.
# AGENT: CALLED BY: `fxstack/runtime/runner.py` and isolated research tooling.
# AGENT: STATE / SIDE EFFECTS: none; pure function over its inputs.
# AGENT: HANDSHAKES: allocator sleeve admission and sleeve-level telemetry.
# AGENT: SEE: `docs/architecture/REFACTOR_PLAN.md` -> `fxstack/strategy/sleeve_governance.py` -> `fxstack/strategy/allocator.py`
"""Complementarity gate: two sleeves that win and lose together are one bet, twice.

Running four playbooks concurrently is only diversification if their outcomes are
actually distinct. If ``range_mean_reversion`` and ``failed_breakout_reversal``
both fade extension and both get paid in the same conditions, stacking them
doubles position risk while adding no edge -- and the portfolio correlation layer
cannot see it, because it measures correlation between *instruments*, not between
*strategies*.

This module measures the strategy axis.

Method
------
Realized closed-trade PnL is bucketed per sleeve by ``(pair, session_bucket)`` --
the conditions a trade was taken under -- and the resulting bucket vectors are
compared pairwise. Bucketing by condition rather than by trade index matters:
sleeves fire at different times and take different trade counts, so there is no
common time axis to correlate on, but there IS a common *condition* axis. The
question the correlation answers is the useful one: "do these two sleeves make and
lose money in the same circumstances?"

Only high POSITIVE correlation is penalized. Negative correlation is the most
valuable relationship a pair of sleeves can have -- one pays when the other does
not -- so it is explicitly admitted rather than flagged.

Evidence policy
---------------
Below :data:`MIN_SHARED_BUCKETS` overlapping buckets or :data:`MIN_SLEEVE_TRADES`
trades, no claim is made and nothing is demoted. Demoting on three trades' worth
of noise destroys more edge than the redundancy costs. This gate is deliberately
NOT the "has this sleeve got any edge at all" gate -- that is the statistical
warrant in ``fxstack/validation/activation_gate.py``, which fails closed. The two
are complementary: one asks *is it real*, this one asks *is it distinct*.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import math
from typing import Any

from fxstack.strategy.allocator_types import SleeveHealthSnapshot


#: Pearson correlation at or above which two sleeves are treated as the same bet.
#: 0.70 is the conventional "substantial shared variance" line (~49% of variance
#: in common). Kept a module constant, not an env knob: a threshold that operators
#: can quietly relax is a threshold that stops meaning anything.
REDUNDANT_CORRELATION_THRESHOLD = 0.70

#: Minimum closed trades a sleeve needs before it can be judged at all.
MIN_SLEEVE_TRADES = 8

#: Minimum overlapping (pair, session) buckets before a correlation is computed.
#: Two points always correlate perfectly; three is still noise.
MIN_SHARED_BUCKETS = 4

VERDICT_ADMITTED = "admitted"
VERDICT_DEMOTED = "demoted_redundant"
VERDICT_INSUFFICIENT = "insufficient_evidence"

COMPLEMENTARITY_SNAPSHOT_VERSION = 1


@dataclass(frozen=True)
class SleevePairCorrelation:
    """One measured relationship between two sleeves."""

    left: str
    right: str
    correlation: float
    shared_buckets: int
    redundant: bool


@dataclass(frozen=True)
class SleeveVerdict:
    """Whether one sleeve may take new entries this cycle, and why."""

    sleeve: str
    verdict: str
    reason: str = ""
    #: The sleeve this one was found redundant against, when demoted.
    redundant_with: str = ""
    correlation: float = 0.0


@dataclass(frozen=True)
class ComplementaritySnapshot:
    """Full result: pairwise measurements plus a verdict per sleeve."""

    verdicts: dict[str, SleeveVerdict] = field(default_factory=dict)
    correlations: list[SleevePairCorrelation] = field(default_factory=list)
    evaluated_sleeves: list[str] = field(default_factory=list)

    def admits(self, sleeve: str) -> bool:
        """True when ``sleeve`` may take new entries.

        Unknown and insufficiently-evidenced sleeves are admitted: this gate
        only ever *withholds* a sleeve it has positively shown to be redundant.
        """
        verdict = self.verdicts.get(str(sleeve or "").strip())
        if verdict is None:
            return True
        return verdict.verdict != VERDICT_DEMOTED

    def block_reason(self, sleeve: str) -> str:
        """Entry-block reason for ``sleeve``, or ``""`` when admitted."""
        verdict = self.verdicts.get(str(sleeve or "").strip())
        if verdict is None or verdict.verdict != VERDICT_DEMOTED:
            return ""
        return f"sleeve_redundant_with:{verdict.redundant_with}"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe telemetry projection."""
        return {
            "schema_version": COMPLEMENTARITY_SNAPSHOT_VERSION,
            "threshold": float(REDUNDANT_CORRELATION_THRESHOLD),
            "evaluated_sleeves": list(self.evaluated_sleeves),
            "verdicts": {
                sleeve: {
                    "verdict": verdict.verdict,
                    "reason": verdict.reason,
                    "redundant_with": verdict.redundant_with,
                    "correlation": float(verdict.correlation),
                }
                for sleeve, verdict in sorted(self.verdicts.items())
            },
            "correlations": [
                {
                    "left": item.left,
                    "right": item.right,
                    "correlation": float(item.correlation),
                    "shared_buckets": int(item.shared_buckets),
                    "redundant": bool(item.redundant),
                }
                for item in self.correlations
            ],
        }


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _condition_buckets(events: Any) -> dict[tuple[str, str], float]:
    """Aggregate a sleeve's realized PnL by ``(pair, session_bucket)``."""
    buckets: dict[tuple[str, str], float] = {}
    if not isinstance(events, (list, tuple)):
        return buckets
    for event in events:
        if not isinstance(event, Mapping):
            continue
        pnl = _finite(event.get("realized_pnl_usd"))
        if pnl is None:
            continue
        key = (
            str(event.get("pair") or "").strip().upper(),
            str(event.get("session_bucket") or "").strip().lower(),
        )
        buckets[key] = buckets.get(key, 0.0) + pnl
    return buckets


def _trade_count(events: Any) -> int:
    if not isinstance(events, (list, tuple)):
        return 0
    return sum(
        1
        for event in events
        if isinstance(event, Mapping) and _finite(event.get("realized_pnl_usd")) is not None
    )


def _pearson(left: list[float], right: list[float]) -> float | None:
    """Pearson correlation, or ``None`` when it is undefined.

    Undefined includes the degenerate case where one side is constant across
    every shared bucket -- zero variance means the relationship carries no
    information, and numpy would return NaN.
    """
    count = len(left)
    if count < 2 or count != len(right):
        return None
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    dev_left = [value - mean_left for value in left]
    dev_right = [value - mean_right for value in right]
    var_left = sum(value * value for value in dev_left)
    var_right = sum(value * value for value in dev_right)
    if var_left <= 0.0 or var_right <= 0.0:
        return None
    covariance = sum(a * b for a, b in zip(dev_left, dev_right))
    denominator = math.sqrt(var_left * var_right)
    if denominator <= 0.0:
        return None
    correlation = covariance / denominator
    if not math.isfinite(correlation):
        return None
    # Guard float drift past the mathematical bounds.
    return max(-1.0, min(1.0, correlation))


def _sleeve_strength(snapshot: SleeveHealthSnapshot | None) -> tuple[float, float, float]:
    """Rank key for "which of two redundant sleeves should keep trading".

    Expectancy first (money per trade is the objective), profit factor second,
    health score third. Callers break remaining ties by sleeve name so the
    outcome is deterministic.
    """
    if snapshot is None:
        return (0.0, 0.0, 0.0)
    return (
        _finite(getattr(snapshot, "expectancy_usd", 0.0)) or 0.0,
        _finite(getattr(snapshot, "profit_factor", 0.0)) or 0.0,
        _finite(getattr(snapshot, "score", 0.0)) or 0.0,
    )


def evaluate_sleeve_complementarity(
    *,
    governance_state: Mapping[str, Any] | None,
    health: Mapping[str, SleeveHealthSnapshot] | None = None,
    threshold: float = REDUNDANT_CORRELATION_THRESHOLD,
) -> ComplementaritySnapshot:
    """Decide which sleeves are distinct enough to run alongside each other.

    ``governance_state`` is ``SleeveGovernanceTracker.export_state()``. ``health``
    is its ``snapshot()``, used only to pick the survivor of a redundant pair.

    A sleeve is demoted when it correlates at or above ``threshold`` with a
    *stronger* sleeve. The stronger side of every redundant pair always keeps
    trading, so the gate can never empty the book.
    """
    snapshots = dict(health or {})
    state = dict(governance_state or {})
    raw_events = state.get("trade_events")
    events_by_sleeve: dict[str, Any] = dict(raw_events) if isinstance(raw_events, Mapping) else {}

    sleeves = sorted(str(name) for name in events_by_sleeve)
    verdicts: dict[str, SleeveVerdict] = {}
    buckets_by_sleeve: dict[str, dict[tuple[str, str], float]] = {}

    for sleeve in sleeves:
        events = events_by_sleeve.get(sleeve)
        if _trade_count(events) < MIN_SLEEVE_TRADES:
            verdicts[sleeve] = SleeveVerdict(
                sleeve=sleeve,
                verdict=VERDICT_INSUFFICIENT,
                reason=f"needs_{MIN_SLEEVE_TRADES}_closed_trades",
            )
            continue
        buckets_by_sleeve[sleeve] = _condition_buckets(events)
        verdicts[sleeve] = SleeveVerdict(sleeve=sleeve, verdict=VERDICT_ADMITTED)

    measurable = sorted(buckets_by_sleeve)
    correlations: list[SleevePairCorrelation] = []
    redundant_pairs: list[tuple[str, str, float]] = []

    for index, left in enumerate(measurable):
        for right in measurable[index + 1 :]:
            shared = sorted(set(buckets_by_sleeve[left]) & set(buckets_by_sleeve[right]))
            if len(shared) < MIN_SHARED_BUCKETS:
                continue
            correlation = _pearson(
                [buckets_by_sleeve[left][key] for key in shared],
                [buckets_by_sleeve[right][key] for key in shared],
            )
            if correlation is None:
                continue
            # Only positive co-movement is redundancy. A negatively correlated
            # pair is the diversification we are trying to protect.
            redundant = bool(correlation >= float(threshold))
            correlations.append(
                SleevePairCorrelation(
                    left=left,
                    right=right,
                    correlation=float(correlation),
                    shared_buckets=len(shared),
                    redundant=redundant,
                )
            )
            if redundant:
                redundant_pairs.append((left, right, float(correlation)))

    for left, right, correlation in redundant_pairs:
        left_key = (*_sleeve_strength(snapshots.get(left)), left)
        right_key = (*_sleeve_strength(snapshots.get(right)), right)
        # Higher strength wins; the name is the deterministic final tiebreak.
        loser, winner = (left, right) if left_key < right_key else (right, left)
        if verdicts.get(loser, SleeveVerdict(loser, VERDICT_ADMITTED)).verdict == VERDICT_DEMOTED:
            continue
        verdicts[loser] = SleeveVerdict(
            sleeve=loser,
            verdict=VERDICT_DEMOTED,
            reason="redundant_positive_correlation",
            redundant_with=winner,
            correlation=correlation,
        )

    return ComplementaritySnapshot(
        verdicts=verdicts,
        correlations=correlations,
        evaluated_sleeves=sleeves,
    )


__all__ = [
    "COMPLEMENTARITY_SNAPSHOT_VERSION",
    "MIN_SHARED_BUCKETS",
    "MIN_SLEEVE_TRADES",
    "REDUNDANT_CORRELATION_THRESHOLD",
    "VERDICT_ADMITTED",
    "VERDICT_DEMOTED",
    "VERDICT_INSUFFICIENT",
    "ComplementaritySnapshot",
    "SleevePairCorrelation",
    "SleeveVerdict",
    "evaluate_sleeve_complementarity",
]
