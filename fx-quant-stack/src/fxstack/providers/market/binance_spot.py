from __future__ import annotations

from typing import Any

from fxstack.providers.catalog import infer_instrument_ref
from fxstack.providers.contracts import CanonicalQuote


def _require_ccxt() -> Any:
    try:
        import ccxt  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency may be optional in CI
        raise RuntimeError("binance_spot provider requires optional dependency 'ccxt'") from exc
    return ccxt


def fetch_latest_quotes(
    *,
    symbols: list[str],
    exchange_id: str = "binance",
) -> dict[str, dict[str, Any]]:
    if not symbols:
        return {}
    ccxt = _require_ccxt()
    exchange_cls = getattr(ccxt, str(exchange_id), None)
    if exchange_cls is None:
        raise RuntimeError(f"unknown ccxt exchange '{exchange_id}'")
    exchange = exchange_cls({"enableRateLimit": True})
    tickers = exchange.fetch_tickers([str(item).upper() for item in symbols])
    out: dict[str, dict[str, Any]] = {}
    for symbol, raw in dict(tickers or {}).items():
        item = dict(raw or {})
        symbol_value = str(symbol or item.get("symbol") or "").upper()
        if not symbol_value:
            continue
        instrument = infer_instrument_ref(symbol_value, provider="binance_spot", venue="spot", asset_class="crypto")
        symbol_key = str(instrument.canonical_symbol).upper()
        bid = float(item.get("bid", 0.0) or 0.0)
        ask = float(item.get("ask", 0.0) or 0.0)
        last = float(item.get("last", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else last
        spread_bps = ((ask - bid) / mid) * 10000.0 if bid > 0 and ask > 0 and mid > 0 else 0.0
        flags: list[str] = []
        if bid <= 0.0:
            flags.append("missing_bid")
        if ask <= 0.0:
            flags.append("missing_ask")
        if bid <= 0.0 or ask <= 0.0:
            flags.append("proxy_spread")
        quote = CanonicalQuote(
            instrument=instrument,
            provider="binance_spot",
            ts=str(item.get("datetime") or ""),
            bid=bid if bid > 0.0 else last,
            ask=ask if ask > 0.0 else last,
            mid=mid,
            spread_bps=float(spread_bps),
            provenance="ccxt_binance_spot",
            quality_flags=flags,
            metadata={"quote_volume": item.get("quoteVolume"), "base_volume": item.get("baseVolume")},
        )
        out[symbol_key] = quote.to_dict()
    return out
