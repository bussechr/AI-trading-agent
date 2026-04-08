from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from fxstack.providers.catalog import infer_instrument_ref


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
    return str(row.get("session_bucket") or row.get("sessionBucket") or "").strip().lower()


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
        try:
            number = float(value)
        except Exception:
            continue
        if number == number:  # NaN guard
            return float(number)
    return None


def _exposure_units(row: dict[str, Any], *, instrument: Any, lots: float) -> tuple[float, str]:
    metadata = row
    explicit = _first_numeric(
        metadata,
        "exposure_units",
        "exposure_unit",
        "notional_units",
        "quote_notional",
        "notional",
        "exposure_notional",
        "gross_notional",
    )
    if explicit is not None:
        return abs(float(explicit)), "notional_units"

    contract_size = _first_numeric(metadata, "contract_size", "lot_size") or float(getattr(instrument, "lot_size", 1.0) or 1.0)
    reference_price = _first_numeric(
        metadata,
        "mark_price",
        "mid",
        "price",
        "open_price",
        "close_price",
        "entry_price",
        "avg_price",
        "last_price",
    )
    if reference_price is not None:
        return abs(float(lots) * float(contract_size) * float(reference_price)), "notional_units"
    return abs(float(lots) * float(contract_size)), "lot_units"


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
        return asdict(self)


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
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["positions"] = [item.to_dict() for item in self.positions]
        return payload


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
    gross_exposure = 0.0
    net_exposure = 0.0
    pending_gross_exposure = 0.0
    pending_net_exposure = 0.0
    gross_lot_exposure = 0.0
    net_lot_exposure = 0.0
    pending_gross_lot_exposure = 0.0
    pending_net_lot_exposure = 0.0
    exposure_unit = "lot_units"

    def _register_row(raw: dict[str, Any], *, pending: bool) -> None:
        nonlocal gross_exposure, net_exposure, pending_gross_exposure, pending_net_exposure, gross_lot_exposure, net_lot_exposure, pending_gross_lot_exposure, pending_net_lot_exposure, exposure_unit
        row = _entry_row(dict(raw or {})) if pending else dict(raw or {})
        symbol = str(row.get("symbol") or row.get("pair") or "").strip().upper()
        if not symbol:
            return
        lots = float(row.get("lots", 0.0) or 0.0)
        side = _position_side(row)
        instrument = infer_instrument_ref(symbol)
        exposure_units, position_unit = _exposure_units(row, instrument=instrument, lots=lots)
        signed_exposure = exposure_units if side == "BUY" else (-exposure_units if side == "SELL" else 0.0)
        if exposure_unit == "lot_units" and position_unit != "lot_units":
            exposure_unit = str(position_unit)
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
            metadata=row,
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
    for raw in list(positions or []):
        _register_row(dict(raw or {}), pending=False)
    for raw in list(pending_entries or []):
        _register_row(dict(raw or {}), pending=True)
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
    )
