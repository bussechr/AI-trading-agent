# AGENT: ROLE: Pure production-owned dislocation candidate evaluator.
# AGENT: ENTRYPOINT: `evaluate_dislocation(request, policy)`.
# AGENT: PRIMARY INPUTS: typed closed-bar request, immutable dislocation policy, IG MT4 catalog.
# AGENT: PRIMARY OUTPUTS: unqualified `EntryProposal` with ordered refusal reasons.
# AGENT: STATE / SIDE EFFECTS: none; never sizes, persists, enqueues, activates, or executes.
"""Versioned production proposal logic for the dislocation control strategy.

This is deliberately only a proposal layer.  ``allowed=True`` means the pure
signal and bracket arithmetic produced a candidate; it does not confer risk,
portfolio, frequency, sizing, queue, or broker authority.  The strategy has
no calibrated probability model, so every result keeps ``win_probability``
unset and the proposal qualification remains ``candidate_unqualified``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any, Literal

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SCOPE_VERSION,
    IG_MT4_VENUE_ID,
    IgMt4InstrumentIdentity,
    get_ig_mt4_instrument,
)
from fxstack.schemas.entry import (
    ENTRY_BAR_SCHEMA_VERSION,
    IMMEDIATE_ENTRY_MAX_DELAY_SECONDS,
    EntryBar,
    EntryEvaluationRequest,
    EntryProposal,
    EntrySide,
)
from fxstack.runtime.scalp_rollover_guard import (
    DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY,
    PRODUCTION_SCALP_MAX_TIME_STOP_BARS,
)


SCALP_DISLOCATION_STRATEGY_ID = "scalp_dislocation"
SCALP_DISLOCATION_STRATEGY_VERSION = "fxstack.strategy.scalp_dislocation.v5"
SCALP_DISLOCATION_POLICY_SCHEMA_VERSION = (
    "fxstack.strategy.scalp_dislocation.policy.v5"
)
SCALP_EXECUTION_DEBIT_BPS = 1.0


@dataclass(frozen=True, slots=True)
class DislocationPolicy:
    """Immutable parameters for the versioned dislocation evaluator."""

    signal_mode: Literal["revert", "momentum"] = "revert"
    min_history_bars: int = 30
    ema_bars: int = 20
    atr_bars: int = 14
    atr_floor_bps: float = 0.3
    z_entry: float = 2.0
    tp_atr_mult: float = 1.5
    sl_atr_mult: float = 1.0
    min_stop_bps: float = 4.5
    execution_debit_bps: float = SCALP_EXECUTION_DEBIT_BPS
    target_cost_multiple: float = 4.0
    stop_cost_multiple: float = 8.0
    p_star_max: float = 0.80
    min_tp_cost_ratio: float = 0.0
    time_stop_bars: int = 5

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the complete versioned config payload used for hashing."""

        return {
            "policy_schema_version": SCALP_DISLOCATION_POLICY_SCHEMA_VERSION,
            "scope_version": IG_MT4_SCALP_SCOPE_VERSION,
            "production_rollover_guard": (
                DEFAULT_PRODUCTION_SCALP_ROLLOVER_POLICY.to_canonical_dict()
            ),
            "signal_mode": self.signal_mode,
            "min_history_bars": self.min_history_bars,
            "ema_bars": self.ema_bars,
            "atr_bars": self.atr_bars,
            "atr_floor_bps": self.atr_floor_bps,
            "z_entry": self.z_entry,
            "tp_atr_mult": self.tp_atr_mult,
            "sl_atr_mult": self.sl_atr_mult,
            "min_stop_bps": self.min_stop_bps,
            "execution_debit_bps": self.execution_debit_bps,
            "target_cost_multiple": self.target_cost_multiple,
            "stop_cost_multiple": self.stop_cost_multiple,
            "p_star_max": self.p_star_max,
            "min_tp_cost_ratio": self.min_tp_cost_ratio,
            "time_stop_bars": self.time_stop_bars,
        }

    def config_sha256(self) -> str:
        """Stable SHA-256 of the complete policy, including schema identity."""

        payload = _canonical_hash_value(self.to_canonical_dict())
        encoded = json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _canonical_hash_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_hash_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_hash_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return {"non_finite_float": "nan"}
        return {"non_finite_float": "+inf" if value > 0.0 else "-inf"}
    return value


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _strict_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    try:
        if float(value) != float(number):
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def _append_reason(reasons: list[str], reason: str) -> None:
    if reason not in reasons:
        reasons.append(reason)


