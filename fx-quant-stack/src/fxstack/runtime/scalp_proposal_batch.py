# AGENT: ROLE: Pure production adapter from bridge M1 rows to ranked scalp proposals.
# AGENT: ENTRYPOINT: `evaluate_dislocation_batch`.
# AGENT: PRIMARY INPUTS: exact IG MT4 universe, versioned bridge rows, as-of time, policy.
# AGENT: PRIMARY OUTPUTS: ranked unqualified proposals plus immutable diagnostics.
# AGENT: STATE / SIDE EFFECTS: none; no settings, I/O, sizing, reservation, queue, or command access.
"""Align raw IG MT4 bridge M1 rows for pure dislocation evaluation.

The adapter converts only an exact 22-symbol input scope and derives one common
target minute from ``as_of_epoch``.  Structural M1 availability is assessed per
symbol so a closed or temporarily incomplete market abstains without preventing
other structurally ready symbols from being evaluated.  It cannot size or
reserve risk, persist state, enqueue commands, or confer execution
qualification.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import math
from typing import Any

from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    IG_MT4_VENUE_ID,
)
from fxstack.schemas.entry import EntryBar, EntryEvaluationRequest, EntryProposal
from fxstack.strategy.scalp_dislocation import (
    DislocationPolicy,
    evaluate_dislocation,
)


SCALP_PROPOSAL_BATCH_SCHEMA_VERSION = "fxstack.runtime.scalp_proposal_batch.v1"
BRIDGE_M1_BAR_SOURCE_ID = "mt4_bridge.market_bars.m1"
BRIDGE_M1_BAR_SOURCE_VERSION = "v1"
M1_SECONDS = 60


@dataclass(frozen=True, slots=True)
class ScalpSymbolProposalDiagnostic:
    symbol: str
    structural_ready: bool
    structural_reasons: tuple[str, ...]
    raw_row_count: int
    filtered_current_bar_count: int
    finalized_row_count: int
    selected_history_count: int
    latest_finalized_minute_epoch: int | None
    evaluation_allowed: bool | None = None
    evaluation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScalpProposalBatchDiagnostics:
    accepted: bool
    reasons: tuple[str, ...]
    source_id: str
    source_version: str
    as_of_epoch: float | None
    current_minute_epoch: int | None
    common_closed_minute_epoch: int | None
    expected_symbols: tuple[str, ...]
    observed_symbols: tuple[str, ...]
    filtered_current_bar_count: int
    symbol_diagnostics: tuple[ScalpSymbolProposalDiagnostic, ...]
    schema_version: str = SCALP_PROPOSAL_BATCH_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ScalpProposalBatchResult:
    proposals: tuple[EntryProposal, ...]
    diagnostics: ScalpProposalBatchDiagnostics

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _PreparedSymbol:
    request: EntryEvaluationRequest
    diagnostic: ScalpSymbolProposalDiagnostic


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _parse_epoch(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        seconds = value.timestamp()
    elif isinstance(value, (int, float)):
        seconds = float(value)
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            seconds = float(text)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            seconds = parsed.astimezone(timezone.utc).timestamp()
    if not math.isfinite(seconds) or seconds <= 0.0:
        return None
    rounded = round(seconds)
    if abs(seconds - rounded) > 1e-6:
        return None
    return int(rounded)


def _quality_flags(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw = row.get("quality_flags")
    if raw is None:
        return ()
    if isinstance(raw, str):
        values: Sequence[Any] = (raw,)
    elif isinstance(raw, Sequence):
        values = raw
    else:
        return (str(raw),)
    return tuple(str(value or "").strip() for value in values if str(value or "").strip())


def _normalize_universe(
    raw_bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
) -> tuple[dict[str, Sequence[Mapping[str, Any]]], tuple[str, ...]]:
    normalized: dict[str, Sequence[Mapping[str, Any]]] = {}
    duplicates: list[str] = []
    for raw_symbol, rows in raw_bars_by_symbol.items():
        symbol = str(raw_symbol or "").strip().upper()
        if symbol in normalized:
            duplicates.append(symbol)
            continue
        normalized[symbol] = rows
    return normalized, tuple(sorted(set(duplicates)))


def _global_refusal(
    *,
    reasons: tuple[str, ...],
    source_id: str,
    source_version: str,
    as_of_epoch: float | None,
    current_minute_epoch: int | None,
    common_closed_minute_epoch: int | None,
    observed_symbols: tuple[str, ...],
    symbol_diagnostics: tuple[ScalpSymbolProposalDiagnostic, ...] = (),
) -> ScalpProposalBatchResult:
    return ScalpProposalBatchResult(
        proposals=(),
        diagnostics=ScalpProposalBatchDiagnostics(
            accepted=False,
            reasons=reasons,
            source_id=source_id,
            source_version=source_version,
            as_of_epoch=as_of_epoch,
            current_minute_epoch=current_minute_epoch,
            common_closed_minute_epoch=common_closed_minute_epoch,
            expected_symbols=IG_MT4_SCALP_SYMBOLS,
            observed_symbols=observed_symbols,
            filtered_current_bar_count=sum(
                item.filtered_current_bar_count for item in symbol_diagnostics
            ),
            symbol_diagnostics=symbol_diagnostics,
        ),
    )


def _entry_bar_from_row(
    *,
    row: Mapping[str, Any],
    symbol: str,
    minute_epoch: int,
    source_id: str,
    source_version: str,
) -> tuple[EntryBar | None, tuple[str, ...]]:
    reasons: list[str] = []
    provider = str(row.get("provider") or "").strip().lower()
    if provider and provider not in {"mt4", "mt4_bridge"}:
        reasons.append("bar_provider_mismatch")
    canonical_symbol = str(row.get("canonical_symbol") or row.get("pair") or "").strip().upper()
    if canonical_symbol and canonical_symbol != symbol:
        reasons.append("bar_symbol_mismatch")
    venue = str(row.get("venue") or "").strip().lower()
    if venue and venue != IG_MT4_VENUE_ID:
        reasons.append("bar_venue_mismatch")
    timeframe = str(row.get("timeframe") or "").strip().upper()
    if timeframe and timeframe != "M1":
        reasons.append("bar_timeframe_mismatch")
    if _quality_flags(row):
        reasons.append("bar_quality_flags_present")

    open_px = _finite_float(row.get("mid_open", row.get("open")))
    high_px = _finite_float(row.get("mid_high", row.get("high")))
    low_px = _finite_float(row.get("mid_low", row.get("low")))
    close_px = _finite_float(row.get("mid_close", row.get("close")))
    bid_close = _finite_float(row.get("bid_close"))
    ask_close = _finite_float(row.get("ask_close"))
    prices = (open_px, high_px, low_px, close_px)
    if any(value is None or value <= 0.0 for value in prices):
        reasons.append("bar_mid_prices_invalid")
    if bid_close is None or ask_close is None or bid_close <= 0.0 or ask_close <= 0.0:
        reasons.append("bar_two_sided_close_missing")
    elif ask_close < bid_close:
        reasons.append("bar_two_sided_close_invalid")
    if all(value is not None and value > 0.0 for value in prices):
        assert open_px is not None
        assert high_px is not None
        assert low_px is not None
        assert close_px is not None
        if (
            high_px < max(open_px, close_px)
            or low_px > min(open_px, close_px)
            or high_px < low_px
        ):
            reasons.append("bar_geometry_invalid")
    if reasons:
        return None, tuple(reasons)

    assert open_px is not None
    assert high_px is not None
    assert low_px is not None
    assert close_px is not None
    assert bid_close is not None
    assert ask_close is not None
    return (
        EntryBar(
            symbol=symbol,
            venue_id=IG_MT4_VENUE_ID,
            source_id=source_id,
            source_version=source_version,
            minute_epoch=minute_epoch,
            bar_seconds=M1_SECONDS,
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            bid_close=bid_close,
            ask_close=ask_close,
            # Finality is derived from timestamp/as-of in `_prepare_symbol`.
            # A raw `closed` field is deliberately ignored.
            closed=True,
            quality_flags=(),
        ),
        (),
    )


def _prepare_symbol(
    *,
    symbol: str,
    raw_rows: Sequence[Mapping[str, Any]],
    required_history_bars: int,
    as_of_epoch: float,
    current_minute_epoch: int,
    common_closed_minute_epoch: int,
    source_id: str,
    source_version: str,
) -> _PreparedSymbol:
    reasons: list[str] = []
    parsed_finalized: dict[int, Mapping[str, Any]] = {}
    filtered_current = 0
    raw_count = len(raw_rows) if isinstance(raw_rows, Sequence) else 0

    if isinstance(raw_rows, (str, bytes)) or not isinstance(raw_rows, Sequence):
        reasons.append("bar_rows_invalid")
        raw_rows = ()

    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            if "bar_row_invalid" not in reasons:
                reasons.append("bar_row_invalid")
            continue
        minute_epoch = _parse_epoch(raw_row.get("time", raw_row.get("ts")))
        if minute_epoch is None:
            if "bar_time_invalid" not in reasons:
                reasons.append("bar_time_invalid")
            continue
        if minute_epoch % M1_SECONDS != 0:
            if "bar_time_not_m1_aligned" not in reasons:
                reasons.append("bar_time_not_m1_aligned")
            continue
        if minute_epoch == current_minute_epoch:
            filtered_current += 1
            continue
        if minute_epoch > current_minute_epoch or minute_epoch + M1_SECONDS > as_of_epoch:
            if "unfinalized_bar_present" not in reasons:
                reasons.append("unfinalized_bar_present")
            continue
        if minute_epoch in parsed_finalized:
            if "duplicate_finalized_bar_minute" not in reasons:
                reasons.append("duplicate_finalized_bar_minute")
            continue
        parsed_finalized[minute_epoch] = raw_row

    finalized_epochs = sorted(parsed_finalized)
    latest_finalized = finalized_epochs[-1] if finalized_epochs else None
    if common_closed_minute_epoch not in parsed_finalized:
        reasons.append("common_closed_minute_missing")
    if len(parsed_finalized) < required_history_bars:
        reasons.append("insufficient_finalized_history")

    expected_epochs = tuple(
        common_closed_minute_epoch - offset * M1_SECONDS
        for offset in reversed(range(required_history_bars))
    )
    if len(parsed_finalized) >= required_history_bars and any(
        epoch not in parsed_finalized for epoch in expected_epochs
    ):
        reasons.append("non_consecutive_finalized_history")

    entry_bars: list[EntryBar] = []
    if not reasons:
        for minute_epoch in expected_epochs:
            entry_bar, row_reasons = _entry_bar_from_row(
                row=parsed_finalized[minute_epoch],
                symbol=symbol,
                minute_epoch=minute_epoch,
                source_id=source_id,
                source_version=source_version,
            )
            if row_reasons:
                for reason in row_reasons:
                    if reason not in reasons:
                        reasons.append(reason)
                continue
            assert entry_bar is not None
            entry_bars.append(entry_bar)

    structural_ready = not reasons and len(entry_bars) == required_history_bars
    diagnostic = ScalpSymbolProposalDiagnostic(
        symbol=symbol,
        structural_ready=structural_ready,
        structural_reasons=tuple(reasons),
        raw_row_count=raw_count,
        filtered_current_bar_count=filtered_current,
        finalized_row_count=len(parsed_finalized),
        selected_history_count=len(entry_bars),
        latest_finalized_minute_epoch=latest_finalized,
    )
    spread_bps = 0.0
    if entry_bars:
        last = entry_bars[-1]
        spread_bps = (last.ask_close - last.bid_close) / last.close * 1e4
    return _PreparedSymbol(
        request=EntryEvaluationRequest(
            symbol=symbol,
            bars=tuple(entry_bars),
            spread_bps=spread_bps,
        ),
        diagnostic=diagnostic,
    )


def _proposal_rank_key(proposal: EntryProposal) -> tuple[float, float, float, str, str]:
    p_star = _finite_float(proposal.p_star)
    disp_z = _finite_float(proposal.disp_z)
    spread_bps = _finite_float(proposal.spread_bps)
    return (
        p_star if p_star is not None else math.inf,
        -abs(disp_z) if disp_z is not None else math.inf,
        spread_bps if spread_bps is not None else math.inf,
        str(proposal.symbol or "").strip().upper(),
        str(proposal.side or "").strip().upper(),
    )


def evaluate_dislocation_batch(
    *,
    raw_bars_by_symbol: Mapping[str, Sequence[Mapping[str, Any]]],
    as_of_epoch: float,
    source_id: str,
    source_version: str,
    policy: DislocationPolicy,
) -> ScalpProposalBatchResult:
    """Evaluate and rank one exact, common-minute IG MT4 proposal batch.

    The current M1 bucket is removed and reported because it is not finalized.
    Exact universe membership, source identity, policy, and the common target
    minute are batch invariants.  Each symbol must independently provide exact
    consecutive history ending at that minute to reach strategy evaluation;
    structurally unready symbols abstain with explicit diagnostics while ready
    symbols continue.  Per-strategy refusals are also ordinary diagnostics.
    """

    normalized_source_id = str(source_id or "").strip()
    normalized_source_version = str(source_version or "").strip()
    observed: tuple[str, ...] = ()
    if not isinstance(raw_bars_by_symbol, Mapping):
        return _global_refusal(
            reasons=("bar_universe_invalid",),
            source_id=normalized_source_id,
            source_version=normalized_source_version,
            as_of_epoch=_finite_float(as_of_epoch),
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_symbols=observed,
        )
    normalized, duplicates = _normalize_universe(raw_bars_by_symbol)
    observed = tuple(sorted(symbol for symbol in normalized if symbol))

    source_reasons: list[str] = []
    if not normalized_source_id:
        source_reasons.append("batch_source_id_missing")
    elif normalized_source_id != BRIDGE_M1_BAR_SOURCE_ID:
        source_reasons.append("batch_source_id_unsupported")
    if not normalized_source_version:
        source_reasons.append("batch_source_version_missing")
    elif normalized_source_version != BRIDGE_M1_BAR_SOURCE_VERSION:
        source_reasons.append("batch_source_version_unsupported")
    if source_reasons:
        return _global_refusal(
            reasons=tuple(source_reasons),
            source_id=normalized_source_id,
            source_version=normalized_source_version,
            as_of_epoch=_finite_float(as_of_epoch),
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_symbols=observed,
        )

    as_of = _finite_float(as_of_epoch)
    if as_of is None or as_of <= M1_SECONDS:
        return _global_refusal(
            reasons=("as_of_epoch_invalid",),
            source_id=normalized_source_id,
            source_version=normalized_source_version,
            as_of_epoch=as_of,
            current_minute_epoch=None,
            common_closed_minute_epoch=None,
            observed_symbols=observed,
        )
    current_minute = int(as_of // M1_SECONDS) * M1_SECONDS
    common_closed_minute = current_minute - M1_SECONDS

    expected = set(IG_MT4_SCALP_SYMBOLS)
    observed_set = set(normalized)
    universe_reasons: list[str] = []
    if duplicates:
        universe_reasons.extend(f"duplicate_universe_symbol:{symbol}" for symbol in duplicates)
    universe_reasons.extend(
        f"missing_universe_symbol:{symbol}"
        for symbol in IG_MT4_SCALP_SYMBOLS
        if symbol not in observed_set
    )
    universe_reasons.extend(
        f"extra_universe_symbol:{symbol}"
        for symbol in sorted(observed_set - expected)
    )
    if universe_reasons:
        return _global_refusal(
            reasons=tuple(universe_reasons),
            source_id=normalized_source_id,
            source_version=normalized_source_version,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            observed_symbols=observed,
        )

    try:
        required_history_bars = int(policy.min_history_bars)
    except (TypeError, ValueError, OverflowError):
        required_history_bars = 0
    if required_history_bars < 2:
        return _global_refusal(
            reasons=("batch_policy_history_invalid",),
            source_id=normalized_source_id,
            source_version=normalized_source_version,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            observed_symbols=observed,
        )

    prepared = tuple(
        _prepare_symbol(
            symbol=symbol,
            raw_rows=normalized[symbol],
            required_history_bars=required_history_bars,
            as_of_epoch=as_of,
            current_minute_epoch=current_minute,
            common_closed_minute_epoch=common_closed_minute,
            source_id=normalized_source_id,
            source_version=normalized_source_version,
        )
        for symbol in IG_MT4_SCALP_SYMBOLS
    )
    allowed: list[EntryProposal] = []
    evaluated_diagnostics: list[ScalpSymbolProposalDiagnostic] = []
    for item in prepared:
        if not item.diagnostic.structural_ready:
            evaluated_diagnostics.append(item.diagnostic)
            continue
        proposal = evaluate_dislocation(item.request, policy)
        evaluated_diagnostics.append(
            replace(
                item.diagnostic,
                evaluation_allowed=proposal.allowed,
                evaluation_reasons=proposal.reasons,
            )
        )
        if proposal.allowed:
            allowed.append(proposal)
    ranked = tuple(sorted(allowed, key=_proposal_rank_key))
    diagnostics = ScalpProposalBatchDiagnostics(
        accepted=True,
        reasons=(),
        source_id=normalized_source_id,
        source_version=normalized_source_version,
        as_of_epoch=as_of,
        current_minute_epoch=current_minute,
        common_closed_minute_epoch=common_closed_minute,
        expected_symbols=IG_MT4_SCALP_SYMBOLS,
        observed_symbols=observed,
        filtered_current_bar_count=sum(
            item.filtered_current_bar_count for item in evaluated_diagnostics
        ),
        symbol_diagnostics=tuple(evaluated_diagnostics),
    )
    return ScalpProposalBatchResult(proposals=ranked, diagnostics=diagnostics)


__all__ = [
    "BRIDGE_M1_BAR_SOURCE_ID",
    "BRIDGE_M1_BAR_SOURCE_VERSION",
    "M1_SECONDS",
    "SCALP_PROPOSAL_BATCH_SCHEMA_VERSION",
    "ScalpProposalBatchDiagnostics",
    "ScalpProposalBatchResult",
    "ScalpSymbolProposalDiagnostic",
    "evaluate_dislocation_batch",
]
