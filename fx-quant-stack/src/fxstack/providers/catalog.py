from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

import pandas as pd

from fxstack.providers.contracts import InstrumentRef


_CRYPTO_QUOTES = ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "EUR")
_SYMBOL_SEPARATORS = re.compile(r"[/_:\-]+")


def _normalize_symbol(symbol: str) -> str:
    return "".join(ch for ch in str(symbol or "").strip().upper() if ch.isalnum())


def _symbol_parts(symbol: str) -> list[str]:
    return [part for part in _SYMBOL_SEPARATORS.split(str(symbol or "").strip().upper()) if part]


def infer_asset_class(symbol: str) -> str:
    txt = _normalize_symbol(symbol)
    parts = _symbol_parts(symbol)
    if len(parts) == 2 and all(len(part) == 3 and part.isalpha() for part in parts):
        return "fx"
    if len(txt) == 6 and txt.isalpha():
        return "fx"
    if any(txt.endswith(suffix) for suffix in _CRYPTO_QUOTES) and len(txt) > 6:
        return "crypto"
    return "unknown"


def split_symbol(symbol: str, *, asset_class: str = "") -> tuple[str, str]:
    txt = _normalize_symbol(symbol)
    kind = str(asset_class or infer_asset_class(txt)).strip().lower()
    if kind == "fx" and len(txt) == 6:
        return txt[:3], txt[3:]
    if kind == "crypto":
        for suffix in _CRYPTO_QUOTES:
            if txt.endswith(suffix) and len(txt) > len(suffix):
                return txt[: -len(suffix)], suffix
    return txt, ""


def normalize_symbol(symbol: str, *, asset_class: str = "") -> str:
    raw = str(symbol or "").strip().upper()
    compact = _normalize_symbol(raw)
    parts = _symbol_parts(raw)
    kind = str(asset_class or "").strip().lower() or infer_asset_class(raw)
    if kind == "fx":
        if len(parts) == 2 and all(len(part) == 3 and part.isalpha() for part in parts):
            return "".join(parts)
        if len(compact) == 6 and compact.isalpha():
            return compact
    if kind == "crypto":
        if len(parts) == 2 and all(part for part in parts):
            return "".join(parts)
        return compact
    return compact or raw


def infer_instrument_ref(
    symbol: str,
    *,
    provider: str = "",
    venue: str = "",
    asset_class: str = "",
    provider_symbol: str = "",
) -> InstrumentRef:
    canonical_symbol = normalize_symbol(symbol, asset_class=asset_class)
    kind = str(asset_class or infer_asset_class(canonical_symbol)).strip().lower() or "unknown"
    base_ccy, quote_ccy = split_symbol(canonical_symbol, asset_class=kind)
    venue_value = str(venue or ("spot" if kind == "crypto" else "otc")).strip().lower()
    instrument_id = f"{kind}:{venue_value}:{canonical_symbol}"
    return InstrumentRef(
        instrument_id=instrument_id,
        canonical_symbol=canonical_symbol,
        provider_symbol=str(provider_symbol or symbol or canonical_symbol).strip().upper(),
        pair=canonical_symbol if kind == "fx" else "",
        asset_class=kind,
        venue=venue_value,
        base_ccy=base_ccy,
        quote_ccy=quote_ccy,
        tick_size=0.0,
        lot_size=1.0,
        metadata={"provider": str(provider or "").strip().lower()},
    )


def enrich_bars_frame(
    frame: pd.DataFrame,
    *,
    instrument: InstrumentRef,
    provider: str,
    timeframe: str,
    provenance: str = "",
    quality_flags: list[str] | None = None,
) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    out = frame.copy()
    out["pair"] = instrument.pair or instrument.canonical_symbol
    out["timeframe"] = str(timeframe).upper()
    out["provider"] = str(provider).strip().lower()
    out["instrument_id"] = str(instrument.instrument_id)
    out["asset_class"] = str(instrument.asset_class)
    out["venue"] = str(instrument.venue)
    out["provider_symbol"] = str(instrument.provider_symbol)
    out["canonical_symbol"] = str(instrument.canonical_symbol)
    out["base_ccy"] = str(instrument.base_ccy)
    out["quote_ccy"] = str(instrument.quote_ccy)
    out["provenance"] = str(provenance or provider)
    out["quality_flags"] = [list(quality_flags or []) for _ in range(len(out))]
    return out


@dataclass(slots=True)
class InstrumentCatalog:
    instruments: dict[str, InstrumentRef] = field(default_factory=dict)

    def get(self, symbol: str, *, provider: str = "", venue: str = "", asset_class: str = "") -> InstrumentRef:
        key = normalize_symbol(symbol, asset_class=asset_class)
        existing = self.instruments.get(key)
        if existing is not None:
            return existing
        inferred = infer_instrument_ref(symbol, provider=provider, venue=venue, asset_class=asset_class)
        self.instruments[key] = inferred
        return inferred

    def to_dict(self) -> dict[str, dict[str, Any]]:
        return {str(key): value.to_dict() for key, value in sorted(self.instruments.items())}


def build_default_catalog(*, fx_pairs: list[str] | None = None, crypto_symbols: list[str] | None = None) -> InstrumentCatalog:
    catalog = InstrumentCatalog()
    for pair in list(fx_pairs or []):
        catalog.get(str(pair).upper(), provider="dukascopy", venue="otc", asset_class="fx")
    for symbol in list(crypto_symbols or []):
        catalog.get(str(symbol).upper(), provider="binance_spot", venue="spot", asset_class="crypto")
    return catalog
