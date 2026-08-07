"""Explicit production identity catalog for the supported IG MT4 universe.

The catalog contains venue and instrument facts only.  It intentionally has
no strategy, sizing, authority, bridge, settings, or research dependency, so
both the installed runtime and excluded research helpers can consume the same
ordered scope without reversing the production boundary.

IG exposes the two crypto instruments here as MT4 CFDs.  Their canonical
asset class remains ``crypto`` for provider and portfolio interoperability;
the ``ig_mt4`` venue identity distinguishes them from spot instruments.

Scope v3 retains scope v2's replacement of non-tradable ``XRPUSD`` with
broker-enabled ``NZDJPY`` and replaces cost-dead/stale ``LTCUSD`` with
broker-listed ``AUDCAD`` while preserving the exact ordered 22-symbol
strategy and execution boundary.
"""

# AGENT: ROLE: Production-owned immutable identity catalog for the exact IG MT4 scalp scope.
# AGENT: HANDSHAKE: Catalog identities -> runtime scalp authority and excluded research adapters.
# AGENT: SIDE EFFECTS: None; contains no settings, transport, persistence, or execution access.

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, Mapping


IG_MT4_VENUE_ID = "ig_mt4"
IG_MT4_SCALP_SCOPE_VERSION = "fxstack.ig_mt4.scalp_scope.v3"


@dataclass(frozen=True, slots=True)
class IgMt4InstrumentIdentity:
    """Immutable canonical and broker-facing identity for one IG MT4 symbol."""

    canonical_symbol: str
    provider_symbol: str
    base_ccy: str
    quote_ccy: str
    asset_class: Literal["fx", "crypto"]
    venue: str = IG_MT4_VENUE_ID

    def __post_init__(self) -> None:
        symbol = str(self.canonical_symbol)
        if symbol != symbol.strip().upper() or len(symbol) != 6 or not symbol.isalpha():
            raise ValueError(f"invalid IG MT4 canonical symbol: {symbol!r}")
        if not str(self.provider_symbol or "").strip():
            raise ValueError(f"missing IG MT4 provider symbol for {symbol}")
        if f"{self.base_ccy}{self.quote_ccy}" != symbol:
            raise ValueError(f"invalid IG MT4 base/quote identity for {symbol}")
        if self.asset_class not in {"fx", "crypto"}:
            raise ValueError(f"invalid IG MT4 asset class for {symbol}")
        if self.venue != IG_MT4_VENUE_ID:
            raise ValueError(f"invalid IG MT4 venue for {symbol}")

    @property
    def instrument_id(self) -> str:
        return f"{self.asset_class}:{self.venue}:{self.canonical_symbol}"

    @property
    def is_crypto_cfd(self) -> bool:
        return self.asset_class == "crypto"


