from __future__ import annotations

import math
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from fxstack.live.policy import normalize_session_bucket
from fxstack._serialization import (
    copy_json_payload,
    json_safe as _json_safe,
    json_safe_dataclass,
)
from fxstack.providers.catalog import infer_instrument_ref


_EXPLICIT_EXPOSURE_FIELDS = (
    "exposure_units",
    "notional_units",
    "quote_notional",
    "notional",
    "exposure_notional",
    "gross_notional",
)
_REFERENCE_PRICE_FIELDS = (
    "mark_price",
    "mid",
    "price",
    "open_price",
    "close_price",
    "entry_price",
    "avg_price",
    "last_price",
)


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return float(number) if math.isfinite(number) else None


@lru_cache(maxsize=512)
def _book_instrument_ref(symbol: str) -> Any:
    """Reuse static instrument identity inside repeated book reconstruction."""

    return infer_instrument_ref(symbol)


def _position_side(row: dict[str, Any]) -> str:
    side = str(row.get("side") or "").strip().upper()
    if side in {"BUY", "SELL"}:
        return side
    cmd = str(row.get("cmd") or row.get("command") or "").strip().upper()
    if cmd in {"BUY", "SELL"}:
        return cmd
    raw_type = row.get("type")
    try:
        type_value = int(raw_type)
    except Exception:
        type_value = -1
    if type_value == 0:
        return "BUY"
    if type_value == 1:
        return "SELL"
    return ""


def _session_bucket(row: dict[str, Any]) -> str:
    bucket = normalize_session_bucket(row.get("session_bucket") or row.get("sessionBucket") or "")
    return "" if bucket == "unknown" else str(bucket)


def _entry_row(row: dict[str, Any]) -> dict[str, Any]:
    merged = dict(row or {})
    for key in ("payload", "approved_order", "command_preview"):
        nested = merged.get(key)
        if isinstance(nested, dict):
            combined = dict(merged)
            combined.update(dict(nested))
            return combined
    return merged


