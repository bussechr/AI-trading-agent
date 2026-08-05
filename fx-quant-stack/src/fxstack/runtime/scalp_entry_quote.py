# AGENT: ROLE: Pure fresh-quote re-anchoring for one qualified scalp entry.
# AGENT: ENTRYPOINT: `refresh_scalp_entry_quote`.
# AGENT: PRIMARY INPUTS: evidence-qualified candidate, canonical IG MT4 tick, clock.
# AGENT: PRIMARY OUTPUTS: immutable refreshed prices/payoff metrics or refusal diagnostics.
# AGENT: STATE / SIDE EFFECTS: none; never reads settings, sizes, persists, or commands.
"""Fail-closed quote refresh for a qualified production scalp candidate.

Qualification authenticates the strategy cell's conservative win-probability
bound, but its signal quote can age before final risk approval.  This module
refreshes that quote without granting any execution authority.  It assumes no
favourable price improvement, re-anchors the original strategy distances, and
rechecks the authenticated probability bound against current spread cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_VENUE_ID,
    get_ig_mt4_instrument,
)
from fxstack.runtime.scalp_entry_qualification import (
    QualifiedScalpEntryCandidate,
)
from fxstack.schemas.entry import EntryProposal
from fxstack.strategy.scalp_dislocation import SCALP_EXECUTION_DEBIT_BPS


SCALP_ENTRY_QUOTE_SCHEMA_VERSION = "fxstack.runtime.scalp_entry_quote.v1"
_ENTRY_SIDES = ("BUY", "SELL")
_TIMESTAMP_FIELDS = (
    "market_event_received_at_epoch",
    "ts_epoch",
    "received_at_epoch",
    "timestamp_epoch",
    "timestamp",
    "time",
    "ts",
)
_FLOAT_ABSOLUTE_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class RefreshedScalpEntryCandidate:
    """A qualified candidate re-anchored to one fresh adverse-side quote."""

    qualified_candidate: QualifiedScalpEntryCandidate
    quote_timestamp_epoch: float
    quote_age_secs: float
    bid_price: float
    ask_price: float
    mid_price: float
    refreshed_entry_price: float
    refreshed_sl_price: float
    refreshed_tp_price: float
    stop_distance_price: float
    target_distance_price: float
    current_spread_bps: float
    adverse_slippage_bps: float
    total_current_cost_bps: float
    live_p_star: float
    conservative_expected_edge_bps: float
    reward_risk_ratio: float
    schema_version: str = SCALP_ENTRY_QUOTE_SCHEMA_VERSION

    @property
    def proposal(self) -> EntryProposal:
        return self.qualified_candidate.proposal

    @property
    def symbol(self) -> str:
        return self.proposal.symbol

    @property
    def side(self) -> str:
        return str(self.proposal.side or "")

    @property
    def win_probability_lower_bound(self) -> float:
        return self.qualified_candidate.win_probability_lower_bound

    @property
    def probability_source(self) -> str:
        return str(self.qualified_candidate.probability_source)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpEntryQuoteDiagnostics:
    """Immutable normalized quote evidence for acceptance or refusal."""

    accepted: bool
    reasons: tuple[str, ...]
    tick_symbol: str = ""
    tick_venue_id: str = ""
    timestamp_source: str = ""
    quote_timestamp_epoch: float | None = None
    as_of_epoch: float | None = None
    max_tick_age_secs: float | None = None
    quote_age_secs: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    mid_price: float | None = None
    signal_entry_price: float | None = None
    refreshed_entry_price: float | None = None
    refreshed_sl_price: float | None = None
    refreshed_tp_price: float | None = None
    current_spread_bps: float | None = None
    adverse_slippage_bps: float | None = None
    total_current_cost_bps: float | None = None
    live_p_star: float | None = None
    conservative_expected_edge_bps: float | None = None
    schema_version: str = SCALP_ENTRY_QUOTE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpEntryQuoteRefreshResult:
    """Pure refresh result; success still carries no risk or queue authority."""

    qualified_candidate: QualifiedScalpEntryCandidate
    refreshed_candidate: RefreshedScalpEntryCandidate | None
    diagnostics: ScalpEntryQuoteDiagnostics

    @property
    def accepted(self) -> bool:
        return self.refreshed_candidate is not None and self.diagnostics.accepted

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.diagnostics.reasons

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _finite_positive(value: Any) -> float | None:
    number = _finite_number(value)
    return number if number is not None and number > 0.0 else None


def _parse_epoch(value: Any) -> float | None:
    numeric = _finite_positive(value)
    if numeric is not None:
        return numeric
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except (ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        return None
    try:
        epoch = parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return epoch if math.isfinite(epoch) and epoch > 0.0 else None


def _nested_mapping(raw_tick: Mapping[str, Any], key: str) -> Mapping[str, Any] | None:
    value = raw_tick.get(key)
    return value if isinstance(value, Mapping) else None


def _identity_values(
    raw_tick: Mapping[str, Any],
    *,
    keys: tuple[str, ...],
    nested_key: str,
    uppercase: bool,
) -> tuple[tuple[str, ...], bool]:
    containers: tuple[Mapping[str, Any], ...] = (raw_tick,)
    nested = _nested_mapping(raw_tick, nested_key)
    if nested is not None:
        containers = (*containers, nested)
    values: list[str] = []
    any_key_present = False
    for container in containers:
        for key in keys:
            if key not in container:
                continue
            any_key_present = True
            value = str(container.get(key) or "").strip()
            if value:
                values.append(value.upper() if uppercase else value.lower())
    return tuple(dict.fromkeys(values)), any_key_present


def _extract_identity(
    raw_tick: Mapping[str, Any],
    *,
    expected_symbol: str,
) -> tuple[str, str, tuple[str, ...]]:
    reasons: list[str] = []
    if "instrument" in raw_tick and not isinstance(raw_tick.get("instrument"), Mapping):
        _append_reason(reasons, "scalp_entry_quote_instrument_identity_invalid")

    symbols, symbol_key_present = _identity_values(
        raw_tick,
        keys=("canonical_symbol", "symbol", "pair"),
        nested_key="instrument",
        uppercase=True,
    )
    venues, venue_key_present = _identity_values(
        raw_tick,
        keys=("venue_id", "venue", "broker_venue_id"),
        nested_key="instrument",
        uppercase=False,
    )
    tick_symbol = symbols[0] if len(symbols) == 1 else ""
    tick_venue = venues[0] if len(venues) == 1 else ""

    if not symbols:
        _append_reason(reasons, "scalp_entry_quote_tick_symbol_missing")
    elif len(symbols) != 1:
        _append_reason(reasons, "scalp_entry_quote_tick_symbol_conflict")
    elif tick_symbol != expected_symbol:
        _append_reason(reasons, "scalp_entry_quote_tick_symbol_mismatch")
    if not venues:
        _append_reason(reasons, "scalp_entry_quote_tick_venue_missing")
    elif len(venues) != 1:
        _append_reason(reasons, "scalp_entry_quote_tick_venue_conflict")
    elif tick_venue != IG_MT4_VENUE_ID:
        _append_reason(reasons, "scalp_entry_quote_tick_venue_invalid")

    # Empty aliases do not conflict with an explicit canonical value (crypto
    # instruments legitimately expose an empty ``pair``), but a mapping with
    # only empty aliases remains missing above.
    del symbol_key_present, venue_key_present
    return tick_symbol, tick_venue, tuple(reasons)


def _extract_timestamp(
    raw_tick: Mapping[str, Any],
) -> tuple[float | None, str, str]:
    metadata = _nested_mapping(raw_tick, "metadata")
    containers: tuple[tuple[str, Mapping[str, Any]], ...] = (("", raw_tick),)
    if metadata is not None:
        containers = (*containers, ("metadata.", metadata))
    for field in _TIMESTAMP_FIELDS:
        for prefix, container in containers:
            if field not in container:
                continue
            source = f"{prefix}{field}"
            parsed = _parse_epoch(container.get(field))
            if parsed is None:
                return None, source, "scalp_entry_quote_tick_timestamp_invalid"
            return parsed, source, ""
    return None, "", "scalp_entry_quote_tick_timestamp_missing"


def _candidate_reasons(
    candidate: QualifiedScalpEntryCandidate,
) -> tuple[str, ...]:
    proposal = candidate.proposal
    reasons: list[str] = []
    instrument = get_ig_mt4_instrument(proposal.symbol)
    if instrument is None or proposal.symbol != instrument.canonical_symbol:
        _append_reason(reasons, "scalp_entry_quote_candidate_symbol_invalid")
    if proposal.venue_id != IG_MT4_VENUE_ID:
        _append_reason(reasons, "scalp_entry_quote_candidate_venue_invalid")
    if proposal.side not in _ENTRY_SIDES:
        _append_reason(reasons, "scalp_entry_quote_candidate_side_invalid")
    if not proposal.allowed or proposal.reasons or proposal.execution_qualified:
        _append_reason(reasons, "scalp_entry_quote_candidate_state_invalid")

    numeric: dict[str, float | None] = {
        field: _finite_positive(getattr(proposal, field))
        for field in (
            "ref_mid",
            "entry_price",
            "stop_bps",
            "target_bps",
            "spread_bps",
            "p_star",
        )
    }
    if any(value is None for value in numeric.values()):
        _append_reason(reasons, "scalp_entry_quote_candidate_geometry_invalid")

    lower_bound = _finite_number(candidate.win_probability_lower_bound)
    expected_edge = _finite_positive(candidate.conservative_expected_edge_bps)
    reward_risk = _finite_positive(candidate.reward_risk_ratio)
    if lower_bound is None or not 0.0 <= lower_bound <= 1.0:
        _append_reason(reasons, "scalp_entry_quote_candidate_probability_invalid")
    if expected_edge is None or reward_risk is None:
        _append_reason(reasons, "scalp_entry_quote_candidate_payoff_invalid")

    if not reasons:
        stop_bps = float(numeric["stop_bps"] or 0.0)
        target_bps = float(numeric["target_bps"] or 0.0)
        spread_bps = float(numeric["spread_bps"] or 0.0)
        p_star = float(numeric["p_star"] or 0.0)
        assert lower_bound is not None
        recorded_cost_bps = spread_bps + SCALP_EXECUTION_DEBIT_BPS
        recomputed_p_star = (stop_bps + recorded_cost_bps) / (target_bps + stop_bps)
        recomputed_edge = (
            lower_bound * target_bps
            - (1.0 - lower_bound) * stop_bps
            - recorded_cost_bps
        )
        recomputed_reward_risk = target_bps / stop_bps
        if (
            not math.isclose(
                p_star,
                recomputed_p_star,
                rel_tol=0.0,
                abs_tol=_FLOAT_ABSOLUTE_TOLERANCE,
            )
            or lower_bound <= p_star
            or expected_edge is None
            or not math.isclose(
                expected_edge,
                recomputed_edge,
                rel_tol=0.0,
                abs_tol=_FLOAT_ABSOLUTE_TOLERANCE,
            )
            or reward_risk is None
            or not math.isclose(
                reward_risk,
                recomputed_reward_risk,
                rel_tol=0.0,
                abs_tol=_FLOAT_ABSOLUTE_TOLERANCE,
            )
        ):
            _append_reason(reasons, "scalp_entry_quote_candidate_payoff_mismatch")
    return tuple(reasons)


def _result(
    *,
    qualified_candidate: QualifiedScalpEntryCandidate,
    refreshed_candidate: RefreshedScalpEntryCandidate | None,
    reasons: tuple[str, ...],
    tick_symbol: str = "",
    tick_venue_id: str = "",
    timestamp_source: str = "",
    quote_timestamp_epoch: float | None = None,
    as_of_epoch: float | None = None,
    max_tick_age_secs: float | None = None,
    quote_age_secs: float | None = None,
    bid_price: float | None = None,
    ask_price: float | None = None,
    mid_price: float | None = None,
    signal_entry_price: float | None = None,
    refreshed_entry_price: float | None = None,
    refreshed_sl_price: float | None = None,
    refreshed_tp_price: float | None = None,
    current_spread_bps: float | None = None,
    adverse_slippage_bps: float | None = None,
    total_current_cost_bps: float | None = None,
    live_p_star: float | None = None,
    conservative_expected_edge_bps: float | None = None,
) -> ScalpEntryQuoteRefreshResult:
    diagnostics = ScalpEntryQuoteDiagnostics(
        accepted=refreshed_candidate is not None and not reasons,
        reasons=reasons,
        tick_symbol=tick_symbol,
        tick_venue_id=tick_venue_id,
        timestamp_source=timestamp_source,
        quote_timestamp_epoch=quote_timestamp_epoch,
        as_of_epoch=as_of_epoch,
        max_tick_age_secs=max_tick_age_secs,
        quote_age_secs=quote_age_secs,
        bid_price=bid_price,
        ask_price=ask_price,
        mid_price=mid_price,
        signal_entry_price=signal_entry_price,
        refreshed_entry_price=refreshed_entry_price,
        refreshed_sl_price=refreshed_sl_price,
        refreshed_tp_price=refreshed_tp_price,
        current_spread_bps=current_spread_bps,
        adverse_slippage_bps=adverse_slippage_bps,
        total_current_cost_bps=total_current_cost_bps,
        live_p_star=live_p_star,
        conservative_expected_edge_bps=conservative_expected_edge_bps,
    )
    return ScalpEntryQuoteRefreshResult(
        qualified_candidate=qualified_candidate,
        refreshed_candidate=refreshed_candidate,
        diagnostics=diagnostics,
    )


def refresh_scalp_entry_quote(
    qualified_candidate: QualifiedScalpEntryCandidate,
    tick: Mapping[str, Any],
    *,
    as_of_epoch: float,
    max_tick_age_secs: float,
) -> ScalpEntryQuoteRefreshResult:
    """Re-anchor one qualified candidate to a fresh canonical IG MT4 tick.

    Adverse entry movement is retained while favourable movement is ignored.
    Because the stop and target distances move with that adverse entry, entry
    slippage is diagnostic rather than a second payoff debit; current spread
    is the complete known current cost used for the live break-even check.
    """

    candidate_reasons = _candidate_reasons(qualified_candidate)
    if candidate_reasons:
        return _result(
            qualified_candidate=qualified_candidate,
            refreshed_candidate=None,
            reasons=candidate_reasons,
        )
    proposal = qualified_candidate.proposal

    if not isinstance(tick, Mapping):
        return _result(
            qualified_candidate=qualified_candidate,
            refreshed_candidate=None,
            reasons=("scalp_entry_quote_tick_mapping_invalid",),
        )
    raw_tick = dict(tick)
    tick_symbol, tick_venue, identity_reasons = _extract_identity(
        raw_tick,
        expected_symbol=proposal.symbol,
    )

    bid = _finite_positive(raw_tick.get("bid"))
    ask = _finite_positive(raw_tick.get("ask"))
    quote_reasons: list[str] = list(identity_reasons)
    if bid is None:
        _append_reason(quote_reasons, "scalp_entry_quote_bid_invalid")
    if ask is None:
        _append_reason(quote_reasons, "scalp_entry_quote_ask_invalid")
    if bid is not None and ask is not None and ask < bid:
        _append_reason(quote_reasons, "scalp_entry_quote_bid_ask_geometry_invalid")

    as_of = _finite_positive(as_of_epoch)
    max_age = _finite_number(max_tick_age_secs)
    if as_of is None:
        _append_reason(quote_reasons, "scalp_entry_quote_clock_invalid")
    if max_age is None or max_age < 0.0:
        _append_reason(quote_reasons, "scalp_entry_quote_max_tick_age_invalid")
        max_age = None
    tick_epoch, timestamp_source, timestamp_reason = _extract_timestamp(raw_tick)
    if timestamp_reason:
        _append_reason(quote_reasons, timestamp_reason)

    quote_age: float | None = None
    if tick_epoch is not None and as_of is not None:
        quote_age = as_of - tick_epoch
        if quote_age < 0.0:
            _append_reason(quote_reasons, "scalp_entry_quote_tick_from_future")
        elif max_age is not None and quote_age > max_age:
            _append_reason(quote_reasons, "scalp_entry_quote_tick_stale")
    if quote_reasons:
        return _result(
            qualified_candidate=qualified_candidate,
            refreshed_candidate=None,
            reasons=tuple(quote_reasons),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            timestamp_source=timestamp_source,
            quote_timestamp_epoch=tick_epoch,
            as_of_epoch=as_of,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            signal_entry_price=_finite_positive(proposal.entry_price),
        )

    assert bid is not None
    assert ask is not None
    assert tick_epoch is not None
    assert as_of is not None
    assert max_age is not None
    assert quote_age is not None
    assert proposal.ref_mid is not None
    assert proposal.entry_price is not None
    assert proposal.stop_bps is not None
    assert proposal.target_bps is not None
    assert proposal.side in _ENTRY_SIDES

    reference_mid = float(proposal.ref_mid)
    signal_entry = float(proposal.entry_price)
    stop_bps = float(proposal.stop_bps)
    target_bps = float(proposal.target_bps)
    lower_bound = float(qualified_candidate.win_probability_lower_bound)
    mid = bid + (ask - bid) / 2.0
    current_spread_bps = (ask - bid) / mid * 1e4
    stop_distance = reference_mid * stop_bps / 1e4
    target_distance = reference_mid * target_bps / 1e4

    if proposal.side == "BUY":
        refreshed_entry = max(signal_entry, ask)
        adverse_slippage_bps = (refreshed_entry - signal_entry) / reference_mid * 1e4
        refreshed_sl = refreshed_entry - stop_distance
        refreshed_tp = refreshed_entry + target_distance
        direction_valid = 0.0 < refreshed_sl < refreshed_entry < refreshed_tp
    else:
        refreshed_entry = min(signal_entry, bid)
        adverse_slippage_bps = (signal_entry - refreshed_entry) / reference_mid * 1e4
        refreshed_sl = refreshed_entry + stop_distance
        refreshed_tp = refreshed_entry - target_distance
        direction_valid = 0.0 < refreshed_tp < refreshed_entry < refreshed_sl

    geometry_values = (
        mid,
        current_spread_bps,
        stop_distance,
        target_distance,
        refreshed_entry,
        adverse_slippage_bps,
        refreshed_sl,
        refreshed_tp,
    )
    if not direction_valid or any(
        not math.isfinite(value) or value < 0.0 for value in geometry_values
    ):
        return _result(
            qualified_candidate=qualified_candidate,
            refreshed_candidate=None,
            reasons=("scalp_entry_quote_refreshed_geometry_invalid",),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            timestamp_source=timestamp_source,
            quote_timestamp_epoch=tick_epoch,
            as_of_epoch=as_of,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            signal_entry_price=signal_entry,
            refreshed_entry_price=refreshed_entry,
            refreshed_sl_price=refreshed_sl,
            refreshed_tp_price=refreshed_tp,
            current_spread_bps=current_spread_bps,
            adverse_slippage_bps=adverse_slippage_bps,
        )

    # Re-anchoring preserves both gross distances, so adverse price movement
    # must not be charged again as a payoff debit.  The current spread remains
    # the conservative known round-trip cost for this strategy contract.
    total_current_cost_bps = current_spread_bps + SCALP_EXECUTION_DEBIT_BPS
    live_p_star = (stop_bps + total_current_cost_bps) / (target_bps + stop_bps)
    expected_edge = (
        lower_bound * target_bps
        - (1.0 - lower_bound) * stop_bps
        - total_current_cost_bps
    )
    payoff_values = (total_current_cost_bps, live_p_star, expected_edge)
    if (
        any(not math.isfinite(value) for value in payoff_values)
        or not 0.0 <= live_p_star < 1.0
        or lower_bound <= live_p_star
        or expected_edge <= 0.0
    ):
        return _result(
            qualified_candidate=qualified_candidate,
            refreshed_candidate=None,
            reasons=("scalp_entry_quote_cost_dead",),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            timestamp_source=timestamp_source,
            quote_timestamp_epoch=tick_epoch,
            as_of_epoch=as_of,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            signal_entry_price=signal_entry,
            refreshed_entry_price=refreshed_entry,
            refreshed_sl_price=refreshed_sl,
            refreshed_tp_price=refreshed_tp,
            current_spread_bps=current_spread_bps,
            adverse_slippage_bps=adverse_slippage_bps,
            total_current_cost_bps=total_current_cost_bps,
            live_p_star=live_p_star,
            conservative_expected_edge_bps=expected_edge,
        )

    refreshed = RefreshedScalpEntryCandidate(
        qualified_candidate=qualified_candidate,
        quote_timestamp_epoch=tick_epoch,
        quote_age_secs=quote_age,
        bid_price=bid,
        ask_price=ask,
        mid_price=mid,
        refreshed_entry_price=refreshed_entry,
        refreshed_sl_price=refreshed_sl,
        refreshed_tp_price=refreshed_tp,
        stop_distance_price=stop_distance,
        target_distance_price=target_distance,
        current_spread_bps=current_spread_bps,
        adverse_slippage_bps=adverse_slippage_bps,
        total_current_cost_bps=total_current_cost_bps,
        live_p_star=live_p_star,
        conservative_expected_edge_bps=expected_edge,
        reward_risk_ratio=target_bps / stop_bps,
    )
    return _result(
        qualified_candidate=qualified_candidate,
        refreshed_candidate=refreshed,
        reasons=(),
        tick_symbol=tick_symbol,
        tick_venue_id=tick_venue,
        timestamp_source=timestamp_source,
        quote_timestamp_epoch=tick_epoch,
        as_of_epoch=as_of,
        max_tick_age_secs=max_age,
        quote_age_secs=quote_age,
        bid_price=bid,
        ask_price=ask,
        mid_price=mid,
        signal_entry_price=signal_entry,
        refreshed_entry_price=refreshed_entry,
        refreshed_sl_price=refreshed_sl,
        refreshed_tp_price=refreshed_tp,
        current_spread_bps=current_spread_bps,
        adverse_slippage_bps=adverse_slippage_bps,
        total_current_cost_bps=total_current_cost_bps,
        live_p_star=live_p_star,
        conservative_expected_edge_bps=expected_edge,
    )


__all__ = [
    "SCALP_ENTRY_QUOTE_SCHEMA_VERSION",
    "RefreshedScalpEntryCandidate",
    "ScalpEntryQuoteDiagnostics",
    "ScalpEntryQuoteRefreshResult",
    "refresh_scalp_entry_quote",
]