IG_MT4_SCALP_INSTRUMENTS: tuple[IgMt4InstrumentIdentity, ...] = (
    IgMt4InstrumentIdentity(
        canonical_symbol="EURUSD",
        provider_symbol="EURUSD",
        base_ccy="EUR",
        quote_ccy="USD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="USDJPY",
        provider_symbol="USDJPY",
        base_ccy="USD",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="AUDUSD",
        provider_symbol="AUDUSD",
        base_ccy="AUD",
        quote_ccy="USD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="GBPUSD",
        provider_symbol="GBPUSD",
        base_ccy="GBP",
        quote_ccy="USD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="USDCAD",
        provider_symbol="USDCAD",
        base_ccy="USD",
        quote_ccy="CAD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="USDCHF",
        provider_symbol="USDCHF",
        base_ccy="USD",
        quote_ccy="CHF",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="EURGBP",
        provider_symbol="EURGBP",
        base_ccy="EUR",
        quote_ccy="GBP",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="EURJPY",
        provider_symbol="EURJPY",
        base_ccy="EUR",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="NZDUSD",
        provider_symbol="NZDUSD",
        base_ccy="NZD",
        quote_ccy="USD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="AUDJPY",
        provider_symbol="AUDJPY",
        base_ccy="AUD",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="CADJPY",
        provider_symbol="CADJPY",
        base_ccy="CAD",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="CHFJPY",
        provider_symbol="CHFJPY",
        base_ccy="CHF",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="EURAUD",
        provider_symbol="EURAUD",
        base_ccy="EUR",
        quote_ccy="AUD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="EURCAD",
        provider_symbol="EURCAD",
        base_ccy="EUR",
        quote_ccy="CAD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="EURCHF",
        provider_symbol="EURCHF",
        base_ccy="EUR",
        quote_ccy="CHF",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="GBPCAD",
        provider_symbol="GBPCAD",
        base_ccy="GBP",
        quote_ccy="CAD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="GBPCHF",
        provider_symbol="GBPCHF",
        base_ccy="GBP",
        quote_ccy="CHF",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="GBPJPY",
        provider_symbol="GBPJPY",
        base_ccy="GBP",
        quote_ccy="JPY",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="BTCUSD",
        provider_symbol="BTCUSD",
        base_ccy="BTC",
        quote_ccy="USD",
        asset_class="crypto",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="ETHUSD",
        provider_symbol="ETHUSD",
        base_ccy="ETH",
        quote_ccy="USD",
        asset_class="crypto",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="AUDCAD",
        provider_symbol="AUDCAD",
        base_ccy="AUD",
        quote_ccy="CAD",
        asset_class="fx",
    ),
    IgMt4InstrumentIdentity(
        canonical_symbol="NZDJPY",
        provider_symbol="NZDJPY",
        base_ccy="NZD",
        quote_ccy="JPY",
        asset_class="fx",
    ),
)

IG_MT4_FX_SYMBOLS: tuple[str, ...] = tuple(
    item.canonical_symbol
    for item in IG_MT4_SCALP_INSTRUMENTS
    if item.asset_class == "fx"
)
IG_MT4_CRYPTO_CFD_SYMBOLS: tuple[str, ...] = tuple(
    item.canonical_symbol
    for item in IG_MT4_SCALP_INSTRUMENTS
    if item.asset_class == "crypto"
)
IG_MT4_SCALP_SYMBOLS: tuple[str, ...] = tuple(
    item.canonical_symbol for item in IG_MT4_SCALP_INSTRUMENTS
)
IG_MT4_SCALP_CATALOG: Mapping[str, IgMt4InstrumentIdentity] = MappingProxyType(
    {item.canonical_symbol: item for item in IG_MT4_SCALP_INSTRUMENTS}
)
IG_MT4_PAIR_LEGS: Mapping[str, tuple[str, str]] = MappingProxyType(
    {
        item.canonical_symbol: (item.base_ccy, item.quote_ccy)
        for item in IG_MT4_SCALP_INSTRUMENTS
    }
)

if len(IG_MT4_SCALP_INSTRUMENTS) != 22 or len(IG_MT4_SCALP_CATALOG) != 22:
    raise RuntimeError("IG MT4 scalp catalog must contain 22 unique instruments")
if len(IG_MT4_FX_SYMBOLS) != 20 or len(IG_MT4_CRYPTO_CFD_SYMBOLS) != 2:
    raise RuntimeError("IG MT4 scalp catalog must contain 20 FX and 2 crypto CFDs")


def get_ig_mt4_instrument(symbol: str) -> IgMt4InstrumentIdentity | None:
    """Return the exact catalog identity for a canonical IG MT4 symbol."""

    key = "".join(char for char in str(symbol or "").strip().upper() if char.isalnum())
    return IG_MT4_SCALP_CATALOG.get(key)


__all__ = [
    "IG_MT4_CRYPTO_CFD_SYMBOLS",
    "IG_MT4_FX_SYMBOLS",
    "IG_MT4_PAIR_LEGS",
    "IG_MT4_SCALP_CATALOG",
    "IG_MT4_SCALP_INSTRUMENTS",
    "IG_MT4_SCALP_SCOPE_VERSION",
    "IG_MT4_SCALP_SYMBOLS",
    "IG_MT4_VENUE_ID",
    "IgMt4InstrumentIdentity",
    "get_ig_mt4_instrument",
]
