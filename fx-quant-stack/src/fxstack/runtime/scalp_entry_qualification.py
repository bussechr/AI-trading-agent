# AGENT: ROLE: Pure evidence qualification for one production scalp entry candidate.
# AGENT: ENTRYPOINT: `qualify_scalp_entry_candidate`.
# AGENT: PRIMARY INPUTS: one unqualified proposal and authenticated validation output.
# AGENT: PRIMARY OUTPUTS: an evidence-qualified wrapper or ordered refusal reasons.
# AGENT: STATE / SIDE EFFECTS: none; never sizes, persists, authorizes, or commands.
"""Fail-closed evidence qualification for production scalp entry candidates.

The strategy proposal remains deliberately execution-unqualified.  A successful
result wraps that immutable proposal with the conservative probability and
payoff metrics authenticated by the production validation verifier.  This seam
does not apply portfolio, sizing, frequency-consumption, queue, or broker gates
and therefore cannot grant execution authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime.scalp_validation_evidence import (
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    SCALP_ADMISSION_MODE_SIGNED,
    ScalpValidationVerification,
)
from fxstack.schemas.entry import (
    ENTRY_PROPOSAL_QUALIFICATION,
    ENTRY_PROPOSAL_SCHEMA_VERSION,
    EntryProposal,
)
from fxstack.strategy.scalp_dislocation import (
    SCALP_DISLOCATION_STRATEGY_ID,
    SCALP_DISLOCATION_STRATEGY_VERSION,
    SCALP_EXECUTION_DEBIT_BPS,
    DislocationPolicy,
)


SCALP_ENTRY_QUALIFICATION_SCHEMA_VERSION = (
    "fxstack.runtime.scalp_entry_qualification.v1"
)
SCALP_DISLOCATION_CONFIG_SHA256 = DislocationPolicy().config_sha256()
P_STAR_ABSOLUTE_TOLERANCE = 1e-12
SIGNED_VALIDATION_PROBABILITY_SOURCE = "signed_validation_lower_bound"

_ENTRY_SIDES = ("BUY", "SELL")


@dataclass(frozen=True, slots=True)
class QualifiedScalpEntryCandidate:
    """Evidence-qualified metrics around an execution-unqualified proposal."""

    proposal: EntryProposal
    win_probability_lower_bound: float
    conservative_expected_edge_bps: float
    reward_risk_ratio: float
    probability_source: str = SIGNED_VALIDATION_PROBABILITY_SOURCE


@dataclass(frozen=True, slots=True)
class ScalpEntryQualificationResult:
    """Pure qualification result with no command or authority semantics."""

    proposal: EntryProposal
    qualified_candidate: QualifiedScalpEntryCandidate | None
    reasons: tuple[str, ...]
    schema_version: str = SCALP_ENTRY_QUALIFICATION_SCHEMA_VERSION

    @property
    def qualified(self) -> bool:
        return self.qualified_candidate is not None and not self.reasons

    @property
    def win_probability_lower_bound(self) -> float | None:
        candidate = self.qualified_candidate
        return None if candidate is None else candidate.win_probability_lower_bound

    @property
    def conservative_expected_edge_bps(self) -> float | None:
        candidate = self.qualified_candidate
        return None if candidate is None else candidate.conservative_expected_edge_bps

    @property
    def reward_risk_ratio(self) -> float | None:
        candidate = self.qualified_candidate
        return None if candidate is None else candidate.reward_risk_ratio


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _finite_positive(value: Any) -> float | None:
    numeric = _finite_number(value)
    return numeric if numeric is not None and numeric > 0.0 else None


def _proposal_reasons(
    proposal: EntryProposal,
    *,
    as_of_epoch: Any,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if proposal.schema_version != ENTRY_PROPOSAL_SCHEMA_VERSION:
        _append_reason(reasons, "scalp_qualification_proposal_schema_invalid")
    if proposal.allowed is not True:
        _append_reason(reasons, "scalp_qualification_proposal_not_allowed")
    if proposal.reasons != ():
        _append_reason(reasons, "scalp_qualification_proposal_reasons_present")
    if proposal.qualification != ENTRY_PROPOSAL_QUALIFICATION:
        _append_reason(reasons, "scalp_qualification_proposal_state_invalid")
    if proposal.execution_qualified is not False:
        _append_reason(reasons, "scalp_qualification_proposal_state_invalid")
    if proposal.win_probability is not None:
        _append_reason(reasons, "scalp_qualification_proposal_probability_present")
    if proposal.execution_type != "market":
        _append_reason(reasons, "scalp_qualification_execution_type_invalid")
    if proposal.pending_orders_forbidden is not True:
        _append_reason(reasons, "scalp_qualification_pending_orders_not_forbidden")
    deadline = (
        int(proposal.entry_deadline_epoch)
        if type(proposal.entry_deadline_epoch) is int
        and int(proposal.entry_deadline_epoch) > 0
        else None
    )
    if deadline is None:
        _append_reason(reasons, "scalp_qualification_entry_deadline_invalid")
    else:
        as_of = _finite_positive(as_of_epoch)
        if as_of is not None and deadline <= as_of:
            _append_reason(reasons, "scalp_qualification_entry_deadline_expired")

    if proposal.strategy_id != SCALP_DISLOCATION_STRATEGY_ID:
        _append_reason(reasons, "scalp_qualification_strategy_id_invalid")
    if proposal.strategy_version != SCALP_DISLOCATION_STRATEGY_VERSION:
        _append_reason(reasons, "scalp_qualification_strategy_version_invalid")
    if proposal.config_sha256 != SCALP_DISLOCATION_CONFIG_SHA256:
        _append_reason(reasons, "scalp_qualification_strategy_config_invalid")

    instrument = get_ig_mt4_instrument(proposal.symbol)
    if instrument is None or proposal.symbol != instrument.canonical_symbol:
        _append_reason(reasons, "scalp_qualification_symbol_invalid")
    else:
        if proposal.instrument_id != instrument.instrument_id:
            _append_reason(reasons, "scalp_qualification_instrument_id_invalid")
    if proposal.venue_id != IG_MT4_VENUE_ID:
        _append_reason(reasons, "scalp_qualification_venue_invalid")
    if proposal.side not in _ENTRY_SIDES:
        _append_reason(reasons, "scalp_qualification_side_invalid")

    numeric_values: dict[str, float | None] = {}
    for field_name in (
        "entry_price",
        "sl_price",
        "tp_price",
        "stop_bps",
        "target_bps",
        "spread_bps",
        "p_star",
    ):
        numeric = _finite_positive(getattr(proposal, field_name))
        numeric_values[field_name] = numeric
        if numeric is None:
            _append_reason(
                reasons,
                f"scalp_qualification_{field_name}_invalid",
            )

    entry = numeric_values["entry_price"]
    stop_price = numeric_values["sl_price"]
    target_price = numeric_values["tp_price"]
    if entry is not None and stop_price is not None and target_price is not None:
        bracket_valid = (
            proposal.side == "BUY" and stop_price < entry < target_price
        ) or (
            proposal.side == "SELL" and target_price < entry < stop_price
        )
        if not bracket_valid:
            _append_reason(reasons, "scalp_qualification_bracket_direction_invalid")

    stop_bps = numeric_values["stop_bps"]
    target_bps = numeric_values["target_bps"]
    spread_bps = numeric_values["spread_bps"]
    p_star = numeric_values["p_star"]
    if (
        stop_bps is not None
        and target_bps is not None
        and spread_bps is not None
        and p_star is not None
    ):
        recorded_cost_bps = spread_bps + SCALP_EXECUTION_DEBIT_BPS
        recomputed = (stop_bps + recorded_cost_bps) / (target_bps + stop_bps)
        if not math.isclose(
            p_star,
            recomputed,
            rel_tol=0.0,
            abs_tol=P_STAR_ABSOLUTE_TOLERANCE,
        ):
            _append_reason(reasons, "scalp_qualification_p_star_mismatch")
    return tuple(reasons)


def _bounds_reasons(
    raw_bounds: Any,
) -> tuple[tuple[str, ...], dict[str, dict[str, float]]]:
    reasons: list[str] = []
    normalized: dict[str, dict[str, float]] = {}
    if not isinstance(raw_bounds, Mapping):
        return ("scalp_qualification_probability_scope_invalid",), normalized
    if set(raw_bounds) != set(IG_MT4_SCALP_SYMBOLS):
        _append_reason(reasons, "scalp_qualification_probability_scope_invalid")

    for symbol in IG_MT4_SCALP_SYMBOLS:
        raw_sides = raw_bounds.get(symbol)
        if not isinstance(raw_sides, Mapping) or set(raw_sides) != set(_ENTRY_SIDES):
            _append_reason(
                reasons,
                f"scalp_qualification_probability_side_scope_invalid:{symbol}",
            )
            continue
        normalized[symbol] = {}
        for side in _ENTRY_SIDES:
            lower_bound = _finite_number(raw_sides.get(side))
            if lower_bound is None or not 0.0 <= lower_bound <= 1.0:
                _append_reason(
                    reasons,
                    f"scalp_qualification_probability_lower_bound_invalid:"
                    f"{symbol}:{side}",
                )
                continue
            normalized[symbol][side] = lower_bound
    return tuple(reasons), normalized


def _verification_reasons(
    verification: ScalpValidationVerification,
    *,
    proposal: EntryProposal,
    as_of_epoch: Any,
) -> tuple[tuple[str, ...], dict[str, dict[str, float]]]:
    reasons: list[str] = []
    admission_mode = str(
        verification.admission_mode or SCALP_ADMISSION_MODE_SIGNED
    ).strip().lower()
    if admission_mode != SCALP_ADMISSION_MODE_SIGNED:
        _append_reason(reasons, "scalp_qualification_admission_mode_invalid")
    if verification.valid is not True:
        _append_reason(reasons, "scalp_qualification_validation_invalid")
    if verification.authenticated is not True:
        _append_reason(reasons, "scalp_qualification_validation_unauthenticated")
    if verification.revocation_verified is not True:
        _append_reason(reasons, "scalp_qualification_revocation_unverified")
    if verification.reason != "" or verification.errors != ():
        _append_reason(reasons, "scalp_qualification_validation_state_invalid")
    if verification.win_probability_bounds_reason != "":
        _append_reason(reasons, "scalp_qualification_validation_state_invalid")

    if (
        verification.strategy_id != SCALP_DISLOCATION_STRATEGY_ID
        or verification.strategy_id != proposal.strategy_id
    ):
        _append_reason(reasons, "scalp_qualification_validation_strategy_id_invalid")
    if (
        verification.strategy_version != SCALP_DISLOCATION_STRATEGY_VERSION
        or verification.strategy_version != proposal.strategy_version
    ):
        _append_reason(
            reasons,
            "scalp_qualification_validation_strategy_version_invalid",
        )
    if (
        verification.config_sha256 != SCALP_DISLOCATION_CONFIG_SHA256
        or verification.config_sha256 != proposal.config_sha256
    ):
        _append_reason(reasons, "scalp_qualification_validation_config_invalid")
    if verification.venue_id != IG_MT4_VENUE_ID:
        _append_reason(reasons, "scalp_qualification_validation_venue_invalid")
    if verification.symbol_scope != IG_MT4_SCALP_SYMBOLS:
        _append_reason(reasons, "scalp_qualification_validation_scope_invalid")
    if (
        isinstance(verification.max_entries_per_symbol_utc_day, bool)
        or verification.max_entries_per_symbol_utc_day
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        _append_reason(reasons, "scalp_qualification_validation_daily_cap_invalid")

    as_of = _finite_positive(as_of_epoch)
    issued_at = _finite_positive(verification.issued_at_epoch)
    expires_at = _finite_positive(verification.expires_at_epoch)
    if as_of is None:
        _append_reason(reasons, "scalp_qualification_clock_invalid")
    if (
        issued_at is None
        or expires_at is None
        or expires_at <= issued_at
        or (as_of is not None and issued_at > as_of)
    ):
        _append_reason(reasons, "scalp_qualification_validation_time_window_invalid")
    elif as_of is not None and expires_at <= as_of:
        _append_reason(reasons, "scalp_qualification_validation_expired")

    bounds_reasons, normalized_bounds = _bounds_reasons(
        verification.win_probability_lower_bounds
    )
    for reason in bounds_reasons:
        _append_reason(reasons, reason)
    return tuple(reasons), normalized_bounds


def _invalid_result(
    proposal: EntryProposal,
    reasons: tuple[str, ...],
) -> ScalpEntryQualificationResult:
    return ScalpEntryQualificationResult(
        proposal=proposal,
        qualified_candidate=None,
        reasons=reasons,
    )


def qualify_scalp_entry_candidate(
    proposal: EntryProposal,
    verification: ScalpValidationVerification,
    *,
    as_of_epoch: float,
) -> ScalpEntryQualificationResult:
    """Qualify one mathematical candidate against signed cell evidence.

    Every invalid outcome omits the probability, edge, payoff, and qualified
    wrapper. The original proposal is returned unchanged for diagnostics.
    """

    reasons = list(_proposal_reasons(proposal, as_of_epoch=as_of_epoch))
    verification_reasons, bounds = _verification_reasons(
        verification,
        proposal=proposal,
        as_of_epoch=as_of_epoch,
    )
    for reason in verification_reasons:
        _append_reason(reasons, reason)
    if reasons:
        return _invalid_result(proposal, tuple(reasons))

    side = proposal.side
    if side not in _ENTRY_SIDES:  # Defensive narrowing after validation.
        return _invalid_result(
            proposal,
            ("scalp_qualification_side_invalid",),
        )
    if (
        proposal.stop_bps is None
        or proposal.target_bps is None
        or proposal.spread_bps is None
        or proposal.p_star is None
    ):  # Defensive narrowing after finite-positive validation.
        return _invalid_result(
            proposal,
            ("scalp_qualification_payoff_metrics_missing",),
        )
    lower_bound = bounds[proposal.symbol][side]
    stop_bps = float(proposal.stop_bps)
    target_bps = float(proposal.target_bps)
    spread_bps = float(proposal.spread_bps)
    p_star = float(proposal.p_star)

    if lower_bound <= p_star:
        return _invalid_result(
            proposal,
            ("scalp_qualification_probability_not_above_p_star",),
        )
    recorded_cost_bps = spread_bps + SCALP_EXECUTION_DEBIT_BPS
    expected_edge_bps = (
        lower_bound * target_bps
        - (1.0 - lower_bound) * stop_bps
        - recorded_cost_bps
    )
    reward_risk_ratio = target_bps / stop_bps
    if not math.isfinite(expected_edge_bps) or expected_edge_bps <= 0.0:
        return _invalid_result(
            proposal,
            ("scalp_qualification_expected_edge_nonpositive",),
        )
    if not math.isfinite(reward_risk_ratio) or reward_risk_ratio <= 0.0:
        return _invalid_result(
            proposal,
            ("scalp_qualification_reward_risk_invalid",),
        )

    candidate = QualifiedScalpEntryCandidate(
        proposal=proposal,
        win_probability_lower_bound=lower_bound,
        conservative_expected_edge_bps=expected_edge_bps,
        reward_risk_ratio=reward_risk_ratio,
        probability_source=SIGNED_VALIDATION_PROBABILITY_SOURCE,
    )
    return ScalpEntryQualificationResult(
        proposal=proposal,
        qualified_candidate=candidate,
        reasons=(),
    )


__all__ = [
    "SIGNED_VALIDATION_PROBABILITY_SOURCE",
    "P_STAR_ABSOLUTE_TOLERANCE",
    "SCALP_DISLOCATION_CONFIG_SHA256",
    "SCALP_ENTRY_QUALIFICATION_SCHEMA_VERSION",
    "QualifiedScalpEntryCandidate",
    "ScalpEntryQualificationResult",
    "qualify_scalp_entry_candidate",
]
