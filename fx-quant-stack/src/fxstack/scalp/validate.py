# AGENT: ROLE: External research validator for advisory scalp candidate certificates; has no production arming path.
# AGENT: ENTRYPOINT: `evaluate_family` (pure verdict); `issue_arming_certificate` (writes cert IFF passed); CLI `python -m fxstack.scalp.validate`.
# AGENT: PRIMARY INPUTS: backtest result JSONs (per-config summaries + per-trade R series) or scalp ledger fills.
# AGENT: PRIMARY OUTPUTS: ArmingVerdict and an external candidate certificate on pass; never production authority.
# AGENT: DEPENDS ON: fxstack.validation (bootstrap, deflated Sharpe), fxstack.scalp.authority.
# AGENT: CALLED BY: external research/operator tooling and tests; excluded from the production runtime wheel.
"""Scalp validation battery -> advisory candidate certificate.

The production runtime deliberately does not read this certificate format.
Issuance demonstrates that local evidence satisfies the prototype semantics;
it does not establish external chronology, immutable engine identity, a
DB-owned activation generation, or broker-poll authority.

Standalone live mode is disabled. The battery takes
the falsification dataset (backtest trades at venue-realistic costs, or the
live shadow ledger) plus the honest count of every configuration tried, and
issues an external candidate certificate only when:

- bootstrap 95% CI lower bound of mean R  > 0
- deflated Sharpe (correcting for the number of trials) >= threshold
- at least ``min_trades`` trades
- all 18 canonical FX symbols x BUY/SELL clear their own evidence thresholds
- one predeclared bracket trade at most per pair/direction/UTC-day
- a win is a full target hit first; initial reward:risk is fixed at >= 1.0
- every target is >= 4x measured stressed round-trip venue cost
- trade, venue-cost, preregistration, and attempt-ledger bytes are SHA-bound

The candidate certificate is Ed25519-signed, sha-bound to the declared scalp
config, symbol-scoped, revocation-state-bound, and expiring. It remains
advisory because its mutable local revocation tree is not an externally
monotonic production authority. A family that fails gets a verdict object --
never a certificate, never a partial one.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import random
import time
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from fxstack.scalp.authority import (
    ARMING_POLICY,
    ATTEMPT_LEDGER_SCHEMA,
    CANONICAL_FX_SYMBOLS,
    EVIDENCE_ARTIFACT_SHA256_FIELDS,
    LOADED_AUTHORITY_STATE_FIELD,
    MAX_TRADES_PER_CELL_UTC_DAY,
    MIN_INITIAL_REWARD_RISK,
    MIN_TARGET_STRESSED_COST_MULTIPLE,
    SUPPORTED_VENUE_ID,
    TRADE_EVIDENCE_SCHEMA,
    WIN_DEFINITION,
    authenticated_revoked_certificate_ids,
    arming_evidence_error,
    arming_policy_sha256,
    certificate_authentication_error,
    config_sha256,
    evidence_artifact_identity_error,
    load_arming_signing_key,
    load_arming_verify_key,
    sign_arming_certificate,
    sign_revocation_registry,
    preregistration_contract_error,
    venue_cost_provenance_error,
    wilson_lower_bound,
    win_rate_confidence_metadata,
)
from fxstack.scalp.backtest import bootstrap_ci_mean
from fxstack.scalp.config import ScalpConfig
from fxstack.validation.overfitting import deflated_sharpe_ratio

DEFAULT_CERT_RELPATH = "arming_certificate.json"
REVOCATION_RELPATH = "arming_certificate.revoked.json"
CERT_VALIDITY_SECS = float(ARMING_POLICY["max_certificate_validity_secs"])
DEFAULT_MIN_CELL_TRADES = int(ARMING_POLICY["min_cell_trades"])
DEFAULT_MIN_CELL_INDEPENDENT_DAYS = int(ARMING_POLICY["min_cell_independent_days"])
DEFAULT_WIN_RATE_FAMILY_CONFIDENCE = float(
    ARMING_POLICY["min_win_rate_family_confidence"]
)


@dataclasses.dataclass(slots=True)
class ArmingVerdict:
    passed: bool
    reasons: list[str]
    trades: int
    mean_r: float
    ci_lo: float
    ci_hi: float
    deflated_sharpe: float
    trials: int
    venue: str
    artifact_sha256: dict[str, str] = dataclasses.field(default_factory=dict)
    venue_cost_provenance: dict[str, Any] = dataclasses.field(default_factory=dict)
    preregistration: dict[str, Any] = dataclasses.field(default_factory=dict)
    trade_contract: dict[str, Any] = dataclasses.field(default_factory=dict)
    independent_days: int = 0
    min_trades_required: int = int(ARMING_POLICY["min_trades"])
    min_independent_days_required: int = int(ARMING_POLICY["min_independent_days"])
    dsr_threshold_required: float = float(ARMING_POLICY["min_dsr"])
    min_positive_quarter_fraction_required: float = float(
        ARMING_POLICY["min_positive_quarter_fraction"]
    )
    max_quarter_share_required: float = float(ARMING_POLICY["max_quarter_share"])
    quarter_stats: dict[str, dict[str, float]] = dataclasses.field(default_factory=dict)
    side_means: dict[str, float] = dataclasses.field(default_factory=dict)
    required_symbols: list[str] = dataclasses.field(default_factory=list)
    min_cell_trades: int = DEFAULT_MIN_CELL_TRADES
    min_cell_independent_days: int = DEFAULT_MIN_CELL_INDEPENDENT_DAYS
    min_cell_win_rate: float | None = None
    win_rate_family_confidence: float = DEFAULT_WIN_RATE_FAMILY_CONFIDENCE
    win_rate_confidence: dict[str, Any] = dataclasses.field(default_factory=dict)
    cell_stats: dict[str, dict[str, dict[str, Any]]] = dataclasses.field(
        default_factory=dict
    )
    passing_symbols: list[str] = dataclasses.field(default_factory=list)
    hypothesis_audit: dict[str, Any] = dataclasses.field(default_factory=dict)
    source_errors: list[str] = dataclasses.field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _quarter_key(epoch: float) -> str:
    import datetime as dt

    t = dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc)
    return f"{t.year}Q{(t.month - 1) // 3 + 1}"


def _day_key(epoch: float) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(float(epoch), dt.timezone.utc).strftime("%Y-%m-%d")


def _normalized_symbols(symbols: list[str] | tuple[str, ...] | None) -> list[str]:
    """Upper-case, de-duplicate, and discard blank symbol identifiers."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in symbols or ():
        symbol = str(raw or "").strip().upper()
        if symbol and symbol not in seen:
            seen.add(symbol)
            out.append(symbol)
    return out