def _first_numeric(row: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if value in (None, ""):
            continue
        number = _finite_float(value)
        if number is not None:
            return float(number)
    return None


def _numeric_contract_errors(row: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in ("lots", *_EXPLICIT_EXPOSURE_FIELDS, "contract_size", "lot_size", *_REFERENCE_PRICE_FIELDS):
        if key not in row or row.get(key) in (None, ""):
            continue
        number = _finite_float(row.get(key))
        if number is None:
            errors.append(f"nonfinite:{key}")
        elif key == "lots" and number < 0.0:
            errors.append("negative:lots")
        elif key in {"contract_size", "lot_size", *_REFERENCE_PRICE_FIELDS} and number <= 0.0:
            errors.append(f"nonpositive:{key}")
    return errors


def _exposure_units(row: dict[str, Any], *, instrument: Any, lots: float) -> tuple[float, str]:
    metadata = row
    explicit = _first_numeric(
        metadata,
        *_EXPLICIT_EXPOSURE_FIELDS,
    )
    if explicit is not None:
        return abs(float(explicit)), "notional_units"

    contract_size = _first_numeric(metadata, "contract_size", "lot_size")
    if contract_size is None or contract_size <= 0.0:
        contract_size = _finite_float(getattr(instrument, "lot_size", 1.0))
    if contract_size is None or contract_size <= 0.0:
        contract_size = 1.0
    reference_price = _first_numeric(
        metadata,
        *_REFERENCE_PRICE_FIELDS,
    )
    if reference_price is not None and reference_price > 0.0:
        return abs(float(lots) * float(contract_size) * float(reference_price)), "notional_units"
    return abs(float(lots) * float(contract_size)), "base_units"


@dataclass(slots=True)
class BookPosition:
    symbol: str
    side: str
    lots: float
    signed_exposure: float
    exposure_units: float
    instrument_id: str
    asset_class: str
    venue: str
    base_ccy: str = ""
    quote_ccy: str = ""
    session_bucket: str = ""
    sleeve: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_safe_dataclass(self)


@dataclass(slots=True)
class PortfolioBook:
    positions: list[BookPosition] = field(default_factory=list)
    pending_positions: list[BookPosition] = field(default_factory=list)
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    pending_gross_exposure: float = 0.0
    pending_net_exposure: float = 0.0
    gross_lot_exposure: float = 0.0
    net_lot_exposure: float = 0.0
    pending_gross_lot_exposure: float = 0.0
    pending_net_lot_exposure: float = 0.0
    exposure_unit: str = "lot_units"
    open_position_count: int = 0
    pending_entry_count: int = 0
    per_symbol_exposure: dict[str, float] = field(default_factory=dict)
    per_symbol_net_exposure: dict[str, float] = field(default_factory=dict)
    per_currency_exposure: dict[str, float] = field(default_factory=dict)
    per_currency_net_exposure: dict[str, float] = field(default_factory=dict)
    per_asset_class_exposure: dict[str, float] = field(default_factory=dict)
    per_asset_class_net_exposure: dict[str, float] = field(default_factory=dict)
    session_counts: dict[str, int] = field(default_factory=dict)
    sleeve_counts: dict[str, int] = field(default_factory=dict)
    # Account-currency loss if this symbol's OPEN positions all stop out, and the
    # sum over symbols. These are the two fields `evaluate_book_stress` reads;
    # until 2026-07-31 NEITHER existed on this class, so every stress evaluation
    # silently returned worst_case_loss_proxy=0.0 and the capital tail-loss gate
    # never bound. Published only when derivable from the rows (see the ladder in
    # `_register_row`); absent data stays absent rather than being invented.
    per_symbol_stop_risk: dict[str, float] = field(default_factory=dict)
    capital_at_risk: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return json_safe_dataclass(
            self,
            overrides={
                "positions": [item.to_dict() for item in self.positions],
                "pending_positions": [
                    item.to_dict() for item in self.pending_positions
                ],
            },
        )

class PreparedPortfolioBook:
    """Cycle-owned open-position book used to compose changing reservations."""

    __slots__ = ("_book", "_derived", "_payload")

    def __init__(self, book: PortfolioBook) -> None:
        self._book = book
        self._payload = book.to_dict()
        self._derived: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        """Return an isolated copy of the once-normalized base-book payload."""

        return copy_json_payload(self._payload)


def build_portfolio_book(
    *,
    positions: list[dict[str, Any]],
    pending_entries: list[dict[str, Any]] | None = None,
) -> PortfolioBook:
    book_positions: list[BookPosition] = []
    pending_positions: list[BookPosition] = []
    per_symbol: dict[str, float] = {}
    per_symbol_net: dict[str, float] = {}
    per_currency: dict[str, float] = {}
    per_currency_net: dict[str, float] = {}
    per_asset_class: dict[str, float] = {}
    per_asset_class_net: dict[str, float] = {}
    session_counts: dict[str, int] = {}
    sleeve_counts: dict[str, int] = {}
    per_symbol_stop_risk: dict[str, float] = {}
    gross_exposure = 0.0
    net_exposure = 0.0
    pending_gross_exposure = 0.0
    pending_net_exposure = 0.0
    gross_lot_exposure = 0.0
    net_lot_exposure = 0.0
    pending_gross_lot_exposure = 0.0
    pending_net_lot_exposure = 0.0
    position_exposure_units: set[str] = set()
    numeric_input_errors: list[str] = []
    invalid_position_rows: set[int] = set()
    invalid_pending_rows: set[int] = set()

    def _register_row(raw: dict[str, Any], *, pending: bool, row_index: int) -> None:
        nonlocal gross_exposure, net_exposure, pending_gross_exposure, pending_net_exposure, gross_lot_exposure, net_lot_exposure, pending_gross_lot_exposure, pending_net_lot_exposure
        row = _entry_row(dict(raw or {})) if pending else dict(raw or {})
        symbol = str(row.get("symbol") or row.get("pair") or "").strip().upper()
        if not symbol:
            return
        row_scope = "pending" if pending else "position"
        row_errors = _numeric_contract_errors(row)
        if row_errors:
            target = invalid_pending_rows if pending else invalid_position_rows
            target.add(int(row_index))
            numeric_input_errors.extend(f"{row_scope}[{row_index}].{item}" for item in row_errors)
        raw_lots = _finite_float(row.get("lots", 0.0))
        lots = abs(float(raw_lots)) if raw_lots is not None else 0.0
        side = _position_side(row)
        instrument = _book_instrument_ref(symbol)
        exposure_units, position_unit = _exposure_units(row, instrument=instrument, lots=lots)
        signed_exposure = exposure_units if side == "BUY" else (-exposure_units if side == "SELL" else 0.0)
        if exposure_units > 0.0:
            position_exposure_units.add(str(position_unit))
        session_bucket = _session_bucket(row)
        sleeve = str(row.get("sleeve") or row.get("playbook") or "").strip().lower()
        position = BookPosition(
            symbol=symbol,
            side=side,
            lots=float(lots),
            signed_exposure=float(signed_exposure),
            exposure_units=float(exposure_units),
            instrument_id=str(instrument.instrument_id),
            asset_class=str(instrument.asset_class),
            venue=str(instrument.venue),
            base_ccy=str(instrument.base_ccy),
            quote_ccy=str(instrument.quote_ccy),
            session_bucket=session_bucket,
            sleeve=sleeve,
            metadata=_json_safe(row),
        )
        if pending:
            pending_positions.append(position)
        else:
            book_positions.append(position)
        gross_exposure += abs(float(signed_exposure))
        net_exposure += float(signed_exposure)
        gross_lot_exposure += abs(float(lots))
        net_lot_exposure += float(lots if side == "BUY" else (-lots if side == "SELL" else 0.0))
        if pending:
            pending_gross_exposure += abs(float(signed_exposure))
            pending_net_exposure += float(signed_exposure)
            pending_gross_lot_exposure += abs(float(lots))
            pending_net_lot_exposure += float(lots if side == "BUY" else (-lots if side == "SELL" else 0.0))
        per_symbol[symbol] = float(per_symbol.get(symbol, 0.0)) + abs(float(signed_exposure))
        per_symbol_net[symbol] = float(per_symbol_net.get(symbol, 0.0)) + float(signed_exposure)
        per_asset_class[position.asset_class] = float(per_asset_class.get(position.asset_class, 0.0)) + abs(float(signed_exposure))
        per_asset_class_net[position.asset_class] = float(per_asset_class_net.get(position.asset_class, 0.0)) + float(signed_exposure)
        if position.asset_class == "fx":
            if position.base_ccy:
                per_currency[position.base_ccy] = float(per_currency.get(position.base_ccy, 0.0)) + abs(float(signed_exposure))
                per_currency_net[position.base_ccy] = float(per_currency_net.get(position.base_ccy, 0.0)) + float(signed_exposure)
            if position.quote_ccy:
                per_currency[position.quote_ccy] = float(per_currency.get(position.quote_ccy, 0.0)) + abs(float(signed_exposure))
                per_currency_net[position.quote_ccy] = float(per_currency_net.get(position.quote_ccy, 0.0)) - float(signed_exposure)
        elif position.asset_class == "crypto":
            if position.quote_ccy:
                per_currency[position.quote_ccy] = float(per_currency.get(position.quote_ccy, 0.0)) + abs(float(signed_exposure))
                per_currency_net[position.quote_ccy] = float(per_currency_net.get(position.quote_ccy, 0.0)) - float(signed_exposure)
        if session_bucket:
            session_counts[session_bucket] = int(session_counts.get(session_bucket, 0)) + 1
        if sleeve:
            sleeve_counts[sleeve] = int(sleeve_counts.get(sleeve, 0)) + 1
        if not pending:
            # Stop-out loss ladder, strictest-to-honest:
            #  1. an explicit account-currency figure supplied upstream;
            #  2. |open - sl| * value_per_price_unit * lots, when the caller
            #     attached the same per-lot contract value the sizer uses
            #     (runner attaches it from live quote rates);
            #  3. nothing. book.py has no rate service, and a stop risk computed
            #     with a guessed conversion is exactly the "fabricated risk
            #     number" stress.py refuses to report.
            stop_risk = _first_numeric(row, "stop_risk", "risk_cash", "capital_at_risk")
            if stop_risk is None:
                sl_price = _first_numeric(row, "sl", "sl_price", "stop_loss")
                open_price = _first_numeric(row, "open_price", "price_open", "entry_price")
                vpu = _first_numeric(row, "value_per_price_unit")
                if (
                    sl_price is not None
                    and open_price is not None
                    and vpu is not None
                    and sl_price > 0.0
                    and open_price > 0.0
                    and vpu > 0.0
                    and lots > 0.0
                ):
                    stop_risk = abs(open_price - sl_price) * vpu * lots
            if stop_risk is not None and math.isfinite(float(stop_risk)) and float(stop_risk) > 0.0:
                per_symbol_stop_risk[symbol] = float(per_symbol_stop_risk.get(symbol, 0.0)) + abs(float(stop_risk))
    for row_index, raw in enumerate(list(positions or [])):
        _register_row(dict(raw or {}), pending=False, row_index=row_index)
    for row_index, raw in enumerate(list(pending_entries or [])):
        _register_row(dict(raw or {}), pending=True, row_index=row_index)
    exposure_unit_contract_valid = len(position_exposure_units) <= 1
    if not position_exposure_units:
        exposure_unit = "lot_units"
    elif exposure_unit_contract_valid:
        exposure_unit = next(iter(position_exposure_units))
    else:
        exposure_unit = "mixed_units"
    return PortfolioBook(
        positions=book_positions,
        pending_positions=pending_positions,
        gross_exposure=float(gross_exposure),
        net_exposure=float(net_exposure),
        pending_gross_exposure=float(pending_gross_exposure),
        pending_net_exposure=float(pending_net_exposure),
        gross_lot_exposure=float(gross_lot_exposure),
        net_lot_exposure=float(net_lot_exposure),
        pending_gross_lot_exposure=float(pending_gross_lot_exposure),
        pending_net_lot_exposure=float(pending_net_lot_exposure),
        exposure_unit=str(exposure_unit),
        open_position_count=int(len(book_positions)),
        pending_entry_count=int(len(list(pending_entries or []))),
        per_symbol_exposure={str(k): float(v) for k, v in sorted(per_symbol.items())},
        per_symbol_net_exposure={str(k): float(v) for k, v in sorted(per_symbol_net.items())},
        per_currency_exposure={str(k): float(v) for k, v in sorted(per_currency.items())},
        per_currency_net_exposure={str(k): float(v) for k, v in sorted(per_currency_net.items())},
        per_asset_class_exposure={str(k): float(v) for k, v in sorted(per_asset_class.items())},
        per_asset_class_net_exposure={str(k): float(v) for k, v in sorted(per_asset_class_net.items())},
        session_counts={str(k): int(v) for k, v in sorted(session_counts.items())},
        sleeve_counts={str(k): int(v) for k, v in sorted(sleeve_counts.items())},
        per_symbol_stop_risk={str(k): float(v) for k, v in sorted(per_symbol_stop_risk.items())},
        capital_at_risk=float(sum(per_symbol_stop_risk.values())),
        metadata={
            "numeric_inputs_valid": not numeric_input_errors,
            "numeric_input_errors": sorted(set(numeric_input_errors)),
            "invalid_position_count": int(len(invalid_position_rows)),
            "invalid_pending_entry_count": int(len(invalid_pending_rows)),
            "exposure_unit_contract_valid": bool(exposure_unit_contract_valid),
            "position_exposure_units": sorted(position_exposure_units),
        },
    )


def prepare_portfolio_book(
    positions: list[dict[str, Any]],
) -> PreparedPortfolioBook:
    """Normalize the invariant open-position side of one allocation cycle."""

    return PreparedPortfolioBook(
        build_portfolio_book(positions=list(positions or []), pending_entries=[])
    )


def _sum_float_maps(
    left: dict[str, float],
    right: dict[str, float],
) -> dict[str, float]:
    keys = set(left) | set(right)
    return {
        str(key): float(left.get(key, 0.0)) + float(right.get(key, 0.0))
        for key in sorted(keys)
    }


def _sum_int_maps(
    left: dict[str, int],
    right: dict[str, int],
) -> dict[str, int]:
    keys = set(left) | set(right)
    return {
        str(key): int(left.get(key, 0)) + int(right.get(key, 0))
        for key in sorted(keys)
    }


def compose_portfolio_book(
    prepared: PreparedPortfolioBook,
    *,
    pending_entries: list[dict[str, Any]] | None = None,
) -> PortfolioBook:
    """Combine a prepared open book with only the current pending entries."""

    base = prepared._book
    if not pending_entries:
        return base
    pending = build_portfolio_book(
        positions=[],
        pending_entries=list(pending_entries or []),
    )
    base_metadata = dict(base.metadata or {})
    pending_metadata = dict(pending.metadata or {})
    numeric_errors = sorted(
        {
            str(item)
            for item in [
                *list(base_metadata.get("numeric_input_errors") or []),
                *list(pending_metadata.get("numeric_input_errors") or []),
            ]
        }
    )
    exposure_units = sorted(
        {
            str(item)
            for item in [
                *list(base_metadata.get("position_exposure_units") or []),
                *list(pending_metadata.get("position_exposure_units") or []),
            ]
            if str(item)
        }
    )
    exposure_unit_contract_valid = len(exposure_units) <= 1
    exposure_unit = (
        "lot_units"
        if not exposure_units
        else exposure_units[0]
        if exposure_unit_contract_valid
        else "mixed_units"
    )
    return PortfolioBook(
        positions=list(base.positions),
        pending_positions=list(pending.pending_positions),
        gross_exposure=float(base.gross_exposure + pending.gross_exposure),
        net_exposure=float(base.net_exposure + pending.net_exposure),
        pending_gross_exposure=float(pending.pending_gross_exposure),
        pending_net_exposure=float(pending.pending_net_exposure),
        gross_lot_exposure=float(base.gross_lot_exposure + pending.gross_lot_exposure),
        net_lot_exposure=float(base.net_lot_exposure + pending.net_lot_exposure),
        pending_gross_lot_exposure=float(pending.pending_gross_lot_exposure),
        pending_net_lot_exposure=float(pending.pending_net_lot_exposure),
        exposure_unit=str(exposure_unit),
        open_position_count=int(base.open_position_count),
        pending_entry_count=int(pending.pending_entry_count),
        per_symbol_exposure=_sum_float_maps(
            base.per_symbol_exposure,
            pending.per_symbol_exposure,
        ),
        per_symbol_net_exposure=_sum_float_maps(
            base.per_symbol_net_exposure,
            pending.per_symbol_net_exposure,
        ),
        per_currency_exposure=_sum_float_maps(
            base.per_currency_exposure,
            pending.per_currency_exposure,
        ),
        per_currency_net_exposure=_sum_float_maps(
            base.per_currency_net_exposure,
            pending.per_currency_net_exposure,
        ),
        per_asset_class_exposure=_sum_float_maps(
            base.per_asset_class_exposure,
            pending.per_asset_class_exposure,
        ),
        per_asset_class_net_exposure=_sum_float_maps(
            base.per_asset_class_net_exposure,
            pending.per_asset_class_net_exposure,
        ),
        session_counts=_sum_int_maps(base.session_counts, pending.session_counts),
        sleeve_counts=_sum_int_maps(base.sleeve_counts, pending.sleeve_counts),
        per_symbol_stop_risk=dict(base.per_symbol_stop_risk),
        capital_at_risk=float(base.capital_at_risk),
        metadata={
            "numeric_inputs_valid": not numeric_errors,
            "numeric_input_errors": numeric_errors,
            "invalid_position_count": int(
                base_metadata.get("invalid_position_count", 0) or 0
            ),
            "invalid_pending_entry_count": int(
                pending_metadata.get("invalid_pending_entry_count", 0) or 0
            ),
            "exposure_unit_contract_valid": bool(exposure_unit_contract_valid),
            "position_exposure_units": exposure_units,
        },
    )
