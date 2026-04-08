from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .lifecycle import LifecycleState


LOT_UNITS = 100_000.0


@dataclass(slots=True)
class TradeFill:
    pair: str
    side: str
    lots: float
    price: float
    ts: str
    event_type: str
    cost_bps: float = 0.0
    realized_pnl_usd: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PositionLedger:
    pair: str
    side: str
    open_lots: float = 0.0
    entry_price: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    avg_entry_price: float = 0.0
    partial_close_count: int = 0
    campaign_state: str = "inactive"
    campaign_reason: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class PortfolioSnapshot:
    equity_usd: float
    open_positions: int
    gross_exposure_lots: float
    net_exposure_lots: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    ledgers: list[PositionLedger] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction_sign(side: str) -> float:
    return -1.0 if str(side).lower() in {"short", "sell"} else 1.0


def build_portfolio_snapshot(
    ledgers: list[PositionLedger],
    *,
    equity_usd: float = 0.0,
) -> PortfolioSnapshot:
    gross = sum(abs(float(ledger.open_lots)) for ledger in ledgers)
    net = sum(float(ledger.open_lots) * _direction_sign(ledger.side) for ledger in ledgers)
    realized = sum(float(ledger.realized_pnl_usd) for ledger in ledgers)
    unrealized = sum(float(ledger.unrealized_pnl_usd) for ledger in ledgers)
    return PortfolioSnapshot(
        equity_usd=float(equity_usd),
        open_positions=int(sum(1 for ledger in ledgers if ledger.open_lots > 0.0)),
        gross_exposure_lots=float(gross),
        net_exposure_lots=float(net),
        realized_pnl_usd=float(realized),
        unrealized_pnl_usd=float(unrealized),
        ledgers=list(ledgers),
    )


def fx_quote_to_usd_rate(*, quote_currency: str, bar_idx: int, mid_arrays: dict[str, Any]) -> float:
    quote = str(quote_currency or "").upper()
    if quote == "USD":
        return 1.0
    direct = {
        "EUR": "EURUSD",
        "GBP": "GBPUSD",
        "AUD": "AUDUSD",
        "NZD": "NZDUSD",
    }
    inverse = {
        "JPY": "USDJPY",
        "CHF": "USDCHF",
        "CAD": "USDCAD",
    }
    if quote in direct:
        pair = direct[quote]
        value = float(mid_arrays[pair][bar_idx])
        return value if value > 0.0 else 0.0
    if quote in inverse:
        pair = inverse[quote]
        value = float(mid_arrays[pair][bar_idx])
        return (1.0 / value) if value > 0.0 else 0.0
    raise KeyError(f"unsupported quote currency for usd conversion: {quote}")


def fx_realized_pnl_usd(
    *,
    pair: str,
    side: str,
    entry_price: float,
    exit_price: float,
    lots: float,
    bar_idx: int,
    mid_arrays: dict[str, Any],
) -> float:
    units = float(lots) * LOT_UNITS
    if str(side).lower() == "long":
        pnl_quote = (float(exit_price) - float(entry_price)) * units
    else:
        pnl_quote = (float(entry_price) - float(exit_price)) * units
    quote_ccy = str(pair)[3:6]
    fx = fx_quote_to_usd_rate(quote_currency=quote_ccy, bar_idx=bar_idx, mid_arrays=mid_arrays)
    return float(pnl_quote * fx)


def fx_mark_to_market_equity(
    *,
    cash_balance: float,
    open_positions: dict[str, Any],
    bar_idx: int,
    bid_arrays: dict[str, Any],
    ask_arrays: dict[str, Any],
    mid_arrays: dict[str, Any],
) -> float:
    equity = float(cash_balance)
    for pos in open_positions.values():
        exit_price = float(bid_arrays[pos.pair][bar_idx]) if str(pos.side).lower() == "long" else float(ask_arrays[pos.pair][bar_idx])
        equity += fx_realized_pnl_usd(
            pair=str(pos.pair),
            side=str(pos.side),
            entry_price=float(pos.entry_price),
            exit_price=float(exit_price),
            lots=float(pos.lots),
            bar_idx=bar_idx,
            mid_arrays=mid_arrays,
        )
    return float(equity)


def record_fill(
    ledger: PositionLedger,
    *,
    fill: TradeFill,
    lifecycle_state: LifecycleState | None = None,
) -> PositionLedger:
    out = PositionLedger(**ledger.to_dict())
    event = fill.to_dict()
    out.events.append(event)
    if fill.event_type in {"entry", "open"}:
        total_notional = float(out.avg_entry_price) * float(out.open_lots) + float(fill.price) * float(fill.lots)
        new_lots = float(out.open_lots) + float(fill.lots)
        out.avg_entry_price = total_notional / new_lots if new_lots > 0.0 else float(fill.price)
        out.open_lots = new_lots
        out.entry_price = float(out.avg_entry_price)
    elif fill.event_type in {"partial_tp", "close_partial"}:
        out.open_lots = max(0.0, float(out.open_lots) - float(fill.lots))
        out.partial_close_count += 1
    elif fill.event_type in {"exit", "close"}:
        out.open_lots = 0.0
    out.realized_pnl_usd += float(fill.realized_pnl_usd)
    if lifecycle_state is not None:
        out.campaign_state = str(lifecycle_state.campaign_state)
        out.campaign_reason = str(lifecycle_state.campaign_reason)
    return out