def _policy_reasons(policy: DislocationPolicy) -> tuple[str, ...]:
    reasons: list[str] = []
    if policy.signal_mode not in {"revert", "momentum"}:
        reasons.append("invalid_signal_mode")
    for name, minimum in (
        ("min_history_bars", 2),
        ("ema_bars", 1),
        ("atr_bars", 1),
        ("time_stop_bars", 1),
    ):
        int_value = _strict_int(getattr(policy, name))
        if int_value is None or int_value < minimum:
            reasons.append(f"invalid_policy_{name}")
    if (
        _strict_int(policy.time_stop_bars) is not None
        and int(policy.time_stop_bars) > PRODUCTION_SCALP_MAX_TIME_STOP_BARS
    ):
        reasons.append("invalid_policy_time_stop_exceeds_rollover_guard")
    for name, allow_zero in (
        ("atr_floor_bps", True),
        ("z_entry", True),
        ("tp_atr_mult", False),
        ("sl_atr_mult", False),
        ("min_stop_bps", True),
        ("execution_debit_bps", True),
        ("target_cost_multiple", False),
        ("stop_cost_multiple", False),
        ("min_tp_cost_ratio", True),
    ):
        float_value = _finite_float(getattr(policy, name))
        if float_value is None or float_value < 0.0 or (
            not allow_zero and float_value == 0.0
        ):
            reasons.append(f"invalid_policy_{name}")
    p_star_max = _finite_float(policy.p_star_max)
    if p_star_max is None or not 0.0 < p_star_max <= 1.0:
        reasons.append("invalid_policy_p_star_max")
    return tuple(reasons)