def _build_hypothesis_audit(
    *,
    trials: int,
    attempted_hypotheses: list[str] | tuple[str, ...] | None,
    sealed_holdout: dict[str, Any] | None,
    attempt_ledger_sha256: str,
) -> tuple[dict[str, Any], list[str]]:
    """Seal either the complete attempted family or a pre-evaluation holdout."""
    if attempted_hypotheses is not None and sealed_holdout is not None:
        return {}, ["hypothesis_audit_ambiguous"]
    if attempted_hypotheses is not None:
        hypotheses = [str(item or "").strip() for item in attempted_hypotheses]
        if (
            not hypotheses
            or any(not item for item in hypotheses)
            or len(set(hypotheses)) != len(hypotheses)
            or len(hypotheses) != int(trials)
        ):
            return {}, ["attempted_hypotheses_incomplete"]
        return (
            {
                "mode": "attempted_hypotheses",
                "attempt_ledger_schema": ATTEMPT_LEDGER_SCHEMA,
                "attempt_ledger_sha256": str(attempt_ledger_sha256),
                "count": len(hypotheses),
                "hypotheses": hypotheses,
                "sha256": config_sha256({"hypotheses": hypotheses}),
            },
            [],
        )
    if sealed_holdout is not None:
        if not isinstance(sealed_holdout, dict):
            return {}, ["sealed_holdout_malformed"]
        base = {
            "mode": "sealed_holdout",
            "trials": int(trials),
            "manifest_sha256": sealed_holdout.get("manifest_sha256"),
            "dataset_sha256": sealed_holdout.get("dataset_sha256"),
            "strategy_space_sha256": sealed_holdout.get("strategy_space_sha256"),
            "sealed_before_evaluation": sealed_holdout.get("sealed_before_evaluation"),
            "attempt_ledger_schema": ATTEMPT_LEDGER_SCHEMA,
            "attempt_ledger_sha256": str(attempt_ledger_sha256),
        }
        return base | {"sha256": config_sha256(base)}, []
    return {}, ["hypothesis_audit_required_for_arming"]


