# AGENT: ROLE: Pure signed-evidence qualification for one MTVCLC runtime candidate.
# AGENT: ENTRYPOINT: `qualify_mtvclc_entry_candidate`.
# AGENT: PRIMARY INPUTS: authority-free MTVCLC candidate, signed-release result, admitted cost row, clock.
# AGENT: PRIMARY OUTPUTS: immutable evidence-qualified candidate or ordered refusal reasons.
# AGENT: STATE / SIDE EFFECTS: none; no I/O, sizing, persistence, queue, broker, or signing access.
"""Qualify one MTVCLC candidate against a signed runtime release.

The strategy evaluator intentionally emits an authority-free candidate.  This
module is the first pure production seam that can attach the authenticated
4,874-family Wilson lower bound for the exact symbol/side cell.  It still does
not size, persist, queue, activate, or execute a trade.

The release input is structural on purpose.  The public release verifier may
live in a separately reviewed module, while this seam requires its normalized
result to expose the attributes described by
``MTVCLCSignedReleaseVerification``.  A mapping carrying the same fields is
also accepted to keep tests and staged integration independent of import
order.  No local fallback probability exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
import math
from typing import Any, Protocol

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime.market_source_identity import MARKET_SOURCE_SCHEMA
from fxstack.strategy.mtvclc import (
    FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
    MAX_ENTRIES_PER_SYMBOL_UTC_DAY,
    MAX_ENTRY_DELAY_SECONDS,
    MAX_QUOTE_GAP_SECONDS,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_PROPOSAL_SCHEMA_VERSION,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    MTVCLC_V1_SYMBOLS,
    STOP_COST_MULTIPLE,
    TARGET_COST_MULTIPLE,
    TIME_STOP_M1_BARS,
    MTVCLCCostCalibration,
    MTVCLCTradeCandidate,
)


MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION = (
    "fxstack.runtime.mtvclc_entry_qualification.v1"
)
MTVCLC_SIGNED_PROBABILITY_SOURCE = (
    "mtvclc_signed_evidence_v3_wilson_lower_4874"
)
MTVCLC_SIGNED_ADMISSION_MODE = "signed_validation"
MTVCLC_REQUIRED_ACCOUNT_MODE = "demo"
MTVCLC_REQUIRED_AUTHORITY_PURPOSE = (
    "mtvclc_ig_demo_runtime_release_eligibility.v1"
)
MTVCLC_RUNTIME_NATIVE_AUTHORITY_PURPOSE = "mtvclc_runtime_native_eligibility.v1"
_EXPECTED_RELEASE_AUTHORITY: dict[str, Any] = {
    "activation_authorized": True,
    "runtime_authorized": True,
    "entry_lane_authorized": True,
    "individual_trade_authorized": False,
    "registry_write_authorized": False,
    "broker_access_authorized": False,
    "broker_trade_authorized": False,
    "research_access_authorized": False,
    "real_account_authorized": False,
}

_SIDES = ("BUY", "SELL")
_ABS_TOLERANCE = 1e-12


class MTVCLCSignedReleaseVerification(Protocol):
    """Normalized public-verifier result consumed by qualification."""

    valid: bool
    reason: str
    errors: Sequence[str]
    authenticated: bool
    revocation_verified: bool
    admission_mode: str
    certificate_sha256: str
    runtime_release_certificate_sha256: str
    evidence_sha256: str
    signing_key_id: str
    runtime_release_signing_key_id: str
    generation_id: str
    strategy_id: str
    strategy_version: str
    config_id: str
    config_sha256: str
    venue_id: str
    account_mode: str
    scope_version: str
    symbol_scope: Sequence[str]
    max_entries_per_symbol_utc_day: int
    maximum_account_currency_risk_per_trade: float
    issued_at_epoch: float
    expires_at_epoch: float
    authority_purpose: str
    authority: Mapping[str, Any]
    qualification_surface_sha256: str
    win_probability_lower_bounds: Mapping[str, Mapping[str, float]]
    base_break_even_probabilities: Mapping[str, Mapping[str, float]]
    evidence_cell_sha256: Mapping[str, Mapping[str, str]]
    evidence_cost_row_sha256: Mapping[str, str]
    cost_mapping_sha256: str
    cost_rows_sha256: str
    cost_calibration_id: str
    cost_calibration_source_sha256: str
    cost_calibration_source_sha256_by_symbol: Mapping[str, str]
    cost_calibrations: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class QualifiedMTVCLCEntryCandidate:
    """An evidence-qualified candidate that still has no execution authority."""

    proposal: MTVCLCTradeCandidate
    admitted_cost: MTVCLCCostCalibration
    win_probability_lower_bound: float
    conservative_expected_edge_bps: float
    reward_risk_ratio: float
    release_generation_id: str
    release_certificate_sha256: str
    release_signing_key_id: str
    evidence_sha256: str
    evidence_cell_sha256: str
    evidence_cost_row_sha256: str
    qualification_surface_sha256: str
    release_expires_at_epoch: float
    probability_source: str = MTVCLC_SIGNED_PROBABILITY_SOURCE
    schema_version: str = MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION

    @property
    def symbol(self) -> str:
        return self.proposal.symbol

    @property
    def side(self) -> str:
        return str(self.proposal.side or "")

    @property
    def entry_deadline_epoch(self) -> int:
        return int(self.proposal.entry_deadline_epoch or 0)

    @property
    def execution_type(self) -> str:
        return str(self.proposal.execution_type)

    @property
    def pending_orders_forbidden(self) -> bool:
        return bool(self.proposal.pending_orders_forbidden)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MTVCLCEntryQualificationResult:
    """Pure qualification result; refusal omits all probability/payoff output."""

    proposal: MTVCLCTradeCandidate
    qualified_candidate: QualifiedMTVCLCEntryCandidate | None
    reasons: tuple[str, ...]
    schema_version: str = MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION

    @property
    def qualified(self) -> bool:
        return self.qualified_candidate is not None and not self.reasons

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _finite(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _finite(value)
    return number if number is not None and number > 0.0 else None


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _same_number(left: Any, right: Any) -> bool:
    left_number = _finite(left)
    right_number = _finite(right)
    return bool(
        left_number is not None
        and right_number is not None
        and math.isclose(
            left_number,
            right_number,
            rel_tol=0.0,
            abs_tol=_ABS_TOLERANCE,
        )
    )


def _release_value(
    verification: MTVCLCSignedReleaseVerification | Mapping[str, Any],
    name: str,
    *aliases: str,
) -> Any:
    names = (name, *aliases)
    if isinstance(verification, Mapping):
        for field_name in names:
            if field_name in verification:
                return verification.get(field_name)
        return None
    for field_name in names:
        if hasattr(verification, field_name):
            return getattr(verification, field_name)
    return None


def _release_nonempty_value(
    verification: MTVCLCSignedReleaseVerification | Mapping[str, Any],
    *names: str,
) -> Any:
    for name in names:
        value = _release_value(verification, name)
        if str(value or "").strip():
            return value
    return None


def _normalized_errors(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    return tuple(str(item) for item in value)


def _normalized_scope(value: Any) -> tuple[str, ...] | None:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    return tuple(str(item or "").strip().upper() for item in value)


def _normalized_bounds(
    value: Any,
) -> tuple[tuple[str, ...], dict[str, dict[str, float]]]:
    reasons: list[str] = []
    normalized: dict[str, dict[str, float]] = {}
    if not isinstance(value, Mapping) or set(value) != set(MTVCLC_V1_SYMBOLS):
        return ("mtvclc_qualification_probability_scope_invalid",), normalized
    for symbol in MTVCLC_V1_SYMBOLS:
        raw_sides = value.get(symbol)
        if not isinstance(raw_sides, Mapping) or set(raw_sides) != set(_SIDES):
            _append_reason(
                reasons,
                f"mtvclc_qualification_probability_side_scope_invalid:{symbol}",
            )
            continue
        normalized[symbol] = {}
        for side in _SIDES:
            lower = _finite(raw_sides.get(side))
            if lower is None or not 0.0 <= lower <= 1.0:
                _append_reason(
                    reasons,
                    "mtvclc_qualification_probability_lower_bound_invalid:"
                    f"{symbol}:{side}",
                )
                continue
            normalized[symbol][side] = lower
    return tuple(reasons), normalized


def _normalized_cell_hashes(
    value: Any,
) -> tuple[tuple[str, ...], dict[str, dict[str, str]]]:
    reasons: list[str] = []
    normalized: dict[str, dict[str, str]] = {}
    if not isinstance(value, Mapping) or set(value) != set(MTVCLC_V1_SYMBOLS):
        return ("mtvclc_qualification_cell_hash_scope_invalid",), normalized
    for symbol in MTVCLC_V1_SYMBOLS:
        raw_sides = value.get(symbol)
        if not isinstance(raw_sides, Mapping) or set(raw_sides) != set(_SIDES):
            _append_reason(
                reasons,
                f"mtvclc_qualification_cell_hash_side_scope_invalid:{symbol}",
            )
            continue
        normalized[symbol] = {}
        for side in _SIDES:
            digest = str(raw_sides.get(side) or "").strip().lower()
            if not _is_sha256(digest):
                _append_reason(
                    reasons,
                    f"mtvclc_qualification_cell_hash_invalid:{symbol}:{side}",
                )
                continue
            normalized[symbol][side] = digest
    return tuple(reasons), normalized


def _normalized_symbol_hashes(
    value: Any,
    *,
    reason_prefix: str,
) -> tuple[tuple[str, ...], dict[str, str]]:
    normalized: dict[str, str] = {}
    if not isinstance(value, Mapping) or set(value) != set(MTVCLC_V1_SYMBOLS):
        return (f"{reason_prefix}_scope_invalid",), normalized
    reasons: list[str] = []
    for symbol in MTVCLC_V1_SYMBOLS:
        digest = str(value.get(symbol) or "").strip().lower()
        if not _is_sha256(digest):
            _append_reason(reasons, f"{reason_prefix}_invalid:{symbol}")
            continue
        normalized[symbol] = digest
    return tuple(reasons), normalized


def _verification_reasons(
    verification: MTVCLCSignedReleaseVerification | Mapping[str, Any],
    *,
    as_of_epoch: Any,
) -> tuple[
    tuple[str, ...],
    dict[str, dict[str, float]],
    dict[str, dict[str, str]],
    dict[str, dict[str, float]],
]:
    reasons: list[str] = []
    if _release_value(verification, "valid") is not True:
        _append_reason(reasons, "mtvclc_qualification_release_invalid")
    if _release_value(verification, "authenticated") is not True:
        _append_reason(reasons, "mtvclc_qualification_release_unauthenticated")
    if _release_value(verification, "revocation_verified") is not True:
        _append_reason(reasons, "mtvclc_qualification_revocation_unverified")
    if (
        str(_release_value(verification, "admission_mode") or "").strip()
        != MTVCLC_SIGNED_ADMISSION_MODE
    ):
        _append_reason(reasons, "mtvclc_qualification_admission_mode_invalid")
    if str(_release_value(verification, "reason") or ""):
        _append_reason(reasons, "mtvclc_qualification_release_state_invalid")
    errors = _normalized_errors(_release_value(verification, "errors"))
    if errors is None or errors:
        _append_reason(reasons, "mtvclc_qualification_release_state_invalid")
    account_mode = str(_release_value(verification, "account_mode") or "").strip().lower()
    expected_authority = {
        **_EXPECTED_RELEASE_AUTHORITY,
        "real_account_authorized": account_mode == "real",
    }
    authority = _release_value(verification, "authority")
    if not isinstance(authority, Mapping) or dict(authority) != expected_authority:
        _append_reason(reasons, "mtvclc_qualification_release_authority_invalid")
    authority_purpose = str(
        _release_value(verification, "authority_purpose") or ""
    ).strip()
    allowed_authority_purposes = {
        MTVCLC_RUNTIME_NATIVE_AUTHORITY_PURPOSE,
        *(
            (MTVCLC_REQUIRED_AUTHORITY_PURPOSE,)
            if account_mode == "demo"
            else ()
        ),
    }
    if authority_purpose not in allowed_authority_purposes:
        _append_reason(
            reasons,
            "mtvclc_qualification_release_authority_purpose_invalid",
        )

    certificate_sha = _release_nonempty_value(
        verification,
        "certificate_sha256",
        "runtime_release_certificate_sha256",
    )
    signing_key_id = _release_nonempty_value(
        verification,
        "signing_key_id",
        "runtime_release_signing_key_id",
    )
    evidence_sha = _release_value(verification, "evidence_sha256")
    surface_sha = _release_value(verification, "qualification_surface_sha256")
    if not _is_sha256(certificate_sha):
        _append_reason(reasons, "mtvclc_qualification_release_certificate_invalid")
    if not _is_sha256(signing_key_id):
        _append_reason(reasons, "mtvclc_qualification_release_key_invalid")
    if not _is_sha256(evidence_sha):
        _append_reason(reasons, "mtvclc_qualification_evidence_identity_invalid")
    if not _is_sha256(surface_sha):
        _append_reason(reasons, "mtvclc_qualification_surface_identity_invalid")
    if not str(_release_value(verification, "generation_id") or "").strip():
        _append_reason(reasons, "mtvclc_qualification_release_generation_invalid")

    expected_identity = {
        "strategy_id": MTVCLC_STRATEGY_ID,
        "strategy_version": MTVCLC_STRATEGY_VERSION,
        "config_id": MTVCLC_CONFIG_ID,
        "config_sha256": MTVCLC_CONFIG_SHA256,
        "venue_id": IG_MT4_VENUE_ID,
        "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
    }
    for field_name, expected in expected_identity.items():
        observed = str(_release_value(verification, field_name) or "").strip()
        if observed.lower() != str(expected).lower():
            _append_reason(
                reasons,
                f"mtvclc_qualification_release_{field_name}_invalid",
            )
    if account_mode not in {"demo", "real"}:
        _append_reason(reasons, "mtvclc_qualification_release_account_mode_invalid")
    scope = _normalized_scope(_release_value(verification, "symbol_scope"))
    if scope != tuple(MTVCLC_V1_SYMBOLS):
        _append_reason(reasons, "mtvclc_qualification_release_scope_invalid")
    if (
        type(_release_value(verification, "max_entries_per_symbol_utc_day"))
        is not int
        or int(_release_value(verification, "max_entries_per_symbol_utc_day"))
        != MAX_ENTRIES_PER_SYMBOL_UTC_DAY
    ):
        _append_reason(reasons, "mtvclc_qualification_release_daily_cap_invalid")
    if not _same_number(
        _release_value(
            verification,
            "maximum_account_currency_risk_per_trade",
        ),
        1.0,
    ):
        _append_reason(reasons, "mtvclc_qualification_release_risk_cap_invalid")

    now = _positive(as_of_epoch)
    issued_at = _positive(_release_value(verification, "issued_at_epoch"))
    expires_at = _positive(_release_value(verification, "expires_at_epoch"))
    if now is None:
        _append_reason(reasons, "mtvclc_qualification_clock_invalid")
    if (
        issued_at is None
        or expires_at is None
        or expires_at <= issued_at
        or (now is not None and issued_at > now)
    ):
        _append_reason(reasons, "mtvclc_qualification_release_time_window_invalid")
    elif now is not None and expires_at <= now:
        _append_reason(reasons, "mtvclc_qualification_release_expired")

    bounds_reasons, bounds = _normalized_bounds(
        _release_value(verification, "win_probability_lower_bounds")
    )
    for reason in bounds_reasons:
        _append_reason(reasons, reason)
    cell_hash_reasons, cell_hashes = _normalized_cell_hashes(
        _release_value(verification, "evidence_cell_sha256")
    )
    for reason in cell_hash_reasons:
        _append_reason(reasons, reason)
    base_reasons, base_break_even = _normalized_bounds(
        _release_value(verification, "base_break_even_probabilities")
    )
    for reason in base_reasons:
        _append_reason(
            reasons,
            reason.replace(
                "mtvclc_qualification_probability_",
                "mtvclc_qualification_base_break_even_",
            ),
        )
    if str(
        _release_value(verification, "win_probability_bounds_reason") or ""
    ):
        _append_reason(reasons, "mtvclc_qualification_probability_state_invalid")
    return tuple(reasons), bounds, cell_hashes, base_break_even


def _cost_reasons(
    cost: Any,
    *,
    symbol: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not isinstance(cost, MTVCLCCostCalibration):
        return ("mtvclc_qualification_cost_not_typed",)
    if cost.symbol != symbol:
        _append_reason(reasons, "mtvclc_qualification_cost_symbol_invalid")
    if not str(cost.calibration_id or "").strip():
        _append_reason(reasons, "mtvclc_qualification_cost_calibration_id_invalid")
    if not _is_sha256(cost.source_sha256):
        _append_reason(reasons, "mtvclc_qualification_cost_source_invalid")
    numeric = (
        _positive(cost.p90_spread_bps),
        _finite(cost.commission_bps_per_round_trip),
        _finite(cost.financing_bps_per_trade),
        _finite(cost.adverse_execution_debit_bps),
        _finite(cost.convert_on_close_charge_fraction),
    )
    if (
        numeric[0] is None
        or any(value is None or value < 0.0 for value in numeric[1:])
        or numeric[4] is None
        or numeric[4] >= 1.0
        or not _same_number(
            cost.adverse_execution_debit_bps,
            FIXED_ADVERSE_EXECUTION_DEBIT_BPS,
        )
    ):
        _append_reason(reasons, "mtvclc_qualification_cost_values_invalid")
    if (
        cost.account_currency != "USD"
        or len(str(cost.pnl_currency or "")) != 3
        or not str(cost.pnl_currency).isalpha()
        or not str(cost.pnl_currency).isupper()
    ):
        _append_reason(reasons, "mtvclc_qualification_cost_currency_invalid")
    instrument = get_ig_mt4_instrument(symbol)
    if instrument is not None and cost.pnl_currency != instrument.quote_ccy:
        _append_reason(reasons, "mtvclc_qualification_cost_pnl_currency_invalid")
    try:
        row_sha = cost.row_sha256()
        recorded_cost = cost.recorded_cost_bps
        p_star = cost.break_even_win_probability
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        row_sha = ""
        recorded_cost = 0.0
        p_star = 0.0
    if not _is_sha256(row_sha):
        _append_reason(reasons, "mtvclc_qualification_cost_row_invalid")
    if not math.isfinite(recorded_cost) or recorded_cost <= 0.0:
        _append_reason(reasons, "mtvclc_qualification_recorded_cost_invalid")
    if not math.isfinite(p_star) or not 0.0 < p_star < 1.0:
        _append_reason(reasons, "mtvclc_qualification_cost_p_star_invalid")
    return tuple(reasons)


def _release_cost_reasons(
    verification: MTVCLCSignedReleaseVerification | Mapping[str, Any],
    *,
    admitted_cost: MTVCLCCostCalibration,
) -> tuple[tuple[str, ...], str]:
    reasons: list[str] = []
    release_calibration_id = str(
        _release_value(verification, "cost_calibration_id") or ""
    )
    release_source_sha = str(
        _release_value(verification, "cost_calibration_source_sha256") or ""
    ).lower()
    if release_calibration_id != admitted_cost.calibration_id:
        _append_reason(reasons, "mtvclc_qualification_release_cost_id_invalid")
    if not _is_sha256(release_source_sha):
        _append_reason(
            reasons,
            "mtvclc_qualification_release_cost_rows_identity_invalid",
        )
    elif release_source_sha != str(
        _release_value(verification, "cost_rows_sha256") or ""
    ).lower():
        _append_reason(
            reasons,
            "mtvclc_qualification_release_cost_rows_identity_mismatch",
        )
    for field_name in ("cost_mapping_sha256", "cost_rows_sha256"):
        if not _is_sha256(_release_value(verification, field_name)):
            _append_reason(
                reasons,
                f"mtvclc_qualification_release_{field_name}_invalid",
            )
    source_reasons, source_by_symbol = _normalized_symbol_hashes(
        _release_value(
            verification,
            "cost_calibration_source_sha256_by_symbol",
        ),
        reason_prefix="mtvclc_qualification_release_cost_source",
    )
    for reason in source_reasons:
        _append_reason(reasons, reason)
    if source_by_symbol.get(admitted_cost.symbol) != admitted_cost.source_sha256:
        _append_reason(reasons, "mtvclc_qualification_release_cost_source_invalid")
    evidence_row_reasons, evidence_rows = _normalized_symbol_hashes(
        _release_value(verification, "evidence_cost_row_sha256"),
        reason_prefix="mtvclc_qualification_release_evidence_cost_row",
    )
    for reason in evidence_row_reasons:
        _append_reason(reasons, reason)

    raw_calibrations = _release_value(verification, "cost_calibrations")
    if not isinstance(raw_calibrations, Mapping) or set(raw_calibrations) != set(
        MTVCLC_V1_SYMBOLS
    ):
        return (
            tuple(
                [
                    *reasons,
                    "mtvclc_qualification_release_cost_scope_invalid",
                ]
            ),
            "",
        )
    raw_row = raw_calibrations.get(admitted_cost.symbol)
    if not isinstance(raw_row, Mapping):
        return (
            tuple(
                [
                    *reasons,
                    "mtvclc_qualification_release_cost_row_missing",
                ]
            ),
            "",
        )

    expected_text = {
        "symbol": admitted_cost.symbol,
        "calibration_id": admitted_cost.calibration_id,
        "source_sha256": admitted_cost.source_sha256,
        "account_currency": admitted_cost.account_currency,
        "pnl_currency": admitted_cost.pnl_currency,
    }
    for field_name, expected in expected_text.items():
        if str(raw_row.get(field_name) or "") != expected:
            _append_reason(
                reasons,
                f"mtvclc_qualification_release_cost_{field_name}_invalid",
            )
    expected_numbers = {
        "p90_spread_bps": admitted_cost.p90_spread_bps,
        "commission_bps_per_round_trip": (
            admitted_cost.commission_bps_per_round_trip
        ),
        "financing_bps_per_trade": admitted_cost.financing_bps_per_trade,
        "convert_on_close_charge_fraction": (
            admitted_cost.convert_on_close_charge_fraction
        ),
        "adverse_execution_debit_bps": (
            admitted_cost.adverse_execution_debit_bps
        ),
    }
    for field_name, expected in expected_numbers.items():
        if not _same_number(raw_row.get(field_name), expected):
            _append_reason(
                reasons,
                f"mtvclc_qualification_release_cost_{field_name}_invalid",
            )
    runtime_row_sha = str(
        raw_row.get("runtime_calibration_row_sha256") or ""
    ).lower()
    evidence_row_sha = str(raw_row.get("evidence_cost_row_sha256") or "").lower()
    if runtime_row_sha != admitted_cost.row_sha256():
        _append_reason(reasons, "mtvclc_qualification_release_cost_row_invalid")
    if not _is_sha256(evidence_row_sha):
        _append_reason(
            reasons,
            "mtvclc_qualification_release_evidence_cost_row_invalid",
        )
    elif evidence_rows.get(admitted_cost.symbol) != evidence_row_sha:
        _append_reason(
            reasons,
            "mtvclc_qualification_release_evidence_cost_row_mismatch",
        )
    return tuple(reasons), evidence_row_sha


def _candidate_reasons(
    proposal: MTVCLCTradeCandidate,
    *,
    admitted_cost: MTVCLCCostCalibration,
    as_of_epoch: Any,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not isinstance(proposal, MTVCLCTradeCandidate):
        return ("mtvclc_qualification_candidate_not_typed",)
    if proposal.schema_version != MTVCLC_PROPOSAL_SCHEMA_VERSION:
        _append_reason(reasons, "mtvclc_qualification_candidate_schema_invalid")
    if proposal.allowed is not True or proposal.reasons != ():
        _append_reason(reasons, "mtvclc_qualification_candidate_not_allowed")
    if proposal.qualification != "candidate_unqualified":
        _append_reason(reasons, "mtvclc_qualification_candidate_state_invalid")
    authority_bits = (
        proposal.evidence_qualified,
        proposal.release_authorized,
        proposal.activation_authorized,
        proposal.risk_qualified,
        proposal.sizing_authorized,
        proposal.queue_authorized,
        proposal.broker_trade_authorized,
        proposal.execution_qualified,
    )
    if any(value is not False for value in authority_bits):
        _append_reason(reasons, "mtvclc_qualification_candidate_authority_invalid")
    if proposal.win_probability is not None:
        _append_reason(reasons, "mtvclc_qualification_candidate_probability_present")
    if (
        proposal.execution_type != "market"
        or proposal.immediate_market_trade is not True
        or proposal.pending_orders_forbidden is not True
    ):
        _append_reason(reasons, "mtvclc_qualification_immediate_market_invalid")

    expected_identity = (
        proposal.strategy_id == MTVCLC_STRATEGY_ID
        and proposal.strategy_version == MTVCLC_STRATEGY_VERSION
        and proposal.config_id == MTVCLC_CONFIG_ID
        and proposal.config_sha256 == MTVCLC_CONFIG_SHA256
        and proposal.scope_version == IG_MT4_SCALP_SCOPE_VERSION
        and proposal.venue_id == IG_MT4_VENUE_ID
        and proposal.symbol in MTVCLC_V1_SYMBOLS
    )
    instrument = get_ig_mt4_instrument(proposal.symbol)
    if (
        not expected_identity
        or instrument is None
        or proposal.instrument_id != instrument.instrument_id
    ):
        _append_reason(reasons, "mtvclc_qualification_candidate_identity_invalid")
    if proposal.side not in _SIDES:
        _append_reason(reasons, "mtvclc_qualification_candidate_side_invalid")
    if (
        proposal.bar_source_id != proposal.quote_source_id
        or not _is_sha256(proposal.bar_source_id)
        or proposal.bar_source_version != MARKET_SOURCE_SCHEMA
        or proposal.quote_source_version != MARKET_SOURCE_SCHEMA
        or not _is_sha256(proposal.market_source_identity_sha256)
    ):
        _append_reason(reasons, "mtvclc_qualification_market_source_invalid")

    signal_epoch = (
        proposal.signal_epoch if type(proposal.signal_epoch) is int else None
    )
    expected_entry = (
        proposal.expected_entry_epoch
        if type(proposal.expected_entry_epoch) is int
        else None
    )
    deadline = (
        proposal.entry_deadline_epoch
        if type(proposal.entry_deadline_epoch) is int
        else None
    )
    entry_epoch = proposal.entry_epoch if type(proposal.entry_epoch) is int else None
    now = _positive(as_of_epoch)
    if (
        signal_epoch is None
        or signal_epoch <= 0
        or signal_epoch % 60
        or expected_entry is None
        or not signal_epoch + 60 <= expected_entry < signal_epoch + 120
        or deadline != expected_entry + MAX_ENTRY_DELAY_SECONDS
        or entry_epoch is None
        or entry_epoch != expected_entry
    ):
        _append_reason(reasons, "mtvclc_qualification_entry_timing_invalid")
    if now is None:
        _append_reason(reasons, "mtvclc_qualification_clock_invalid")
    elif deadline is None or deadline <= now:
        _append_reason(reasons, "mtvclc_qualification_entry_deadline_expired")

    bid = _positive(proposal.entry_bid)
    ask = _positive(proposal.entry_ask)
    entry = _positive(proposal.entry_price)
    stop = _positive(proposal.stop_price)
    target = _positive(proposal.target_price)
    if bid is None or ask is None or entry is None or ask < bid:
        _append_reason(reasons, "mtvclc_qualification_entry_quote_invalid")
    elif (
        (proposal.side == "BUY" and entry != ask)
        or (proposal.side == "SELL" and entry != bid)
    ):
        _append_reason(reasons, "mtvclc_qualification_entry_price_basis_invalid")
    if stop is None or target is None or entry is None:
        _append_reason(reasons, "mtvclc_qualification_bracket_invalid")
    elif not (
        (proposal.side == "BUY" and stop < entry < target)
        or (proposal.side == "SELL" and target < entry < stop)
    ):
        _append_reason(reasons, "mtvclc_qualification_bracket_direction_invalid")

    cost_reasons = _cost_reasons(admitted_cost, symbol=proposal.symbol)
    for reason in cost_reasons:
        _append_reason(reasons, reason)
    if isinstance(admitted_cost, MTVCLCCostCalibration):
        try:
            admitted_row_sha = admitted_cost.row_sha256()
            admitted_recorded_cost = admitted_cost.recorded_cost_bps
            admitted_p_star = admitted_cost.break_even_win_probability
        except (TypeError, ValueError, OverflowError, ZeroDivisionError):
            admitted_row_sha = ""
            admitted_recorded_cost = 0.0
            admitted_p_star = 0.0
        if (
            proposal.cost_calibration_id != admitted_cost.calibration_id
            or proposal.cost_calibration_source_sha256
            != admitted_cost.source_sha256
            or proposal.cost_calibration_row_sha256 != admitted_row_sha
        ):
            _append_reason(reasons, "mtvclc_qualification_cost_binding_invalid")
        numeric_bindings = (
            (proposal.p90_spread_bps, admitted_cost.p90_spread_bps),
            (proposal.recorded_cost_bps, admitted_recorded_cost),
            (
                proposal.conversion_charge_fraction,
                admitted_cost.convert_on_close_charge_fraction,
            ),
            (proposal.p_star, admitted_p_star),
            (
                proposal.target_bps,
                TARGET_COST_MULTIPLE * admitted_recorded_cost,
            ),
            (
                proposal.stop_bps,
                STOP_COST_MULTIPLE * admitted_recorded_cost,
            ),
        )
        if any(not _same_number(left, right) for left, right in numeric_bindings):
            _append_reason(reasons, "mtvclc_qualification_cost_geometry_invalid")
        if bid is not None and ask is not None:
            quote_mid = bid + (ask - bid) / 2.0
            quote_spread_bps = (ask - bid) / quote_mid * 1e4
            if (
                not _same_number(proposal.live_spread_bps, quote_spread_bps)
                or quote_spread_bps
                > admitted_cost.p90_spread_bps + _ABS_TOLERANCE
            ):
                _append_reason(
                    reasons,
                    "mtvclc_qualification_candidate_spread_invalid",
                )
        if proposal.time_stop_bars != TIME_STOP_M1_BARS:
            _append_reason(reasons, "mtvclc_qualification_time_stop_invalid")
        if proposal.maximum_quote_gap_seconds != MAX_QUOTE_GAP_SECONDS:
            _append_reason(reasons, "mtvclc_qualification_quote_gap_invalid")
        if proposal.maximum_entries_per_symbol_utc_day != MAX_ENTRIES_PER_SYMBOL_UTC_DAY:
            _append_reason(reasons, "mtvclc_qualification_daily_cap_invalid")
        if entry is not None:
            target_bps = TARGET_COST_MULTIPLE * admitted_recorded_cost
            stop_bps = STOP_COST_MULTIPLE * admitted_recorded_cost
            expected_stop = (
                entry * (1.0 - stop_bps / 1e4)
                if proposal.side == "BUY"
                else entry * (1.0 + stop_bps / 1e4)
            )
            expected_target = (
                entry * (1.0 + target_bps / 1e4)
                if proposal.side == "BUY"
                else entry * (1.0 - target_bps / 1e4)
            )
            if not _same_number(proposal.stop_price, expected_stop) or not _same_number(
                proposal.target_price,
                expected_target,
            ):
                _append_reason(reasons, "mtvclc_qualification_bracket_geometry_invalid")
    return tuple(reasons)


def _invalid_result(
    proposal: MTVCLCTradeCandidate,
    reasons: tuple[str, ...],
) -> MTVCLCEntryQualificationResult:
    return MTVCLCEntryQualificationResult(
        proposal=proposal,
        qualified_candidate=None,
        reasons=reasons,
    )


def qualify_mtvclc_entry_candidate(
    proposal: MTVCLCTradeCandidate,
    verification: MTVCLCSignedReleaseVerification | Mapping[str, Any],
    admitted_cost: MTVCLCCostCalibration,
    *,
    as_of_epoch: float,
) -> MTVCLCEntryQualificationResult:
    """Attach the exact signed cell bound to one authority-free candidate."""

    reasons = list(
        _candidate_reasons(
            proposal,
            admitted_cost=admitted_cost,
            as_of_epoch=as_of_epoch,
        )
    )
    release_reasons, bounds, cell_hashes, base_break_even = _verification_reasons(
        verification,
        as_of_epoch=as_of_epoch,
    )
    for reason in release_reasons:
        _append_reason(reasons, reason)
    if isinstance(admitted_cost, MTVCLCCostCalibration):
        release_cost_reasons, evidence_cost_row_sha = _release_cost_reasons(
            verification,
            admitted_cost=admitted_cost,
        )
    else:
        release_cost_reasons, evidence_cost_row_sha = (), ""
    for reason in release_cost_reasons:
        _append_reason(reasons, reason)
    if reasons:
        return _invalid_result(proposal, tuple(reasons))

    assert proposal.side in _SIDES
    assert proposal.p_star is not None
    assert proposal.target_bps is not None
    assert proposal.stop_bps is not None
    lower_bound = bounds[proposal.symbol][proposal.side]
    p_star = float(proposal.p_star)
    signed_p_star = base_break_even[proposal.symbol][proposal.side]
    if not _same_number(signed_p_star, p_star):
        return _invalid_result(
            proposal,
            ("mtvclc_qualification_signed_p_star_mismatch",),
        )
    if lower_bound <= p_star:
        return _invalid_result(
            proposal,
            ("mtvclc_qualification_probability_not_above_p_star",),
        )

    conversion = float(admitted_cost.convert_on_close_charge_fraction)
    target_bps = float(proposal.target_bps)
    stop_bps = float(proposal.stop_bps)
    recorded_cost = float(admitted_cost.recorded_cost_bps)
    expected_edge = (
        lower_bound * target_bps * (1.0 - conversion)
        - (1.0 - lower_bound) * stop_bps * (1.0 + conversion)
        - recorded_cost
    )
    reward_risk = target_bps / stop_bps
    if (
        not math.isfinite(expected_edge)
        or expected_edge <= 0.0
        or not math.isfinite(reward_risk)
        or reward_risk <= 0.0
    ):
        return _invalid_result(
            proposal,
            ("mtvclc_qualification_expected_edge_not_positive",),
        )

    certificate_sha = str(
        _release_nonempty_value(
            verification,
            "certificate_sha256",
            "runtime_release_certificate_sha256",
        )
        or ""
    ).lower()
    signing_key_id = str(
        _release_nonempty_value(
            verification,
            "signing_key_id",
            "runtime_release_signing_key_id",
        )
        or ""
    ).lower()
    evidence_sha = str(_release_value(verification, "evidence_sha256") or "").lower()
    surface_sha = str(
        _release_value(verification, "qualification_surface_sha256") or ""
    ).lower()
    qualified = QualifiedMTVCLCEntryCandidate(
        proposal=proposal,
        admitted_cost=admitted_cost,
        win_probability_lower_bound=float(lower_bound),
        conservative_expected_edge_bps=float(expected_edge),
        reward_risk_ratio=float(reward_risk),
        release_generation_id=str(
            _release_value(verification, "generation_id") or ""
        ),
        release_certificate_sha256=certificate_sha,
        release_signing_key_id=signing_key_id,
        evidence_sha256=evidence_sha,
        evidence_cell_sha256=cell_hashes[proposal.symbol][proposal.side],
        evidence_cost_row_sha256=evidence_cost_row_sha,
        qualification_surface_sha256=surface_sha,
        release_expires_at_epoch=float(
            _release_value(verification, "expires_at_epoch")
        ),
    )
    return MTVCLCEntryQualificationResult(
        proposal=proposal,
        qualified_candidate=qualified,
        reasons=(),
    )


__all__ = [
    "MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION",
    "MTVCLC_SIGNED_PROBABILITY_SOURCE",
    "MTVCLCEntryQualificationResult",
    "MTVCLCSignedReleaseVerification",
    "QualifiedMTVCLCEntryCandidate",
    "qualify_mtvclc_entry_candidate",
]