def _bar_validation_reasons(
    *,
    bars: tuple[EntryBar, ...],
    symbol: str,
    venue_id: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    normalized_sources: list[tuple[str, str]] = []
    epochs: list[int] = []
    bar_seconds_values: list[int] = []

    for bar in bars:
        if str(bar.schema_version) != ENTRY_BAR_SCHEMA_VERSION:
            _append_reason(reasons, "bar_schema_version_invalid")
        if str(bar.symbol or "").strip().upper() != symbol:
            _append_reason(reasons, "bar_symbol_mismatch")
        if str(bar.venue_id or "").strip().lower() != venue_id:
            _append_reason(reasons, "bar_venue_mismatch")

        source_id = str(bar.source_id or "").strip()
        source_version = str(bar.source_version or "").strip()
        if not source_id:
            _append_reason(reasons, "bar_source_identity_missing")
        if not source_version:
            _append_reason(reasons, "bar_source_version_missing")
        normalized_sources.append((source_id, source_version))

        if bar.closed is not True:
            _append_reason(reasons, "bar_not_closed")
        if any(str(flag or "").strip() for flag in tuple(bar.quality_flags or ())):
            _append_reason(reasons, "bar_quality_flags_present")

        minute_epoch = _strict_int(bar.minute_epoch)
        bar_seconds = _strict_int(bar.bar_seconds)
        if minute_epoch is None or minute_epoch < 0:
            _append_reason(reasons, "bar_time_invalid")
        else:
            epochs.append(minute_epoch)
        if bar_seconds is None or bar_seconds <= 0:
            _append_reason(reasons, "bar_timeframe_invalid")
        else:
            bar_seconds_values.append(bar_seconds)

        prices = tuple(
            _finite_float(value)
            for value in (
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.bid_close,
                bar.ask_close,
            )
        )
        if any(value is None or value <= 0.0 for value in prices):
            _append_reason(reasons, "bar_prices_invalid")
            continue
        open_px, high_px, low_px, close_px, bid_close, ask_close = (
            float(value) for value in prices if value is not None
        )
        if (
            low_px > min(open_px, close_px)
            or high_px < max(open_px, close_px)
            or high_px < low_px
        ):
            _append_reason(reasons, "bar_geometry_invalid")
        if ask_close < bid_close:
            _append_reason(reasons, "bar_quote_invalid")

    if normalized_sources and len(set(normalized_sources)) != 1:
        _append_reason(reasons, "mixed_bar_source_identity")
    if len(epochs) == len(bars) and len(bar_seconds_values) == len(bars):
        step = bar_seconds_values[0]
        if any(value != step for value in bar_seconds_values):
            _append_reason(reasons, "non_consecutive_closed_bars")
        elif any(epoch % step != 0 for epoch in epochs):
            _append_reason(reasons, "non_consecutive_closed_bars")
        elif any(current - previous != step for previous, current in zip(epochs, epochs[1:])):
            _append_reason(reasons, "non_consecutive_closed_bars")
    return tuple(reasons)


def _atr_bps(bars: tuple[EntryBar, ...], *, periods: int) -> float:
    if len(bars) < 2:
        return 0.0
    true_ranges: list[float] = []
    for previous, current in zip(bars, bars[1:]):
        true_range = max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        true_ranges.append(true_range / current.close * 1e4)
    tail = true_ranges[-max(1, int(periods)) :]
    return sum(tail) / len(tail)


def _ema(values: list[float], *, periods: int) -> float:
    if not values:
        return 0.0
    weight = 2.0 / (float(max(1, periods)) + 1.0)
    average = values[0]
    for value in values[1:]:
        average = value * weight + average * (1.0 - weight)
    return average


def _proposal(
    *,
    request: EntryEvaluationRequest,
    policy: DislocationPolicy,
    instrument: IgMt4InstrumentIdentity | None,
    allowed: bool,
    reasons: tuple[str, ...],
    side: EntrySide | None = None,
    ref_mid: float | None = None,
    entry_price: float | None = None,
    sl_price: float | None = None,
    tp_price: float | None = None,
    atr_bps: float | None = None,
    stop_bps: float | None = None,
    target_bps: float | None = None,
    disp_z: float | None = None,
    p_star: float | None = None,
) -> EntryProposal:
    bars = tuple(request.bars or ())
    last = bars[-1] if bars else None
    spread = _finite_float(request.spread_bps)
    minute_epoch = _strict_int(last.minute_epoch) if last is not None else None
    bar_seconds = _strict_int(last.bar_seconds) if last is not None else None
    entry_deadline_epoch = (
        minute_epoch + bar_seconds + IMMEDIATE_ENTRY_MAX_DELAY_SECONDS
        if minute_epoch is not None and bar_seconds is not None
        else None
    )
    return EntryProposal(
        strategy_id=SCALP_DISLOCATION_STRATEGY_ID,
        strategy_version=SCALP_DISLOCATION_STRATEGY_VERSION,
        config_sha256=policy.config_sha256(),
        symbol=(
            instrument.canonical_symbol
            if instrument is not None
            else str(request.symbol or "").strip().upper()
        ),
        instrument_id=instrument.instrument_id if instrument is not None else "",
        venue_id=instrument.venue if instrument is not None else "",
        source_id=str(last.source_id or "").strip() if last is not None else "",
        source_version=(
            str(last.source_version or "").strip() if last is not None else ""
        ),
        allowed=allowed,
        reasons=reasons,
        side=side,
        minute_epoch=minute_epoch,
        ref_mid=ref_mid,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        atr_bps=atr_bps,
        stop_bps=stop_bps,
        target_bps=target_bps,
        disp_z=disp_z,
        spread_bps=max(0.0, spread) if spread is not None else None,
        p_star=p_star,
        time_stop_bars=int(policy.time_stop_bars) if allowed else None,
        entry_deadline_epoch=entry_deadline_epoch,
    )


def evaluate_dislocation(
    request: EntryEvaluationRequest,
    policy: DislocationPolicy,
) -> EntryProposal:
    """Evaluate a versioned, execution-unqualified dislocation candidate.

    The signal and bracket calculations intentionally match the research
    control evaluator.  Production-specific validation is stricter: the full
    supplied history must be one exact run of finalized, quality-clean bars
    from one explicitly versioned source and the IG MT4 production catalog.
    """

    symbol = str(request.symbol or "").strip().upper()
    instrument = get_ig_mt4_instrument(symbol)
    policy_reasons = _policy_reasons(policy)
    if policy_reasons:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=policy_reasons,
        )
    if not symbol:
        return _proposal(
            request=request,
            policy=policy,
            instrument=None,
            allowed=False,
            reasons=("entry_symbol_missing",),
        )
    if instrument is None:
        return _proposal(
            request=request,
            policy=policy,
            instrument=None,
            allowed=False,
            reasons=("unsupported_ig_mt4_symbol",),
        )

    spread_bps = _finite_float(request.spread_bps)
    if spread_bps is None:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("spread_not_finite",),
        )

    bars = tuple(request.bars or ())
    if len(bars) < int(policy.min_history_bars):
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("insufficient_valid_history",),
        )
    bar_reasons = _bar_validation_reasons(
        bars=bars,
        symbol=instrument.canonical_symbol,
        venue_id=IG_MT4_VENUE_ID,
    )
    if bar_reasons:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=bar_reasons,
        )

    last = bars[-1]
    atr = _atr_bps(bars, periods=int(policy.atr_bars))
    if atr < float(policy.atr_floor_bps):
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("no_volatility_estimate",),
        )
    mean = _ema([bar.close for bar in bars], periods=int(policy.ema_bars))
    if mean <= 0.0 or last.close <= 0.0:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("degenerate_prices",),
        )
    disp_z = ((last.close - mean) / mean * 1e4) / atr
    if abs(disp_z) < float(policy.z_entry):
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("no_dislocation",),
            atr_bps=atr,
            disp_z=disp_z,
        )

    bar_direction = last.close - last.open
    if policy.signal_mode == "momentum":
        if (disp_z > 0.0 and bar_direction <= 0.0) or (
            disp_z < 0.0 and bar_direction >= 0.0
        ):
            return _proposal(
                request=request,
                policy=policy,
                instrument=instrument,
                allowed=False,
                reasons=("no_continuation_trigger",),
                atr_bps=atr,
                disp_z=disp_z,
            )
        side: EntrySide = "BUY" if disp_z > 0.0 else "SELL"
    else:
        if (disp_z > 0.0 and bar_direction >= 0.0) or (
            disp_z < 0.0 and bar_direction <= 0.0
        ):
            return _proposal(
                request=request,
                policy=policy,
                instrument=instrument,
                allowed=False,
                reasons=("no_reversion_trigger",),
                atr_bps=atr,
                disp_z=disp_z,
            )
        side = "SELL" if disp_z > 0.0 else "BUY"

    # A scalp bracket is priced from the actual trading cost, not from a wide
    # swing-style ATR reward multiple.  Target stays close, stop stays wider,
    # and the short time stop prevents a failed scalp becoming a held position.
    spread_cost_bps = max(0.0, spread_bps)
    recorded_cost_bps = spread_cost_bps + float(policy.execution_debit_bps)
    target_bps = float(policy.target_cost_multiple) * recorded_cost_bps
    stop_bps = max(
        float(policy.stop_cost_multiple) * recorded_cost_bps,
        float(policy.min_stop_bps),
    )
    if target_bps <= 0.0 or stop_bps <= 0.0:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("degenerate_bracket",),
            atr_bps=atr,
            stop_bps=stop_bps,
            target_bps=target_bps,
            disp_z=disp_z,
        )
    p_star = (stop_bps + recorded_cost_bps) / (target_bps + stop_bps)
    if p_star > float(policy.p_star_max):
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("bracket_cost_dead",),
            atr_bps=atr,
            stop_bps=stop_bps,
            target_bps=target_bps,
            disp_z=disp_z,
            p_star=p_star,
        )
    if (
        float(policy.min_tp_cost_ratio) > 0.0
        and recorded_cost_bps > 0.0
        and target_bps < float(policy.min_tp_cost_ratio) * recorded_cost_bps
    ):
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("gross_too_small_vs_cost",),
            atr_bps=atr,
            stop_bps=stop_bps,
            target_bps=target_bps,
            disp_z=disp_z,
            p_star=p_star,
        )

    mid = last.close
    entry_price = last.ask_close if side == "BUY" else last.bid_close
    if entry_price <= 0.0 or mid <= 0.0:
        return _proposal(
            request=request,
            policy=policy,
            instrument=instrument,
            allowed=False,
            reasons=("no_entry_quote",),
            atr_bps=atr,
            stop_bps=stop_bps,
            target_bps=target_bps,
            disp_z=disp_z,
            p_star=p_star,
        )
    stop_distance = stop_bps / 1e4 * mid
    target_distance = target_bps / 1e4 * mid
    if side == "BUY":
        sl_price = entry_price - stop_distance
        tp_price = entry_price + target_distance
    else:
        sl_price = entry_price + stop_distance
        tp_price = entry_price - target_distance

    return _proposal(
        request=request,
        policy=policy,
        instrument=instrument,
        allowed=True,
        reasons=(),
        side=side,
        ref_mid=mid,
        entry_price=entry_price,
        sl_price=sl_price,
        tp_price=tp_price,
        atr_bps=atr,
        stop_bps=stop_bps,
        target_bps=target_bps,
        disp_z=disp_z,
        p_star=p_star,
    )


__all__ = [
    "SCALP_DISLOCATION_POLICY_SCHEMA_VERSION",
    "SCALP_DISLOCATION_STRATEGY_ID",
    "SCALP_DISLOCATION_STRATEGY_VERSION",
    "DislocationPolicy",
    "evaluate_dislocation",
]
