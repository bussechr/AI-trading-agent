# AGENT: ROLE: Pure authenticated-quote refresh for one qualified MTVCLC candidate.
# AGENT: ENTRYPOINT: `refresh_mtvclc_entry_quote`.
# AGENT: PRIMARY INPUTS: signed-qualified candidate, authenticated IG MT4 tick, clock.
# AGENT: PRIMARY OUTPUTS: immutable current ask/bid entry geometry and full-cost payoff, or refusal.
# AGENT: STATE / SIDE EFFECTS: none; no I/O, sizing, persistence, queue, broker, or signing access.
"""Refresh a qualified MTVCLC candidate on one authenticated IG MT4 tick.

The pure strategy candidate uses the first eligible quote after the completed
signal bar.  Before risk and broker-grid projection, this seam re-anchors the
same frozen 4x/8x cost distances to the current immediate-market side: BUY at
ask and SELL at bid.  It uses the complete current cost rather than the legacy
spread-only scalp equation and preserves the conversion charge used by the
sealed MTVCLC screen.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import math
from typing import Any

from fxstack._serialization import flat_dataclass_dict
from fxstack.providers.ig_mt4_catalog import IG_MT4_VENUE_ID
from fxstack.runtime.market_source_identity import (
    MARKET_SOURCE_SCHEMA,
    authenticated_market_source_from_row,
)
from fxstack.runtime.mtvclc_entry_qualification import (
    MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION,
    MTVCLC_SIGNED_PROBABILITY_SOURCE,
    QualifiedMTVCLCEntryCandidate,
)
from fxstack.strategy.mtvclc import (
    MAX_QUOTE_GAP_SECONDS,
    MTVCLC_CONFIG_ID,
    MTVCLC_CONFIG_SHA256,
    MTVCLC_STRATEGY_ID,
    MTVCLC_STRATEGY_VERSION,
    STOP_COST_MULTIPLE,
    TARGET_COST_MULTIPLE,
    TIME_STOP_M1_BARS,
    MTVCLCCostCalibration,
    MTVCLCTradeCandidate,
)


MTVCLC_ENTRY_QUOTE_SCHEMA_VERSION = "fxstack.runtime.mtvclc_entry_quote.v1"
_SIDES = ("BUY", "SELL")
_ABS_TOLERANCE = 1e-12


@dataclass(frozen=True, slots=True)
class RefreshedMTVCLCEntryCandidate:
    """A qualified MTVCLC candidate re-anchored to an authenticated tick."""

    qualified_candidate: QualifiedMTVCLCEntryCandidate
    quote_timestamp_epoch: float
    quote_age_secs: float
    market_source_id: str
    source_event_token_sha256: str
    market_event_sequence: int
    bid_price: float
    ask_price: float
    mid_price: float
    refreshed_entry_price: float
    refreshed_sl_price: float
    refreshed_tp_price: float
    stop_distance_price: float
    target_distance_price: float
    stop_bps: float
    target_bps: float
    current_spread_bps: float
    fixed_non_spread_cost_bps: float
    current_total_cost_bps: float
    conversion_charge_fraction: float
    live_p_star: float
    conservative_expected_edge_bps: float
    reward_risk_ratio: float
    adverse_slippage_bps: float
    schema_version: str = MTVCLC_ENTRY_QUOTE_SCHEMA_VERSION

    @property
    def proposal(self) -> MTVCLCTradeCandidate:
        return self.qualified_candidate.proposal

    @property
    def admitted_cost(self) -> MTVCLCCostCalibration:
        return self.qualified_candidate.admitted_cost

    @property
    def symbol(self) -> str:
        return self.qualified_candidate.symbol

    @property
    def side(self) -> str:
        return self.qualified_candidate.side

    @property
    def win_probability_lower_bound(self) -> float:
        return self.qualified_candidate.win_probability_lower_bound

    @property
    def probability_source(self) -> str:
        return self.qualified_candidate.probability_source

    @property
    def entry_deadline_epoch(self) -> int:
        return self.qualified_candidate.entry_deadline_epoch

    @property
    def execution_type(self) -> str:
        return self.qualified_candidate.execution_type

    @property
    def pending_orders_forbidden(self) -> bool:
        return self.qualified_candidate.pending_orders_forbidden

    @property
    def geometry_reference_price(self) -> float:
        """Price basis used for the fixed bps distance projection."""

        return self.refreshed_entry_price

    def to_dict(self) -> dict[str, Any]:
        payload = flat_dataclass_dict(self)
        payload["qualified_candidate"] = self.qualified_candidate.to_dict()
        return payload


@dataclass(frozen=True, slots=True)
class MTVCLCEntryQuoteDiagnostics:
    accepted: bool
    reasons: tuple[str, ...]
    tick_symbol: str = ""
    tick_venue_id: str = ""
    market_source_id: str = ""
    quote_timestamp_epoch: float | None = None
    as_of_epoch: float | None = None
    max_tick_age_secs: float | None = None
    quote_age_secs: float | None = None
    bid_price: float | None = None
    ask_price: float | None = None
    mid_price: float | None = None
    current_spread_bps: float | None = None
    fixed_non_spread_cost_bps: float | None = None
    current_total_cost_bps: float | None = None
    live_p_star: float | None = None
    conservative_expected_edge_bps: float | None = None
    schema_version: str = MTVCLC_ENTRY_QUOTE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return flat_dataclass_dict(self)


@dataclass(frozen=True, slots=True)
class MTVCLCEntryQuoteRefreshResult:
    qualified_candidate: QualifiedMTVCLCEntryCandidate
    refreshed_candidate: RefreshedMTVCLCEntryCandidate | None
    diagnostics: MTVCLCEntryQuoteDiagnostics

    @property
    def accepted(self) -> bool:
        return self.refreshed_candidate is not None and self.diagnostics.accepted

    @property
    def reasons(self) -> tuple[str, ...]:
        return self.diagnostics.reasons

    def to_dict(self) -> dict[str, Any]:
        payload = flat_dataclass_dict(self)
        payload["qualified_candidate"] = self.qualified_candidate.to_dict()
        payload["refreshed_candidate"] = (
            None
            if self.refreshed_candidate is None
            else self.refreshed_candidate.to_dict()
        )
        payload["diagnostics"] = self.diagnostics.to_dict()
        return payload


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


def _is_sha256(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _quality_flags(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = row.get("quality_flags")
    if raw is None:
        return ()
    if isinstance(raw, str):
        values: Sequence[Any] = (raw,)
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        values = raw
    else:
        values = (raw,)
    return tuple(
        str(value or "").strip() for value in values if str(value or "").strip()
    )


def _qualified_reasons(
    qualified: QualifiedMTVCLCEntryCandidate,
    *,
    as_of_epoch: Any,
) -> tuple[str, ...]:
    if not isinstance(qualified, QualifiedMTVCLCEntryCandidate):
        return ("mtvclc_entry_quote_candidate_not_typed",)
    reasons: list[str] = []
    proposal = qualified.proposal
    cost = qualified.admitted_cost
    now = _positive(as_of_epoch)
    if qualified.schema_version != MTVCLC_ENTRY_QUALIFICATION_SCHEMA_VERSION:
        _append_reason(reasons, "mtvclc_entry_quote_qualification_schema_invalid")
    if qualified.probability_source != MTVCLC_SIGNED_PROBABILITY_SOURCE:
        _append_reason(reasons, "mtvclc_entry_quote_probability_source_invalid")
    if not _is_sha256(qualified.release_certificate_sha256):
        _append_reason(reasons, "mtvclc_entry_quote_release_certificate_invalid")
    if not _is_sha256(qualified.release_signing_key_id):
        _append_reason(reasons, "mtvclc_entry_quote_release_key_invalid")
    if (
        not str(qualified.release_generation_id or "").strip()
        or not _is_sha256(qualified.evidence_sha256)
        or not _is_sha256(qualified.evidence_cell_sha256)
        or not _is_sha256(qualified.evidence_cost_row_sha256)
        or not _is_sha256(qualified.qualification_surface_sha256)
    ):
        _append_reason(reasons, "mtvclc_entry_quote_surface_identity_invalid")
    release_expiry = _positive(qualified.release_expires_at_epoch)
    if now is None:
        _append_reason(reasons, "mtvclc_entry_quote_clock_invalid")
    elif release_expiry is None or release_expiry <= now:
        _append_reason(reasons, "mtvclc_entry_quote_release_expired")
    if proposal.entry_deadline_epoch is None or (
        now is not None and proposal.entry_deadline_epoch <= now
    ):
        _append_reason(reasons, "mtvclc_entry_quote_entry_deadline_expired")
    if (
        proposal.strategy_id != MTVCLC_STRATEGY_ID
        or proposal.strategy_version != MTVCLC_STRATEGY_VERSION
        or proposal.config_id != MTVCLC_CONFIG_ID
        or proposal.config_sha256 != MTVCLC_CONFIG_SHA256
        or proposal.venue_id != IG_MT4_VENUE_ID
        or proposal.side not in _SIDES
        or proposal.execution_type != "market"
        or proposal.immediate_market_trade is not True
        or proposal.pending_orders_forbidden is not True
        or proposal.allowed is not True
        or proposal.reasons != ()
    ):
        _append_reason(reasons, "mtvclc_entry_quote_candidate_identity_invalid")
    if not isinstance(cost, MTVCLCCostCalibration) or cost.symbol != proposal.symbol:
        _append_reason(reasons, "mtvclc_entry_quote_cost_invalid")
        return tuple(reasons)
    try:
        row_sha = cost.row_sha256()
        recorded_cost = float(cost.recorded_cost_bps)
        frozen_p_star = float(cost.break_even_win_probability)
    except (TypeError, ValueError, OverflowError, ZeroDivisionError):
        row_sha = ""
        recorded_cost = 0.0
        frozen_p_star = 0.0
    if (
        proposal.cost_calibration_id != cost.calibration_id
        or proposal.cost_calibration_source_sha256 != cost.source_sha256
        or proposal.cost_calibration_row_sha256 != row_sha
        or not _same_number(proposal.recorded_cost_bps, recorded_cost)
        or not _same_number(proposal.p_star, frozen_p_star)
        or not _same_number(
            proposal.target_bps,
            TARGET_COST_MULTIPLE * recorded_cost,
        )
        or not _same_number(
            proposal.stop_bps,
            STOP_COST_MULTIPLE * recorded_cost,
        )
        or proposal.time_stop_bars != TIME_STOP_M1_BARS
    ):
        _append_reason(reasons, "mtvclc_entry_quote_cost_binding_invalid")
    lower = _finite(qualified.win_probability_lower_bound)
    if lower is None or not frozen_p_star < lower <= 1.0:
        _append_reason(reasons, "mtvclc_entry_quote_probability_invalid")
    if (
        not _positive(qualified.conservative_expected_edge_bps)
        or not _positive(qualified.reward_risk_ratio)
    ):
        _append_reason(reasons, "mtvclc_entry_quote_payoff_invalid")
    return tuple(reasons)


def _tick_identity(
    row: Mapping[str, Any],
    *,
    expected_symbol: str,
    expected_source_id: str,
) -> tuple[str, str, str, tuple[str, ...]]:
    reasons: list[str] = []
    instrument_raw = row.get("instrument")
    instrument = dict(instrument_raw) if isinstance(instrument_raw, Mapping) else {}
    symbol = str(
        instrument.get("canonical_symbol")
        or row.get("canonical_symbol")
        or row.get("pair")
        or row.get("symbol")
        or ""
    ).strip().upper()
    venue = str(instrument.get("venue") or row.get("venue") or "").strip().lower()
    if str(row.get("provider") or "").strip().lower() != "mt4_bridge":
        _append_reason(reasons, "mtvclc_entry_quote_provider_invalid")
    if symbol != expected_symbol:
        _append_reason(reasons, "mtvclc_entry_quote_symbol_invalid")
    if venue != IG_MT4_VENUE_ID:
        _append_reason(reasons, "mtvclc_entry_quote_venue_invalid")
    source, source_error = authenticated_market_source_from_row(row)
    source_id = source.source_id if source is not None else ""
    if source_error:
        _append_reason(reasons, f"mtvclc_entry_quote_{source_error}")
    elif source_id != expected_source_id:
        _append_reason(reasons, "mtvclc_entry_quote_market_source_changed")
    if row.get("market_source_schema") != MARKET_SOURCE_SCHEMA:
        _append_reason(reasons, "mtvclc_entry_quote_market_source_schema_invalid")
    return symbol, venue, source_id, tuple(reasons)


def _result(
    *,
    qualified: QualifiedMTVCLCEntryCandidate,
    refreshed: RefreshedMTVCLCEntryCandidate | None,
    reasons: tuple[str, ...],
    tick_symbol: str = "",
    tick_venue_id: str = "",
    market_source_id: str = "",
    quote_timestamp_epoch: float | None = None,
    as_of_epoch: float | None = None,
    max_tick_age_secs: float | None = None,
    quote_age_secs: float | None = None,
    bid_price: float | None = None,
    ask_price: float | None = None,
    mid_price: float | None = None,
    current_spread_bps: float | None = None,
    fixed_non_spread_cost_bps: float | None = None,
    current_total_cost_bps: float | None = None,
    live_p_star: float | None = None,
    conservative_expected_edge_bps: float | None = None,
) -> MTVCLCEntryQuoteRefreshResult:
    accepted = refreshed is not None and not reasons
    return MTVCLCEntryQuoteRefreshResult(
        qualified_candidate=qualified,
        refreshed_candidate=refreshed if accepted else None,
        diagnostics=MTVCLCEntryQuoteDiagnostics(
            accepted=accepted,
            reasons=reasons,
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue_id,
            market_source_id=market_source_id,
            quote_timestamp_epoch=quote_timestamp_epoch,
            as_of_epoch=as_of_epoch,
            max_tick_age_secs=max_tick_age_secs,
            quote_age_secs=quote_age_secs,
            bid_price=bid_price,
            ask_price=ask_price,
            mid_price=mid_price,
            current_spread_bps=current_spread_bps,
            fixed_non_spread_cost_bps=fixed_non_spread_cost_bps,
            current_total_cost_bps=current_total_cost_bps,
            live_p_star=live_p_star,
            conservative_expected_edge_bps=conservative_expected_edge_bps,
        ),
    )


def refresh_mtvclc_entry_quote(
    qualified_candidate: QualifiedMTVCLCEntryCandidate,
    tick: Mapping[str, Any],
    *,
    as_of_epoch: float,
    max_tick_age_secs: float = float(MAX_QUOTE_GAP_SECONDS),
) -> MTVCLCEntryQuoteRefreshResult:
    """Re-anchor one qualified candidate to the current ask or bid."""

    candidate_reasons = _qualified_reasons(
        qualified_candidate,
        as_of_epoch=as_of_epoch,
    )
    if candidate_reasons:
        return _result(
            qualified=qualified_candidate,
            refreshed=None,
            reasons=candidate_reasons,
        )
    if not isinstance(tick, Mapping):
        return _result(
            qualified=qualified_candidate,
            refreshed=None,
            reasons=("mtvclc_entry_quote_tick_not_mapping",),
        )

    proposal = qualified_candidate.proposal
    cost = qualified_candidate.admitted_cost
    raw = dict(tick)
    tick_symbol, tick_venue, source_id, identity_reasons = _tick_identity(
        raw,
        expected_symbol=proposal.symbol,
        expected_source_id=proposal.quote_source_id,
    )
    reasons = list(identity_reasons)
    if _quality_flags(raw):
        _append_reason(reasons, "mtvclc_entry_quote_quality_flags_present")
    if raw.get("transport_fresh") is not True:
        _append_reason(reasons, "mtvclc_entry_quote_transport_not_fresh")
    if raw.get("source_event_baseline_initialized") is not True:
        _append_reason(reasons, "mtvclc_entry_quote_source_event_baseline_missing")

    bid = _positive(raw.get("bid"))
    ask = _positive(raw.get("ask"))
    if bid is None or ask is None or ask < bid:
        _append_reason(reasons, "mtvclc_entry_quote_prices_invalid")
    received_at = _positive(raw.get("received_at_epoch"))
    now = _positive(as_of_epoch)
    max_age = _finite(max_tick_age_secs)
    if now is None:
        _append_reason(reasons, "mtvclc_entry_quote_clock_invalid")
    if max_age is None or max_age < 0.0 or max_age > MAX_QUOTE_GAP_SECONDS:
        _append_reason(reasons, "mtvclc_entry_quote_max_age_invalid")
        max_age = None
    quote_age: float | None = None
    if received_at is None:
        _append_reason(reasons, "mtvclc_entry_quote_transport_time_invalid")
    elif now is not None:
        quote_age = now - received_at
        if quote_age < 0.0:
            _append_reason(reasons, "mtvclc_entry_quote_from_future")
        elif max_age is not None and quote_age > max_age:
            _append_reason(reasons, "mtvclc_entry_quote_stale")
        if (
            proposal.expected_entry_epoch is None
            or received_at < proposal.expected_entry_epoch
        ):
            _append_reason(reasons, "mtvclc_entry_quote_before_entry_window")

    source_event_token = str(raw.get("source_event_token") or "").strip()
    token_invalid = (
        not source_event_token
        or len(source_event_token) > 128
        or not source_event_token.isascii()
        or not source_event_token.isdecimal()
    )
    if not token_invalid:
        try:
            token_invalid = int(source_event_token) <= 0
        except (ValueError, OverflowError):
            token_invalid = True
    if token_invalid:
        _append_reason(reasons, "mtvclc_entry_quote_source_event_token_invalid")
    sequence = raw.get("market_event_sequence")
    if type(sequence) is not int or sequence < 0:
        _append_reason(reasons, "mtvclc_entry_quote_market_event_sequence_invalid")
        sequence = None
    market_received_raw = raw.get("market_event_received_at_epoch")
    market_received = (
        _positive(market_received_raw) if market_received_raw is not None else None
    )
    if market_received_raw is not None and (
        market_received is None
        or (received_at is not None and market_received > received_at)
    ):
        _append_reason(reasons, "mtvclc_entry_quote_market_event_receipt_invalid")
    if sequence is not None and (
        (sequence == 0) != (market_received_raw is None)
    ):
        _append_reason(
            reasons,
            "mtvclc_entry_quote_market_event_receipt_sequence_mismatch",
        )

    mid: float | None = None
    spread_bps: float | None = None
    if bid is not None and ask is not None:
        mid = bid + (ask - bid) / 2.0
        spread_bps = (ask - bid) / mid * 1e4
        if spread_bps > cost.p90_spread_bps + _ABS_TOLERANCE:
            _append_reason(reasons, "mtvclc_entry_quote_spread_above_frozen_p90")
    if reasons:
        return _result(
            qualified=qualified_candidate,
            refreshed=None,
            reasons=tuple(reasons),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            market_source_id=source_id,
            quote_timestamp_epoch=received_at,
            as_of_epoch=now,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            current_spread_bps=spread_bps,
        )

    assert bid is not None
    assert ask is not None
    assert mid is not None
    assert spread_bps is not None
    assert received_at is not None
    assert now is not None
    assert max_age is not None
    assert quote_age is not None
    assert sequence is not None
    assert proposal.side in _SIDES
    entry_price = ask if proposal.side == "BUY" else bid
    recorded_cost = float(cost.recorded_cost_bps)
    target_bps = TARGET_COST_MULTIPLE * recorded_cost
    stop_bps = STOP_COST_MULTIPLE * recorded_cost
    target_distance = entry_price * target_bps / 1e4
    stop_distance = entry_price * stop_bps / 1e4
    if proposal.side == "BUY":
        sl_price = entry_price - stop_distance
        tp_price = entry_price + target_distance
        adverse_slippage = max(
            0.0,
            (entry_price - float(proposal.entry_price or entry_price))
            / entry_price
            * 1e4,
        )
        geometry_valid = 0.0 < sl_price < entry_price < tp_price
    else:
        sl_price = entry_price + stop_distance
        tp_price = entry_price - target_distance
        adverse_slippage = max(
            0.0,
            (float(proposal.entry_price or entry_price) - entry_price)
            / entry_price
            * 1e4,
        )
        geometry_valid = 0.0 < tp_price < entry_price < sl_price

    fixed_non_spread = (
        float(cost.commission_bps_per_round_trip)
        + float(cost.financing_bps_per_trade)
        + float(cost.adverse_execution_debit_bps)
    )
    current_total_cost = spread_bps + fixed_non_spread
    conversion = float(cost.convert_on_close_charge_fraction)
    denominator = (
        target_bps * (1.0 - conversion)
        + stop_bps * (1.0 + conversion)
    )
    lower = float(qualified_candidate.win_probability_lower_bound)
    if not math.isfinite(denominator) or denominator <= 0.0:
        return _result(
            qualified=qualified_candidate,
            refreshed=None,
            reasons=("mtvclc_entry_quote_current_cost_dead",),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            market_source_id=source_id,
            quote_timestamp_epoch=received_at,
            as_of_epoch=now,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            current_spread_bps=spread_bps,
            fixed_non_spread_cost_bps=fixed_non_spread,
            current_total_cost_bps=current_total_cost,
        )
    live_p_star = (
        stop_bps * (1.0 + conversion) + current_total_cost
    ) / denominator
    expected_edge = (
        lower * target_bps * (1.0 - conversion)
        - (1.0 - lower) * stop_bps * (1.0 + conversion)
        - current_total_cost
    )
    payoff_values = (
        entry_price,
        sl_price,
        tp_price,
        target_distance,
        stop_distance,
        fixed_non_spread,
        current_total_cost,
        live_p_star,
        expected_edge,
    )
    if (
        not geometry_valid
        or any(not math.isfinite(value) for value in payoff_values)
        or denominator <= 0.0
        or not 0.0 < live_p_star < 1.0
        or lower <= live_p_star
        or expected_edge <= 0.0
    ):
        return _result(
            qualified=qualified_candidate,
            refreshed=None,
            reasons=("mtvclc_entry_quote_current_cost_dead",),
            tick_symbol=tick_symbol,
            tick_venue_id=tick_venue,
            market_source_id=source_id,
            quote_timestamp_epoch=received_at,
            as_of_epoch=now,
            max_tick_age_secs=max_age,
            quote_age_secs=quote_age,
            bid_price=bid,
            ask_price=ask,
            mid_price=mid,
            current_spread_bps=spread_bps,
            fixed_non_spread_cost_bps=fixed_non_spread,
            current_total_cost_bps=current_total_cost,
            live_p_star=live_p_star,
            conservative_expected_edge_bps=expected_edge,
        )

    refreshed = RefreshedMTVCLCEntryCandidate(
        qualified_candidate=qualified_candidate,
        quote_timestamp_epoch=received_at,
        quote_age_secs=quote_age,
        market_source_id=source_id,
        source_event_token_sha256=hashlib.sha256(
            source_event_token.encode("ascii")
        ).hexdigest(),
        market_event_sequence=sequence,
        bid_price=bid,
        ask_price=ask,
        mid_price=mid,
        refreshed_entry_price=entry_price,
        refreshed_sl_price=sl_price,
        refreshed_tp_price=tp_price,
        stop_distance_price=stop_distance,
        target_distance_price=target_distance,
        stop_bps=stop_bps,
        target_bps=target_bps,
        current_spread_bps=spread_bps,
        fixed_non_spread_cost_bps=fixed_non_spread,
        current_total_cost_bps=current_total_cost,
        conversion_charge_fraction=conversion,
        live_p_star=live_p_star,
        conservative_expected_edge_bps=expected_edge,
        reward_risk_ratio=target_bps / stop_bps,
        adverse_slippage_bps=adverse_slippage,
    )
    return _result(
        qualified=qualified_candidate,
        refreshed=refreshed,
        reasons=(),
        tick_symbol=tick_symbol,
        tick_venue_id=tick_venue,
        market_source_id=source_id,
        quote_timestamp_epoch=received_at,
        as_of_epoch=now,
        max_tick_age_secs=max_age,
        quote_age_secs=quote_age,
        bid_price=bid,
        ask_price=ask,
        mid_price=mid,
        current_spread_bps=spread_bps,
        fixed_non_spread_cost_bps=fixed_non_spread,
        current_total_cost_bps=current_total_cost,
        live_p_star=live_p_star,
        conservative_expected_edge_bps=expected_edge,
    )


__all__ = [
    "MTVCLC_ENTRY_QUOTE_SCHEMA_VERSION",
    "MTVCLCEntryQuoteDiagnostics",
    "MTVCLCEntryQuoteRefreshResult",
    "RefreshedMTVCLCEntryCandidate",
    "refresh_mtvclc_entry_quote",
]
