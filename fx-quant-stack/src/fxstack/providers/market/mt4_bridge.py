from __future__ import annotations

from typing import Any

import pandas as pd
import requests

from fxstack.live.policy import normalize_spread_bps
from fxstack.providers.catalog import infer_instrument_ref
from fxstack.providers.contracts import CanonicalQuote, InstrumentRef
from fxstack.providers.ig_mt4_catalog import (
    IG_MT4_SCALP_SYMBOLS,
    get_ig_mt4_instrument,
)
from fxstack.runtime.market_source_identity import MARKET_SOURCE_FIELDS


_BRIDGE_TICK_FRESHNESS_FIELDS: tuple[str, ...] = (
    "received_at_epoch",
    "received_at",
    "transport_age_secs",
    "transport_fresh",
    "source_event_token",
    "source_event_last_token",
    "source_event_baseline_initialized",
    "market_event_received_at_epoch",
    "market_event_received_at",
    "market_event_sequence",
    "market_event_trigger",
    "market_event_identity_present",
    "market_event_age_secs",
    "market_event_stale_after_secs",
    "market_event_fresh",
    "market_event_reason",
)
_BRIDGE_TICK_PROVENANCE_FIELDS: tuple[str, ...] = (
    *_BRIDGE_TICK_FRESHNESS_FIELDS,
    *MARKET_SOURCE_FIELDS,
)


def _headers(api_key: str) -> dict[str, str] | None:
    txt = str(api_key or "").strip()
    return {"X-API-Key": txt} if txt else None


def _resolve_ig_mt4_instrument(symbol: str) -> InstrumentRef:
    """Resolve known IG identities; preserve the legacy FX fallback otherwise."""

    provider_symbol = str(symbol or "").strip().upper()
    identity = get_ig_mt4_instrument(provider_symbol)
    if identity is None:
        return infer_instrument_ref(
            provider_symbol,
            provider="mt4_bridge",
            venue="otc",
            asset_class="fx",
        )
    return infer_instrument_ref(
        identity.canonical_symbol,
        provider="mt4_bridge",
        venue=identity.venue,
        asset_class=identity.asset_class,
        provider_symbol=provider_symbol or identity.provider_symbol,
    )


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
        mid = (
            (bid + ask) / 2.0
            if bid > 0 and ask > 0
            else float(row.get("mid", 0.0) or 0.0)
        )
        spread_bps, spread_source = normalize_spread_bps(tick=row, pair=symbol_key)
        quality_flags: list[str] = []
        if bid <= 0.0:
            quality_flags.append("missing_bid")
        if ask <= 0.0:
            quality_flags.append("missing_ask")
        if str(spread_source) == "missing":
            quality_flags.append("missing_spread")
        freshness = {
            key: row.get(key) for key in _BRIDGE_TICK_PROVENANCE_FIELDS if key in row
        }
        instrument = _resolve_ig_mt4_instrument(symbol_key)
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
            metadata={
                "spread_unit_source": str(spread_source),
                **freshness,
            },
        )
        canonical = quote.to_dict()
        # Runtime entry readiness and live feature bucketing consume these
        # fields directly from the quote row. Keep them in canonical metadata
        # for provenance while also preserving the established top-level tick
        # interface; otherwise provider normalization silently turns a fresh
        # broker event into a fail-closed "identity missing" decision.
        canonical.update(freshness)
        out[str(instrument.canonical_symbol)] = canonical
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
            params={
                "symbol": str(symbol).upper(),
                "timeframe": str(timeframe).upper(),
                "limit": max(1, min(int(limit), 2000)),
            },
            headers=_headers(api_key),
            timeout=3,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return []
    rows = list(payload.get("bars") or []) if isinstance(payload, dict) else []
    return _normalize_bars(str(symbol).upper(), rows)


def _normalize_bars(symbol: str, rows: list[Any]) -> list[dict[str, Any]]:
    instrument = _resolve_ig_mt4_instrument(str(symbol).upper())
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
    ts_col = (
        "ts"
        if any("ts" in item for item in out)
        else ("time" if any("time" in item for item in out) else "")
    )
    if not ts_col:
        return out
    sortable: list[tuple[Any, int, dict[str, Any]]] = []
    for index, item in enumerate(out):
        parsed_ts = pd.to_datetime(item.get(ts_col), utc=True, errors="coerce")
        if pd.isna(parsed_ts):
            continue
        sortable.append((parsed_ts, index, item))
    sortable.sort(key=lambda entry: (entry[0], entry[1]))
    deduplicated: dict[Any, dict[str, Any]] = {}
    for parsed_ts, _index, item in sortable:
        # Preserve the original row object and its scalar types. Building a
        # DataFrame here coerces sparse integer iVolume values to floats before
        # the provider reaches the runtime.
        deduplicated[parsed_ts] = item
    return list(deduplicated.values())


def fetch_exact_scalp_bar_batch(
    bridge_url: str,
    *,
    timeframe: str = "M1",
    limit: int = 242,
    api_key: str = "",
) -> dict[str, list[dict[str, Any]]]:
    """Fetch the ordered exact-22 bar scope through one bridge request."""

    url = f"{str(bridge_url).rstrip('/')}/v2/market/bars/batch"
    try:
        response = requests.get(
            url,
            params={
                "timeframe": str(timeframe).upper(),
                "limit": max(1, min(int(limit), 2000)),
            },
            headers=_headers(api_key),
            timeout=3,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    observed_symbols = tuple(
        str(item or "").strip().upper()
        for item in list(payload.get("symbols") or [])
    )
    raw_scope = payload.get("bars_by_symbol")
    if (
        payload.get("schema") != "fxstack.exact_scalp_bar_batch.v1"
        or observed_symbols != IG_MT4_SCALP_SYMBOLS
        or str(payload.get("timeframe") or "").upper() != "M1"
        or not isinstance(raw_scope, dict)
        or tuple(str(item).upper() for item in raw_scope) != IG_MT4_SCALP_SYMBOLS
    ):
        return {}
    return {
        symbol: _normalize_bars(symbol, list(raw_scope.get(symbol) or []))
        for symbol in IG_MT4_SCALP_SYMBOLS
    }


def fetch_ready(bridge_url: str, *, api_key: str = "") -> dict[str, Any]:
    url = f"{str(bridge_url).rstrip('/')}/v2/ready"
    try:
        response = requests.get(url, headers=_headers(api_key), timeout=2)
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return {}
    return dict(payload if isinstance(payload, dict) else {})
