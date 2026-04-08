from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class InstrumentRef:
    instrument_id: str
    canonical_symbol: str
    provider_symbol: str = ""
    pair: str = ""
    asset_class: str = "fx"
    venue: str = ""
    base_ccy: str = ""
    quote_ccy: str = ""
    tick_size: float = 0.0
    lot_size: float = 1.0
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class CanonicalBar:
    instrument: InstrumentRef
    timeframe: str
    ts: str
    provider: str
    bid_open: float
    bid_high: float
    bid_low: float
    bid_close: float
    ask_open: float
    ask_high: float
    ask_low: float
    ask_close: float
    mid_open: float
    mid_high: float
    mid_low: float
    mid_close: float
    volume: float = 0.0
    spread: float = 0.0
    provenance: str = ""
    quality_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["instrument"] = self.instrument.to_dict()
        return payload


@dataclass(slots=True)
class CanonicalQuote:
    instrument: InstrumentRef
    provider: str
    ts: str
    bid: float
    ask: float
    mid: float = 0.0
    spread_bps: float = 0.0
    provenance: str = ""
    quality_flags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["instrument"] = self.instrument.to_dict()
        return payload


@dataclass(slots=True)
class ProviderSnapshot:
    provider: str
    role: str
    status: str = "unknown"
    venue: str = ""
    freshness_secs: float | None = None
    latency_ms: float | None = None
    missing_rate: float = 0.0
    fallback_mode: str = ""
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ProviderCapabilities:
    provider: str
    asset_classes: list[str] = field(default_factory=list)
    supports_history: bool = False
    supports_market_data: bool = False
    supports_execution: bool = False
    supports_bid_ask: bool = False
    supports_proxy_spread: bool = False
    shadow_only: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionRequest:
    provider: str
    command_id: str
    symbol: str
    cmd: str
    side: str = ""
    lots: float = 0.0
    close_lots: float = 0.0
    trace_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class ExecutionUpdate:
    provider: str
    command_id: str
    status: str
    symbol: str = ""
    ticket: int = -1
    message: str = ""
    trace_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
