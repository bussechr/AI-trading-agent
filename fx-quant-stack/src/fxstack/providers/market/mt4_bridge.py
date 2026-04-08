from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from fxstack.live.policy import normalize_spread_bps
from fxstack.providers.catalog import infer_instrument_ref
from fxstack.providers.contracts import CanonicalQuote


def _headers(api_key: str) -> dict[str, str] | None:
    txt = str(api_key or "").strip()
    return {"X-API-Key": txt} if txt else None


def fetch_quotes(bridge_url: str, *, api_key: str = "") -> dict[str, dict[str, Any]]:
    url = f"{str(bridge_url).rstrip('/')}/v2/market/ticks"
    try:
        response = requests.get(url, headers=_headers(api_key), timeout=2)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    raw_quotes = dict(payload if isinstance(payload, dict) else {})
    out: dict[str, dict[str, Any]] = {}
    for symbol, raw in raw_quotes.items():
        row = dict(raw or {})
        symbol_key = str(symbol or row.get("symbol") or "").strip().upper()
        if not symbol_key:
            continue
        bid = float(row.get("bid", 0.0) or 0.0)
        ask = float(row.get("ask", 0.0) or 0.0)
        mid = (bid + ask) / 2.0 if bid > 0 and ask > 0 else float(row.get("mid", 0.0) or 0.0)
        spread_bps, spread_source = normalize_spread_bps(tick=row, pair=symbol_key)
        quality_flags: list[str] = []
        if bid <= 0.0:
            quality_flags.append("missing_bid")
        if ask <= 0.0:
            quality_flags.append("missing_ask")
        if str(spread_source) == "missing":
            quality_flags.append("missing_spread")
        instrument = infer_instrument_ref(symbol_key, provider="mt4_bridge", venue="otc", asset_class="fx")
        quote = CanonicalQuote(
            instrument=instrument,
            provider="mt4_bridge",
            ts=str(row.get("time") or row.get("ts") or ""),
            bid=bid,
            ask=ask,
            mid=mid,
            spread_bps=float(spread_bps),
            provenance="mt4_bridge",
            quality_flags=quality_flags,
            metadata={"spread_unit_source": str(spread_source)},
        )
        out[str(instrument.canonical_symbol)] = quote.to_dict()
    return out


def fetch_bars(
    bridge_url: str,
    *,
    symbol: str,
    timeframe: str,
    limit: int = 400,
    api_key: str = "",
) -> list[dict[str, Any]]:
    url = f"{str(bridge_url).rstrip('/')}/v2/market/bars"
    try:
        response = requests.get(
            url,
            params={"symbol": str(symbol).upper(), "timeframe": str(timeframe).upper(), "limit": max(1, min(int(limit), 2000))},
            headers=_headers(api_key),
            timeout=3,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []
    rows = list(payload.get("bars") or []) if isinstance(payload, dict) else []
    instrument = infer_instrument_ref(str(symbol).upper(), provider="mt4_bridge", venue="otc", asset_class="fx")
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row or {})
        item["pair"] = str(instrument.pair or instrument.canonical_symbol)
        item["provider"] = "mt4_bridge"
        item["instrument_id"] = str(instrument.instrument_id)
        item["asset_class"] = str(instrument.asset_class)
        item["venue"] = str(instrument.venue)
        item["provider_symbol"] = str(instrument.provider_symbol)
        item["canonical_symbol"] = str(instrument.canonical_symbol)
        item["base_ccy"] = str(instrument.base_ccy)
        item["quote_ccy"] = str(instrument.quote_ccy)
        item["provenance"] = "mt4_bridge"
        item["quality_flags"] = list(item.get("quality_flags") or [])
        out.append(item)
    frame = pd.DataFrame(out)
    ts_col = "ts" if "ts" in frame.columns else ("time" if "time" in frame.columns else "")
    if not ts_col:
        return out
    frame["_ts_sort"] = pd.to_datetime(frame[ts_col], utc=True, errors="coerce")
    frame = frame.dropna(subset=["_ts_sort"]).sort_values("_ts_sort").drop_duplicates(subset=["_ts_sort"], keep="last")
    return frame.drop(columns=["_ts_sort"]).to_dict(orient="records")


def fetch_ready(bridge_url: str, *, api_key: str = "") -> dict[str, Any]:
    url = f"{str(bridge_url).rstrip('/')}/v2/ready"
    try:
        response = requests.get(url, headers=_headers(api_key), timeout=2)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    return dict(payload if isinstance(payload, dict) else {})