def clustered_bootstrap_ci_mean(
    trades: list[dict[str, Any]], *, n_boot: int = 5000, seed: int = 1337
) -> tuple[float, float]:
    """95% CI of mean R, resampling DAYS rather than trades.

    Scalp trades are not independent draws: several pairs fire on the same
    news minute, and one session's regime produces a whole cluster of
    correlated outcomes. An iid trade bootstrap understates the CI by roughly
    the square root of the cluster size -- enough for a family with 42 trades
    on 6 days to look like n=42 and pass a battery it should fail.

    Resampling whole days preserves within-day correlation, so the interval
    reflects how many INDEPENDENT things were actually observed.
    """
    if not trades:
        return 0.0, 0.0
    by_day: dict[str, list[float]] = {}
    for t in trades:
        epoch = t.get("epoch")
        key = _day_key(epoch) if isinstance(epoch, (int, float)) and epoch > 0 else "_"
        by_day.setdefault(key, []).append(float(t.get("r") or 0.0))
    day_keys = sorted(by_day)
    if len(day_keys) < 2:
        values = [r for rs in by_day.values() for r in rs]
        return bootstrap_ci_mean(values, n_boot=n_boot, seed=seed)
    rng = random.Random(seed)
    n_days = len(day_keys)
    means: list[float] = []
    for _ in range(n_boot):
        pooled: list[float] = []
        for _ in range(n_days):
            pooled.extend(by_day[day_keys[rng.randrange(n_days)]])
        if pooled:
            means.append(sum(pooled) / len(pooled))
    if not means:
        return 0.0, 0.0
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def evaluate_family(
    *,
    trades: list[dict[str, Any]],
    trials: int,
    venue: str,
    min_trades: int = 300,
    dsr_threshold: float = 0.95,
    min_positive_quarter_fraction: float = 0.6,
    max_quarter_share: float = 0.4,
    min_independent_days: int = 60,
    required_symbols: list[str] | tuple[str, ...] | None = None,
    min_cell_trades: int = DEFAULT_MIN_CELL_TRADES,
    min_cell_independent_days: int = DEFAULT_MIN_CELL_INDEPENDENT_DAYS,
    min_cell_win_rate: float | None = None,
    win_rate_family_confidence: float = DEFAULT_WIN_RATE_FAMILY_CONFIDENCE,
    attempted_hypotheses: list[str] | tuple[str, ...] | None = None,
    sealed_holdout: dict[str, Any] | None = None,
    source_errors: list[str] | tuple[str, ...] | None = None,
    artifact_sha256: dict[str, str] | None = None,
    venue_cost_provenance: dict[str, Any] | None = None,
    preregistration: dict[str, Any] | None = None,
    attempt_ledger: dict[str, Any] | None = None,
) -> ArmingVerdict:
    """Pure verdict over a family's trades ({r, epoch, side, symbol} each).

    ``trials`` is the honest count of EVERY configuration evaluated while
    searching -- understating it inflates the deflated Sharpe and is the
    classic way backtests lie to their owners.

    Anti-overfit slicing: pooled statistics alone let one lucky trend carry a
    dead strategy, so the edge must ALSO hold across time and direction --
    most quarters positive, no single quarter dominating total R, and both
    BUY and SELL sides independently positive. Quick-fire directional
    execution that only worked long-in-an-uptrend dies here, before live.

    The 90% claim always covers the fixed 18-pair FX universe. Every symbol
    must have a separately passing BUY and SELL evidence cell. Each cell clears its
    explicit trade/day floors, positive mean R, and a day-clustered positive
    CI lower bound. Generic research may omit ``min_cell_win_rate``, but live
    arming cannot: every required cell's simultaneous one-sided lower bound
    must clear at least 0.90 with at least 0.95 family confidence. The Bernoulli
    unit is one trade per pair/direction/UTC-day. A win is only a full,
    predeclared target hit before the stop; profitable partial/time exits are
    not wins. Bonferroni allocation controls family-wise confidence across all
    36 cells. The signed evidence also binds the exact trade, cost,
    preregistration, and attempt-ledger bytes by SHA-256.
    """
    reasons = [str(reason) for reason in source_errors or () if str(reason)]
    recorded_source_errors = list(reasons)
    normalized_required_symbols = _normalized_symbols(required_symbols)
    arming_requested = bool(normalized_required_symbols)
    try:
        normalized_artifact_sha256 = {
            str(key): str(value or "").strip().lower()
            for key, value in dict(artifact_sha256 or {}).items()
        }
    except (TypeError, ValueError):
        normalized_artifact_sha256 = {}
    try:
        normalized_provenance = dict(venue_cost_provenance or {})
    except (TypeError, ValueError):
        normalized_provenance = {}
    try:
        normalized_preregistration = dict(preregistration or {})
    except (TypeError, ValueError):
        normalized_preregistration = {}
    try:
        normalized_attempt_ledger = dict(attempt_ledger or {})
    except (TypeError, ValueError):
        normalized_attempt_ledger = {}
    if arming_requested:
        artifact_error = evidence_artifact_identity_error(normalized_artifact_sha256)
        if artifact_error:
            reasons.append(artifact_error)
        if normalized_required_symbols != list(CANONICAL_FX_SYMBOLS):
            reasons.append("canonical_symbol_scope_required")
        provenance_error = venue_cost_provenance_error(
            normalized_provenance,
            required_symbols=normalized_required_symbols,
        )
        if provenance_error:
            reasons.append(provenance_error)
        preregistration_error = preregistration_contract_error(
            normalized_preregistration
        )
        if preregistration_error:
            reasons.append(preregistration_error)
        raw_attempted = normalized_attempt_ledger.get("hypotheses")
        if (
            normalized_attempt_ledger.get("schema") != ATTEMPT_LEDGER_SCHEMA
            or not isinstance(raw_attempted, list)
        ):
            reasons.append("attempt_ledger_contract_invalid")
        else:
            ledger_hypotheses = [str(value or "").strip() for value in raw_attempted]
            if attempted_hypotheses is None:
                attempted_hypotheses = ledger_hypotheses
            elif list(attempted_hypotheses) != ledger_hypotheses:
                reasons.append("attempt_ledger_hypotheses_mismatch")
    try:
        trials = int(trials)
        min_trades = int(min_trades)
        min_independent_days = int(min_independent_days)
        min_cell_trades = int(min_cell_trades)
        min_cell_independent_days = int(min_cell_independent_days)
        dsr_threshold = float(dsr_threshold)
        min_positive_quarter_fraction = float(min_positive_quarter_fraction)
        max_quarter_share = float(max_quarter_share)
    except (TypeError, ValueError, OverflowError):
        trials = 0
        min_trades = int(ARMING_POLICY["min_trades"])
        min_independent_days = int(ARMING_POLICY["min_independent_days"])
        min_cell_trades = DEFAULT_MIN_CELL_TRADES
        min_cell_independent_days = DEFAULT_MIN_CELL_INDEPENDENT_DAYS
        dsr_threshold = float(ARMING_POLICY["min_dsr"])
        min_positive_quarter_fraction = float(
            ARMING_POLICY["min_positive_quarter_fraction"]
        )
        max_quarter_share = float(ARMING_POLICY["max_quarter_share"])
        reasons.append("validation_thresholds_malformed")
    if trials < 1:
        reasons.append("trials_invalid")
    if arming_requested and (
        min_trades < int(ARMING_POLICY["min_trades"])
        or min_independent_days < int(ARMING_POLICY["min_independent_days"])
        or min_cell_trades < DEFAULT_MIN_CELL_TRADES
        or min_cell_independent_days < DEFAULT_MIN_CELL_INDEPENDENT_DAYS
        or not math.isfinite(dsr_threshold)
        or dsr_threshold < float(ARMING_POLICY["min_dsr"])
        or not math.isfinite(min_positive_quarter_fraction)
        or min_positive_quarter_fraction
        < float(ARMING_POLICY["min_positive_quarter_fraction"])
        or not math.isfinite(max_quarter_share)
        or not 0.0 < max_quarter_share <= float(ARMING_POLICY["max_quarter_share"])
    ):
        reasons.append("arming_validation_policy_weakened")
    try:
        min_cell_win_rate = (
            None if min_cell_win_rate is None else float(min_cell_win_rate)
        )
        win_rate_family_confidence = float(win_rate_family_confidence)
    except (TypeError, ValueError, OverflowError):
        min_cell_win_rate = None
        win_rate_family_confidence = 0.0
        reasons.append("win_rate_policy_malformed")
    if arming_requested and (
        min_cell_win_rate is None
        or not math.isfinite(min_cell_win_rate)
        or not float(ARMING_POLICY["min_cell_win_rate"]) <= min_cell_win_rate <= 1.0
    ):
        reasons.append("arming_win_rate_target_required")
    if (
        not math.isfinite(win_rate_family_confidence)
        or not 0.0 < win_rate_family_confidence < 1.0
        or (
            arming_requested
            and
            win_rate_family_confidence
            < float(ARMING_POLICY["min_win_rate_family_confidence"])
        )
    ):
        reasons.append("arming_win_rate_confidence_weakened")
        win_rate_family_confidence = DEFAULT_WIN_RATE_FAMILY_CONFIDENCE
    try:
        win_rate_confidence = win_rate_confidence_metadata(
            family_cells=(len(CANONICAL_FX_SYMBOLS) * 2 if arming_requested else 0),
            family_confidence=win_rate_family_confidence,
        )
    except ValueError:
        win_rate_confidence = {}
        reasons.append("win_rate_confidence_invalid")
    hypothesis_audit, hypothesis_errors = _build_hypothesis_audit(
        trials=trials,
        attempted_hypotheses=attempted_hypotheses,
        sealed_holdout=sealed_holdout,
        attempt_ledger_sha256=normalized_artifact_sha256.get("attempt_ledger", ""),
    )
    if arming_requested:
        reasons.extend(hypothesis_errors)

    validated_trades: list[dict[str, Any]] = []
    observed_reward_risks: list[float] = []
    observed_target_cost_multiples: list[float] = []
    seen_cell_days: set[tuple[str, str, str]] = set()
    try:
        cost_rows = dict(normalized_provenance.get("symbols") or {})
    except (TypeError, ValueError):
        cost_rows = {}
    preregistered_reward_risk = normalized_preregistration.get(
        "fixed_initial_reward_risk"
    )
    if not isinstance(trades, list):
        reasons.append("trades_malformed")
        trades = []
    for index, raw_trade in enumerate(trades):
        if not isinstance(raw_trade, dict):
            reasons.append(f"invalid_trade:{index}:record")
            continue
        raw_r = raw_trade.get("r")
        raw_epoch = raw_trade.get("epoch")
        if isinstance(raw_r, bool) or not isinstance(raw_r, (int, float)):
            reasons.append(f"invalid_trade:{index}:r")
            continue
        if isinstance(raw_epoch, bool) or not isinstance(raw_epoch, (int, float)):
            reasons.append(f"invalid_trade:{index}:epoch")
            continue
        r_value = float(raw_r)
        epoch = float(raw_epoch)
        side = str(raw_trade.get("side") or "").strip().upper()
        symbol = str(raw_trade.get("symbol") or "").strip().upper()
        if not math.isfinite(r_value):
            reasons.append(f"invalid_trade:{index}:r_nonfinite")
            continue
        if not math.isfinite(epoch) or epoch <= 0.0:
            reasons.append(f"invalid_trade:{index}:epoch_nonfinite")
            continue
        try:
            _day_key(epoch)
            _quarter_key(epoch)
        except (OSError, OverflowError, ValueError):
            reasons.append(f"invalid_trade:{index}:epoch_range")
            continue
        if side not in {"BUY", "SELL"}:
            reasons.append(f"invalid_trade:{index}:side")
            continue
        if not arming_requested:
            validated_trades.append(
                {
                    **raw_trade,
                    "r": r_value,
                    "epoch": epoch,
                    "side": side,
                    "symbol": symbol,
                }
            )
            continue
        if symbol not in normalized_required_symbols:
            reasons.append(f"invalid_trade:{index}:symbol_scope")
            continue
        if raw_trade.get("target_predeclared") is not True:
            reasons.append(f"invalid_trade:{index}:target_not_predeclared")
            continue
        target_hit_first = raw_trade.get("full_target_hit_first")
        if not isinstance(target_hit_first, bool):
            reasons.append(f"invalid_trade:{index}:target_hit_outcome")
            continue
        raw_risk_bps = raw_trade.get("initial_risk_bps")
        raw_target_bps = raw_trade.get("initial_target_bps")
        raw_reward_risk = raw_trade.get("initial_reward_risk")
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            for value in (raw_risk_bps, raw_target_bps, raw_reward_risk)
        ):
            reasons.append(f"invalid_trade:{index}:initial_geometry")
            continue
        risk_bps = float(raw_risk_bps)
        target_bps = float(raw_target_bps)
        reported_reward_risk = float(raw_reward_risk)
        if (
            not all(math.isfinite(value) for value in (risk_bps, target_bps, reported_reward_risk))
            or risk_bps <= 0.0
            or target_bps <= 0.0
            or reported_reward_risk < MIN_INITIAL_REWARD_RISK
        ):
            reasons.append(f"invalid_trade:{index}:initial_geometry")
            continue
        computed_reward_risk = target_bps / risk_bps
        if not math.isclose(
            reported_reward_risk, computed_reward_risk, rel_tol=1e-12, abs_tol=1e-12
        ):
            reasons.append(f"invalid_trade:{index}:reward_risk_inconsistent")
            continue
        if (
            isinstance(preregistered_reward_risk, bool)
            or not isinstance(preregistered_reward_risk, (int, float))
            or not math.isfinite(float(preregistered_reward_risk))
            or not math.isclose(
                computed_reward_risk,
                float(preregistered_reward_risk),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            reasons.append(f"invalid_trade:{index}:reward_risk_not_preregistered")
            continue
        try:
            cost_row = dict(cost_rows[symbol])
            stressed_round_trip_cost_bps = float(
                cost_row["stressed_round_trip_cost_bps"]
            )
        except (KeyError, TypeError, ValueError, OverflowError):
            reasons.append(f"invalid_trade:{index}:stressed_cost_unavailable")
            continue
        if (
            not math.isfinite(stressed_round_trip_cost_bps)
            or stressed_round_trip_cost_bps <= 0.0
        ):
            reasons.append(f"invalid_trade:{index}:stressed_cost_unavailable")
            continue
        target_cost_multiple = target_bps / stressed_round_trip_cost_bps
        if target_cost_multiple < MIN_TARGET_STRESSED_COST_MULTIPLE:
            reasons.append(
                f"invalid_trade:{index}:target_below_stressed_cost_multiple:"
                f"{target_cost_multiple:.6f}<{MIN_TARGET_STRESSED_COST_MULTIPLE:.6f}"
            )
            continue
        if target_hit_first and r_value <= 0.0:
            reasons.append(f"invalid_trade:{index}:target_hit_nonpositive_r")
            continue
        cell_day = (symbol, side, _day_key(epoch))
        if cell_day in seen_cell_days:
            reasons.append(
                f"multiple_trades_per_cell_utc_day:{symbol}:{side}:{cell_day[2]}"
            )
        seen_cell_days.add(cell_day)
        observed_reward_risks.append(computed_reward_risk)
        observed_target_cost_multiples.append(target_cost_multiple)
        validated_trades.append(
            {
                **raw_trade,
                "r": r_value,
                "epoch": epoch,
                "side": side,
                "symbol": symbol,
                "target_predeclared": True,
                "full_target_hit_first": target_hit_first,
                "initial_risk_bps": risk_bps,
                "initial_target_bps": target_bps,
                "initial_reward_risk": computed_reward_risk,
            }
        )
    if observed_reward_risks and any(
        not math.isclose(
            value, observed_reward_risks[0], rel_tol=1e-12, abs_tol=1e-12
        )
        for value in observed_reward_risks[1:]
    ):
        reasons.append("initial_reward_risk_not_fixed")
    preregistered_cost_multiple = normalized_preregistration.get(
        "min_target_stressed_cost_multiple"
    )
    if (
        observed_target_cost_multiples
        and not isinstance(preregistered_cost_multiple, bool)
        and isinstance(preregistered_cost_multiple, (int, float))
        and math.isfinite(float(preregistered_cost_multiple))
        and min(observed_target_cost_multiples)
        < float(preregistered_cost_multiple)
    ):
        reasons.append("target_cost_multiple_below_preregistration")
    trades = validated_trades
    trade_rs = [float(t["r"]) for t in trades]
    n = len(trade_rs)
    mean_r = sum(trade_rs) / n if n else 0.0
    # Day-clustered, NOT iid over trades: correlated same-session outcomes
    # must not masquerade as independent evidence.
    ci_lo, ci_hi = clustered_bootstrap_ci_mean(trades) if n else (0.0, 0.0)
    independent_days = len(
        {
            _day_key(t["epoch"])
            for t in trades
            if isinstance(t.get("epoch"), (int, float)) and t["epoch"] > 0
        }
    )
    if n < min_trades:
        reasons.append(f"insufficient_trades:{n}<{min_trades}")
    if independent_days < min_independent_days:
        # Trade count is not evidence count. A family that fires on a handful
        # of days has seen a handful of markets, whatever its trade tally.
        reasons.append(
            f"insufficient_independent_days:{independent_days}<{min_independent_days}"
        )
    if not math.isfinite(mean_r) or mean_r <= 0.0:
        reasons.append(f"mean_not_positive_or_finite:{mean_r}")
    if (
        not math.isfinite(ci_lo)
        or not math.isfinite(ci_hi)
        or ci_lo <= 0.0
        or ci_hi < ci_lo
    ):
        reasons.append(f"ci_lower_bound_not_positive:{ci_lo:.4f}")

    # Time slices: the edge must repeat, not have happened once.
    by_quarter: dict[str, list[float]] = {}
    for t in trades:
        epoch = t.get("epoch")
        if isinstance(epoch, (int, float)) and float(epoch) > 0:
            by_quarter.setdefault(_quarter_key(epoch), []).append(float(t.get("r") or 0.0))
    days_by_quarter: dict[str, set[str]] = {}
    for t in trades:
        epoch = t.get("epoch")
        if isinstance(epoch, (int, float)) and float(epoch) > 0:
            days_by_quarter.setdefault(_quarter_key(epoch), set()).add(_day_key(epoch))
    quarter_stats = {
        q: {
            "trades": float(len(rs)),
            "total_r": sum(rs),
            "mean_r": sum(rs) / len(rs),
            "days": float(len(days_by_quarter.get(q, ()))),
        }
        for q, rs in sorted(by_quarter.items())
    }
    # A quarter counts as evidence only if it saw enough distinct DAYS.
    scored = {
        q: s for q, s in quarter_stats.items() if s["trades"] >= 10 and s["days"] >= 5
    }
    if len(scored) < 4:
        reasons.append(f"insufficient_time_slices:{len(scored)}<4")
    else:
        positive = sum(1 for s in scored.values() if s["mean_r"] > 0.0)
        fraction = positive / len(scored)
        if fraction < min_positive_quarter_fraction:
            reasons.append(
                f"edge_not_repeatable_across_quarters:{fraction:.2f}<"
                f"{min_positive_quarter_fraction}"
            )
        total_r = sum(s["total_r"] for s in scored.values())
        if total_r > 0.0 and math.isfinite(total_r):
            top_share = max(s["total_r"] for s in scored.values()) / total_r
            if top_share > max_quarter_share:
                reasons.append(
                    f"single_quarter_dependence:{top_share:.2f}>{max_quarter_share}"
                )
        else:
            reasons.append("scored_quarter_total_not_positive")

    # Direction slices: long-only or short-only profit is a trend bet in
    # disguise, not directional execution.
    side_means: dict[str, float] = {}
    side_days: dict[str, int] = {}
    for side in ("BUY", "SELL"):
        side_trades = [t for t in trades if str(t.get("side") or "").upper() == side]
        if not side_trades:
            continue
        side_means[side] = sum(float(t.get("r") or 0.0) for t in side_trades) / len(side_trades)
        side_days[side] = len(
            {
                _day_key(t["epoch"])
                for t in side_trades
                if isinstance(t.get("epoch"), (int, float)) and t["epoch"] > 0
            }
        )
    if len(side_means) < 2:
        reasons.append("one_sided_trade_population")
    else:
        for side, side_mean in side_means.items():
            if side_mean <= 0.0:
                reasons.append(f"direction_dependent_edge:{side}:{side_mean:.4f}")
            # Each direction needs its own independent evidence, or "both
            # sides positive" is satisfied by one lucky session per side.
            if side_days.get(side, 0) < max(10, min_independent_days // 4):
                reasons.append(
                    f"direction_evidence_too_thin:{side}:{side_days.get(side, 0)}d"
                )

    # Required symbol x direction cells: pooled all-pair results cannot grant
    # authority to a pair or side that has no independently positive evidence.
    cell_stats: dict[str, dict[str, dict[str, Any]]] = {}
    passing_symbols: list[str] = []
    for symbol in normalized_required_symbols:
        symbol_cells: dict[str, dict[str, Any]] = {}
        symbol_passed = True
        for side in ("BUY", "SELL"):
            cell_trades = [
                t
                for t in trades
                if str(t.get("symbol") or "").strip().upper() == symbol
                and str(t.get("side") or "").strip().upper() == side
            ]
            cell_rs = [float(t.get("r") or 0.0) for t in cell_trades]
            cell_n = len(cell_rs)
            cell_mean = sum(cell_rs) / cell_n if cell_n else 0.0
            cell_ci_lo, cell_ci_hi = (
                clustered_bootstrap_ci_mean(cell_trades) if cell_n else (0.0, 0.0)
            )
            raw_day_stats: dict[str, dict[str, Any]] = {}
            for trade in cell_trades:
                day = _day_key(float(trade["epoch"]))
                day_row = raw_day_stats.setdefault(
                    day, {"trades": 0, "wins": 0, "total_r": 0.0}
                )
                day_row["trades"] += 1
                day_row["wins"] += int(trade["full_target_hit_first"] is True)
                day_row["total_r"] += float(trade["r"])
            day_stats = {day: raw_day_stats[day] for day in sorted(raw_day_stats)}
            cell_days = len(day_stats)
            wins = sum(
                int(trade["full_target_hit_first"] is True)
                for trade in cell_trades
            )
            win_rate = wins / cell_n if cell_n else 0.0
            winning_days = sum(
                int(row["trades"] == 1 and row["wins"] == 1)
                for row in day_stats.values()
            )
            day_win_rate = winning_days / cell_days if cell_days else 0.0
            z = float(win_rate_confidence.get("z") or 0.0)
            win_rate_ci_lo = wilson_lower_bound(
                wins=winning_days, trials=cell_days, z=z
            )
            cell_reasons: list[str] = []
            if cell_n < min_cell_trades:
                cell_reasons.append(
                    f"cell_insufficient_trades:{symbol}:{side}:"
                    f"{cell_n}<{min_cell_trades}"
                )
            if cell_days < min_cell_independent_days:
                cell_reasons.append(
                    f"cell_insufficient_independent_days:{symbol}:{side}:"
                    f"{cell_days}<{min_cell_independent_days}"
                )
            if not math.isfinite(cell_mean) or cell_mean <= 0.0:
                cell_reasons.append(
                    f"cell_mean_not_positive:{symbol}:{side}:{cell_mean:.4f}"
                )
            if (
                not math.isfinite(cell_ci_lo)
                or not math.isfinite(cell_ci_hi)
                or cell_ci_lo <= 0.0
                or cell_ci_hi < cell_ci_lo
            ):
                cell_reasons.append(
                    f"cell_ci_lower_bound_not_positive:{symbol}:{side}:"
                    f"{cell_ci_lo:.4f}"
                )
            if (
                min_cell_win_rate is not None
                and win_rate_ci_lo < min_cell_win_rate
            ):
                cell_reasons.append(
                    f"cell_win_rate_ci_lower_bound_below_threshold:{symbol}:{side}:"
                    f"{win_rate_ci_lo:.4f}<{min_cell_win_rate:.4f}:"
                    f"observed={win_rate:.4f}"
                )
            cell_passed = not cell_reasons
            symbol_passed = symbol_passed and cell_passed
            reasons.extend(cell_reasons)
            symbol_cells[side] = {
                "passed": cell_passed,
                "trades": cell_n,
                "independent_days": cell_days,
                "wins": wins,
                "win_definition": WIN_DEFINITION,
                "win_rate": win_rate,
                "winning_days": winning_days,
                "day_win_rate": day_win_rate,
                "win_rate_ci_lo": win_rate_ci_lo,
                "win_rate_confidence": dict(win_rate_confidence),
                "day_stats": day_stats,
                "mean_r": cell_mean,
                "ci_lo": cell_ci_lo,
                "ci_hi": cell_ci_hi,
                "reasons": cell_reasons,
            }
        cell_stats[symbol] = symbol_cells
        if symbol_passed:
            passing_symbols.append(symbol)

    dsr = 0.0
    if n >= 2:
        try:
            variance = sum((r - mean_r) ** 2 for r in trade_rs) / (n - 1)
            std = variance**0.5
            if math.isfinite(std) and std > 0.0:
                skew = sum((r - mean_r) ** 3 for r in trade_rs) / (n * std**3)
                kurt = sum((r - mean_r) ** 4 for r in trade_rs) / (n * std**4)
                # Variance of Sharpe across trials: without the per-config series
                # here, use the conservative iid approximation 1/n per trial.
                result = deflated_sharpe_ratio(
                    sharpe_per_period=mean_r / std,
                    n_obs=n,
                    n_trials=max(1, int(trials)),
                    sharpe_variance_across_trials=1.0 / max(1, n),
                    skew=skew,
                    kurtosis=kurt,
                )
                dsr = float(dict(result).get("dsr") or 0.0)
        except (ArithmeticError, OverflowError, TypeError, ValueError):
            dsr = float("nan")
    if not math.isfinite(dsr) or not 0.0 <= dsr <= 1.0 or dsr < dsr_threshold:
        reasons.append(f"deflated_sharpe_below_threshold:{dsr:.3f}<{dsr_threshold}")
    trade_contract = {
        "schema": TRADE_EVIDENCE_SCHEMA,
        "win_definition": WIN_DEFINITION,
        "fixed_initial_reward_risk": (
            observed_reward_risks[0] if observed_reward_risks else 0.0
        ),
        "min_target_stressed_cost_multiple": (
            min(observed_target_cost_multiples)
            if observed_target_cost_multiples
            else 0.0
        ),
        "max_trades_per_cell_utc_day": MAX_TRADES_PER_CELL_UTC_DAY,
        "trades": n,
        "full_target_hits": sum(
            int(trade.get("full_target_hit_first") is True) for trade in trades
        ),
    }
    return ArmingVerdict(
        passed=not reasons,
        reasons=reasons,
        trades=n,
        mean_r=mean_r,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        deflated_sharpe=dsr,
        trials=int(trials),
        venue=str(normalized_provenance.get("venue_id") or venue).strip().lower(),
        artifact_sha256=normalized_artifact_sha256,
        venue_cost_provenance=normalized_provenance,
        preregistration=normalized_preregistration,
        trade_contract=trade_contract,
        independent_days=independent_days,
        min_trades_required=min_trades,
        min_independent_days_required=min_independent_days,
        dsr_threshold_required=dsr_threshold,
        min_positive_quarter_fraction_required=min_positive_quarter_fraction,
        max_quarter_share_required=max_quarter_share,
        quarter_stats=quarter_stats,
        side_means=side_means,
        required_symbols=normalized_required_symbols,
        min_cell_trades=min_cell_trades,
        min_cell_independent_days=min_cell_independent_days,
        min_cell_win_rate=min_cell_win_rate,
        win_rate_family_confidence=win_rate_family_confidence,
        win_rate_confidence=win_rate_confidence,
        cell_stats=cell_stats,
        passing_symbols=passing_symbols,
        hypothesis_audit=hypothesis_audit,
        source_errors=recorded_source_errors,
    )


def issue_arming_certificate(
    *,
    verdict: ArmingVerdict,
    config: ScalpConfig,
    family: str,
    symbols: list[str],
    data_root: Path | None = None,
    now_epoch: float | None = None,
    authority_signing_key: Ed25519PrivateKey | None = None,
    authority_signing_key_file: str | Path | None = None,
) -> Path:
    """Write the certificate IFF the verdict passed; raises otherwise.

    The refusal is an exception, not a return value, so no code path can
    accidentally treat a failed battery as armed.
    """
    requested_symbols = _normalized_symbols(symbols)
    if requested_symbols != list(CANONICAL_FX_SYMBOLS):
        raise PermissionError("arming refused: canonical_symbol_scope_required")
    evidence = verdict.to_dict()
    semantic_error = arming_evidence_error(
        evidence, covered_symbols=requested_symbols
    )
    if semantic_error:
        raise PermissionError(f"arming refused: {semantic_error}")
    try:
        now = float(now_epoch if now_epoch is not None else time.time())
    except (TypeError, ValueError, OverflowError) as exc:
        raise PermissionError("arming refused: now_epoch_invalid") from exc
    if not math.isfinite(now) or now <= 0.0:
        raise PermissionError("arming refused: now_epoch_invalid")
    if not str(family or "").strip():
        raise PermissionError("arming refused: family_missing")
    signing_key = load_arming_signing_key(
        authority_signing_key, signing_key_file=authority_signing_key_file
    )
    if signing_key is None:
        raise PermissionError("arming refused: signing_key_unavailable")
    root = Path(data_root if data_root is not None else config.data_root)
    root.mkdir(parents=True, exist_ok=True)
    revoked_ids, revocation_error = load_revoked_certificate_ids(
        root, authority_verify_key=signing_key.public_key()
    )
    if revocation_error:
        raise PermissionError(f"arming refused: {revocation_error}")
    cert: dict[str, Any] = {
        "family": str(family),
        "config_sha256": scalp_config_sha256(config),
        "validation_policy_sha256": arming_policy_sha256(),
        "symbols": requested_symbols,
        "issued_at_epoch": now,
        "expires_at_epoch": now + CERT_VALIDITY_SECS,
        "evidence": evidence,
    }
    sign_arming_certificate(cert, signing_key=signing_key)
    authority_state: dict[str, Any] = {
        "state_updated_at_epoch": now,
        "reason": "certificate_issued",
        "active_certificate_sha256": cert["cert_sha256"],
        "revoked_certificate_sha256": "",
        "revoked_certificate_ids": sorted(revoked_ids),
        "prior_certificate_sha256": "",
        "quarantined_filename": "",
    }
    sign_revocation_registry(authority_state, signing_key=signing_key)
    marker_path = root / REVOCATION_RELPATH
    marker_temp = root / f".{REVOCATION_RELPATH}.tmp"
    marker_temp.write_text(
        json.dumps(authority_state, indent=1, allow_nan=False), encoding="utf-8"
    )
    # Publish the trust state first. A crash before the certificate replace is
    # a temporary fail-closed active-ID mismatch, never stale authority.
    marker_temp.replace(marker_path)
    path = root / DEFAULT_CERT_RELPATH
    temp = root / f".{DEFAULT_CERT_RELPATH}.tmp"
    temp.write_text(json.dumps(cert, indent=1, allow_nan=False), encoding="utf-8")
    temp.replace(path)
    return path


def scalp_config_sha256(config: ScalpConfig) -> str:
    """Sha over the execution-semantic scalp config fields."""
    payload = {
        field.name: getattr(config, field.name)
        for field in dataclasses.fields(config)
        if field.name not in {"bridge_url", "api_key_file", "data_root"}
    }
    return config_sha256(payload)


def load_revoked_certificate_ids(
    data_root: str | Path,
    *,
    authority_verify_key: Ed25519PublicKey | None = None,
    authority_verify_key_file: str | Path | None = None,
) -> tuple[set[str], str]:
    """Load and authenticate the append-only revocation identity registry."""
    verify_key = load_arming_verify_key(
        authority_verify_key, verify_key_file=authority_verify_key_file
    )
    if verify_key is None:
        return set(), "verify_key_unavailable"
    path = Path(data_root) / REVOCATION_RELPATH
    if not path.exists():
        return set(), ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set(), "revocation_registry_unreadable"
    return authenticated_revoked_certificate_ids(payload, verify_key=verify_key)


def load_certificate(
    data_root: str | Path,
    *,
    authority_verify_key: Ed25519PublicKey | None = None,
    authority_verify_key_file: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load only an authenticated, non-revoked certificate.

    A missing trust anchor, malformed signature, corrupt revocation registry,
    or restored quarantined identity all collapse to no authority.
    """
    path = Path(data_root) / DEFAULT_CERT_RELPATH
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    verify_key = load_arming_verify_key(
        authority_verify_key, verify_key_file=authority_verify_key_file
    )
    if certificate_authentication_error(payload, verify_key=verify_key):
        return None
    revoked_ids, revocation_error = load_revoked_certificate_ids(
        data_root, authority_verify_key=verify_key
    )
    if revocation_error or str(payload.get("cert_sha256") or "").lower() in revoked_ids:
        return None
    marker_path = Path(data_root) / REVOCATION_RELPATH
    try:
        authority_state = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    active_id = str(
        authority_state.get("active_certificate_sha256")
        if isinstance(authority_state, dict)
        else ""
    ).strip().lower()
    if active_id != str(payload.get("cert_sha256") or "").strip().lower():
        return None
    payload[LOADED_AUTHORITY_STATE_FIELD] = authority_state
    return payload


def revoke_arming_certificate(
    data_root: str | Path,
    *,
    reason: str,
    now_epoch: float | None = None,
    authority_signing_key: Ed25519PrivateKey | None = None,
    authority_signing_key_file: str | Path | None = None,
) -> Path | None:
    """Atomically quarantine an old authority after failed revalidation."""
    root = Path(data_root)
    root.mkdir(parents=True, exist_ok=True)
    now = float(now_epoch if now_epoch is not None else time.time())
    if not math.isfinite(now):
        now = time.time()
    active = root / DEFAULT_CERT_RELPATH
    quarantined: Path | None = None
    prior_sha = ""
    revoked_cert_sha = ""
    if active.exists():
        raw = active.read_bytes()
        prior_sha = hashlib.sha256(raw).hexdigest()
        try:
            active_payload = json.loads(raw)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            active_payload = {}
        candidate_id = str(
            active_payload.get("cert_sha256") if isinstance(active_payload, dict) else ""
        ).strip().lower()
        if len(candidate_id) == 64 and all(c in "0123456789abcdef" for c in candidate_id):
            revoked_cert_sha = candidate_id
        stem = f"arming_certificate.revoked.{int(now * 1_000_000)}.{prior_sha[:12]}"
        quarantined = root / f"{stem}.json"
        suffix = 0
        while quarantined.exists():
            suffix += 1
            quarantined = root / f"{stem}.{suffix}.json"
        active.replace(quarantined)
    signing_key = load_arming_signing_key(
        authority_signing_key, signing_key_file=authority_signing_key_file
    )
    existing_ids: set[str] = set()
    revocation_error = ""
    if signing_key is not None:
        existing_ids, revocation_error = load_revoked_certificate_ids(
            root, authority_verify_key=signing_key.public_key()
        )
    if revoked_cert_sha:
        existing_ids.add(revoked_cert_sha)
    marker: dict[str, Any] = {
        "revoked_at_epoch": now,
        "state_updated_at_epoch": now,
        "reason": str(reason or "revalidation_failed"),
        "active_certificate_sha256": "",
        "prior_certificate_sha256": prior_sha,
        "revoked_certificate_sha256": revoked_cert_sha,
        "revoked_certificate_ids": sorted(existing_ids),
        "quarantined_filename": quarantined.name if quarantined else "",
    }
    if signing_key is not None and not revocation_error:
        sign_revocation_registry(marker, signing_key=signing_key)
    else:
        # An unsigned/invalid marker is deliberately unreadable by the runtime,
        # so key loss or corrupt prior state still revokes all authority.
        marker["schema"] = "fxstack.scalp.arming_revocations.untrusted"
        marker["revocation_error"] = revocation_error or "signing_key_unavailable"
    marker_path = root / REVOCATION_RELPATH
    temp = root / f".{REVOCATION_RELPATH}.tmp"
    temp.write_text(json.dumps(marker, indent=1, allow_nan=False), encoding="utf-8")
    temp.replace(marker_path)
    if signing_key is None:
        raise PermissionError("revocation signing key unavailable")
    if revocation_error:
        raise PermissionError(f"revocation registry invalid: {revocation_error}")
    return quarantined


def _json_safe(value: Any) -> Any:
    """Replace non-finite diagnostics so refusal reports remain valid JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def trade_rs_from_backtest_json(paths: list[Path]) -> tuple[list[float], str]:
    """Not implemented from summaries: summaries carry aggregates, not trades.

    Kept explicit so nobody 'helpfully' reconstructs a trade series from
    aggregate stats -- rerun the backtest with a per-trade dump instead.
    """
    raise NotImplementedError(
        "arming needs per-trade R series; rerun fxstack.scalp.backtest with "
        "--trades-out and feed that file"
    )


def trades_from_ledger(
    ledger_dir: Path, *, errors: list[str] | None = None
) -> list[dict[str, Any]]:
    """Per-trade records from the live shadow ledger (fill records)."""
    out: list[dict[str, Any]] = []
    found_ledger = False
    for path in sorted(Path(ledger_dir).glob("ledger_*.jsonl")):
        found_ledger = True
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line_number, line in enumerate(fh, start=1):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        if errors is not None:
                            errors.append(f"ledger_malformed:{path.name}:{line_number}:json")
                        continue
                    if not isinstance(rec, dict):
                        if errors is not None:
                            errors.append(f"ledger_malformed:{path.name}:{line_number}:record")
                        continue
                    if rec.get("kind") != "fill":
                        continue
                    value = rec.get("pnl_r")
                    epoch = rec.get("exit_epoch")
                    side = str(rec.get("side") or "").strip().upper()
                    symbol = str(rec.get("symbol") or "").strip().upper()
                    exit_reason = str(rec.get("exit_reason") or "").strip().lower()
                    try:
                        meta = dict(rec.get("meta") or {})
                    except (TypeError, ValueError):
                        meta = {}
                    initial_risk_bps = meta.get("initial_risk_bps")
                    initial_target_bps = meta.get("initial_target_bps")
                    valid = (
                        not isinstance(value, bool)
                        and isinstance(value, (int, float))
                        and math.isfinite(float(value))
                        and not isinstance(epoch, bool)
                        and isinstance(epoch, (int, float))
                        and math.isfinite(float(epoch))
                        and float(epoch) > 0.0
                        and side in {"BUY", "SELL"}
                        and bool(symbol)
                        and meta.get("trade_evidence_schema") == TRADE_EVIDENCE_SCHEMA
                        and meta.get("target_predeclared") is True
                        and not isinstance(initial_risk_bps, bool)
                        and isinstance(initial_risk_bps, (int, float))
                        and math.isfinite(float(initial_risk_bps))
                        and float(initial_risk_bps) > 0.0
                        and not isinstance(initial_target_bps, bool)
                        and isinstance(initial_target_bps, (int, float))
                        and math.isfinite(float(initial_target_bps))
                        and float(initial_target_bps) > 0.0
                        and exit_reason
                        in {
                            "tp",
                            "sl",
                            "sl_wick",
                            "time_stop",
                            "breakeven",
                            "breakeven_wick",
                        }
                    )
                    if not valid:
                        if errors is not None:
                            errors.append(
                                f"ledger_malformed:{path.name}:{line_number}:fill"
                            )
                        continue
                    out.append(
                        {
                            "r": float(value),
                            "epoch": float(epoch),
                            "side": side,
                            "symbol": symbol,
                            "target_predeclared": True,
                            "full_target_hit_first": exit_reason == "tp",
                            "initial_risk_bps": float(initial_risk_bps),
                            "initial_target_bps": float(initial_target_bps),
                            "initial_reward_risk": (
                                float(initial_target_bps) / float(initial_risk_bps)
                            ),
                        }
                    )
        except OSError:
            if errors is not None:
                errors.append(f"ledger_unreadable:{path.name}")
            continue
    if not found_ledger and errors is not None:
        errors.append("ledger_missing")
    return out


def _file_sha256(path: str | Path) -> str:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return ""


def _ledger_bundle_sha256(ledger_dir: str | Path) -> str:
    """Identity of the immutable ledger inventory (names, sizes, and bytes)."""
    inventory: list[dict[str, Any]] = []
    for path in sorted(Path(ledger_dir).glob("ledger_*.jsonl")):
        try:
            raw = path.read_bytes()
        except OSError:
            return ""
        inventory.append(
            {
                "name": path.name,
                "bytes": len(raw),
                "sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return config_sha256({"files": inventory}) if inventory else ""


def _load_json_object(path: str | Path, *, error: str, errors: list[str]) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        errors.append(error)
        return {}
    if not isinstance(payload, dict):
        errors.append(error)
        return {}
    return payload


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--trades-json",
        default=None,
        help="immutable JSON trade ledger: {'trades': [...]}",
    )
    ap.add_argument("--ledger-dir", default=None,
                    help="immutable scalp live-shadow ledger directory")
    ap.add_argument(
        "--venue-cost-json",
        default=None,
        help="required supported venue-cost provenance artifact",
    )
    ap.add_argument(
        "--preregistration-json",
        default=None,
        help="required pre-evaluation strategy/estimand contract",
    )
    ap.add_argument(
        "--attempt-ledger-json",
        default=None,
        help="required immutable ledger of every attempted hypothesis",
    )
    ap.add_argument("--trials", type=int, required=True,
                    help="HONEST count of every config evaluated in the search")
    ap.add_argument("--family", required=True)
    ap.add_argument(
        "--symbols",
        required=True,
        help="must be the exact canonical 18-pair FX universe",
    )
    ap.add_argument("--data-root", default=None)
    ap.add_argument(
        "--signing-key-file",
        default=None,
        help="operator-only Ed25519 private PEM; alternatively set "
        "FXSCALP_ARMING_SIGNING_KEY_FILE (missing key fails closed)",
    )
    ap.add_argument(
        "--hypotheses-json",
        default=None,
        help="legacy consistency check; attempt-ledger JSON remains required",
    )
    ap.add_argument(
        "--sealed-holdout-json",
        default=None,
        help="alternative pre-evaluation sealed holdout metadata",
    )
    ap.add_argument("--min-cell-trades", type=int, default=DEFAULT_MIN_CELL_TRADES)
    ap.add_argument(
        "--min-cell-independent-days",
        type=int,
        default=DEFAULT_MIN_CELL_INDEPENDENT_DAYS,
    )
    ap.add_argument(
        "--min-cell-win-rate",
        type=float,
        default=float(ARMING_POLICY["min_cell_win_rate"]),
        help="per-cell day-cluster lower-bound minimum (cannot be below 0.90)",
    )
    ap.add_argument(
        "--win-rate-family-confidence",
        type=float,
        default=DEFAULT_WIN_RATE_FAMILY_CONFIDENCE,
        help="simultaneous family-wise confidence for one-sided Wilson bounds",
    )
    ap.add_argument("--issue", action="store_true",
                    help="write the certificate on pass (default: verdict only)")
    args = ap.parse_args(argv)

    source_errors: list[str] = []
    trades: list[dict[str, Any]] = []
    artifact_sha256 = {name: "" for name in EVIDENCE_ARTIFACT_SHA256_FIELDS}
    if args.trades_json:
        artifact_sha256["trade_ledger"] = _file_sha256(args.trades_json)
        try:
            payload = json.loads(Path(args.trades_json).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = {}
            source_errors.append("trades_json_unreadable")
        if not isinstance(payload, dict) or not isinstance(payload.get("trades"), list):
            source_errors.append("trades_json_malformed")
        else:
            trades = list(payload["trades"])
    elif args.ledger_dir:
        trades = trades_from_ledger(Path(args.ledger_dir), errors=source_errors)
        artifact_sha256["trade_ledger"] = _ledger_bundle_sha256(args.ledger_dir)
    else:
        raise SystemExit("one of --trades-json / --ledger-dir is required")

    if args.venue_cost_json:
        venue_cost_provenance = _load_json_object(
            args.venue_cost_json,
            error="venue_cost_json_malformed",
            errors=source_errors,
        )
        artifact_sha256["venue_cost_artifact"] = _file_sha256(
            args.venue_cost_json
        )
    else:
        venue_cost_provenance = {}
        source_errors.append("venue_cost_json_required")
    if args.preregistration_json:
        preregistration = _load_json_object(
            args.preregistration_json,
            error="preregistration_json_malformed",
            errors=source_errors,
        )
        artifact_sha256["preregistration"] = _file_sha256(
            args.preregistration_json
        )
    else:
        preregistration = {}
        source_errors.append("preregistration_json_required")
    if args.attempt_ledger_json:
        attempt_ledger = _load_json_object(
            args.attempt_ledger_json,
            error="attempt_ledger_json_malformed",
            errors=source_errors,
        )
        artifact_sha256["attempt_ledger"] = _file_sha256(
            args.attempt_ledger_json
        )
    else:
        attempt_ledger = {}
        source_errors.append("attempt_ledger_json_required")

    attempted_hypotheses: list[str] | None = None
    sealed_holdout: dict[str, Any] | None = None
    raw_attempts = attempt_ledger.get("hypotheses")
    if isinstance(raw_attempts, list):
        attempted_hypotheses = [str(item) for item in raw_attempts]
    if args.hypotheses_json:
        try:
            hypothesis_payload = json.loads(
                Path(args.hypotheses_json).read_text(encoding="utf-8")
            )
            if isinstance(hypothesis_payload, dict):
                hypothesis_payload = hypothesis_payload.get("hypotheses")
            if not isinstance(hypothesis_payload, list):
                raise ValueError("hypotheses must be a list")
            legacy_hypotheses = [str(item) for item in hypothesis_payload]
            if (
                attempted_hypotheses is not None
                and attempted_hypotheses != legacy_hypotheses
            ):
                source_errors.append("hypotheses_json_attempt_ledger_mismatch")
            elif attempted_hypotheses is None:
                attempted_hypotheses = legacy_hypotheses
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            source_errors.append("hypotheses_json_malformed")
    if args.sealed_holdout_json:
        try:
            holdout_payload = json.loads(
                Path(args.sealed_holdout_json).read_text(encoding="utf-8")
            )
            if not isinstance(holdout_payload, dict):
                raise ValueError("sealed holdout must be an object")
            sealed_holdout = holdout_payload
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            source_errors.append("sealed_holdout_json_malformed")

    required_symbols = [
        s.strip().upper() for s in args.symbols.split(",") if s.strip()
    ]
    verdict = evaluate_family(
        trades=trades,
        trials=args.trials,
        venue=SUPPORTED_VENUE_ID,
        required_symbols=required_symbols,
        min_cell_trades=args.min_cell_trades,
        min_cell_independent_days=args.min_cell_independent_days,
        min_cell_win_rate=args.min_cell_win_rate,
        win_rate_family_confidence=args.win_rate_family_confidence,
        attempted_hypotheses=attempted_hypotheses,
        sealed_holdout=sealed_holdout,
        source_errors=source_errors,
        artifact_sha256=artifact_sha256,
        venue_cost_provenance=venue_cost_provenance,
        preregistration=preregistration,
        attempt_ledger=attempt_ledger,
    )
    print(json.dumps(_json_safe(verdict.to_dict()), indent=1, allow_nan=False))
    if args.issue:
        config = ScalpConfig()
        if args.data_root:
            config.data_root = str(Path(args.data_root))
        try:
            path = issue_arming_certificate(
                verdict=verdict,
                config=config,
                family=args.family,
                symbols=required_symbols,
                authority_signing_key_file=args.signing_key_file,
            )
        except (OSError, PermissionError, TypeError, ValueError) as exc:
            reason = str(exc) or "revalidation_failed"
            try:
                quarantined = revoke_arming_certificate(
                    config.data_root,
                    reason=reason,
                    authority_signing_key_file=args.signing_key_file,
                )
            except (OSError, PermissionError) as revoke_error:
                print(f"certificate REFUSED; REVOCATION FAILED: {revoke_error}")
                return 2
            detail = f"; quarantined={quarantined}" if quarantined else ""
            print(f"certificate REFUSED: {reason}{detail}")
            return 1
        print(f"certificate written: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
