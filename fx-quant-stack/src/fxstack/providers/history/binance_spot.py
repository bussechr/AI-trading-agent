from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from fxstack.providers.catalog import enrich_bars_frame, infer_instrument_ref


_TIMEFRAME_MAP = {
    "M1": "1m",
    "M3": "3m",
    "M5": "5m",
    "M15": "15m",
    "M30": "30m",
    "H1": "1h",
    "H2": "2h",
    "H4": "4h",
    "H6": "6h",
    "H8": "8h",
    "H12": "12h",
    "D": "1d",
    "D1": "1d",
    "W": "1w",
    "W1": "1w",
}


def _require_ccxt() -> Any:
    try:
        import ccxt  # type: ignore
    except Exception as exc:  # pragma: no cover - dependency may be optional in CI
        raise RuntimeError("binance_spot provider requires optional dependency 'ccxt'") from exc
    return ccxt


def normalize_exchange_timeframe(timeframe: str) -> str:
    raw = str(timeframe or "").strip()
    if not raw:
        return "5m"
    upper = raw.upper()
    mapped = _TIMEFRAME_MAP.get(upper)
    if mapped:
        return mapped
    return raw.lower()


def normalize_ohlcv_rows(
    rows: list[list[Any]],
    *,
    symbol: str,
    timeframe: str,
    provider: str = "binance_spot",
) -> pd.DataFrame:
    if not rows:
        return pd.DataFrame()
    instrument = infer_instrument_ref(symbol, provider=provider, venue="spot", asset_class="crypto")
    frame = pd.DataFrame(rows, columns=["ts_ms", "mid_open", "mid_high", "mid_low", "mid_close", "volume"])
    frame["ts"] = pd.to_datetime(frame["ts_ms"], unit="ms", utc=True, errors="coerce")
    for field in ["open", "high", "low", "close"]:
        frame[f"bid_{field}"] = pd.to_numeric(frame[f"mid_{field}"], errors="coerce").astype(float)
        frame[f"ask_{field}"] = pd.to_numeric(frame[f"mid_{field}"], errors="coerce").astype(float)
    frame["spread"] = 0.0
    frame["pair"] = str(instrument.canonical_symbol)
    frame = frame.drop(columns=["ts_ms"])
    frame = (
        frame.dropna(subset=["ts", "mid_open", "mid_high", "mid_low", "mid_close"])
        .sort_values("ts")
        .drop_duplicates(subset=["ts"], keep="last")
        .reset_index(drop=True)
    )
    frame["date"] = pd.to_datetime(frame["ts"], utc=True).dt.strftime("%Y-%m-%d")
    return enrich_bars_frame(
        frame,
        instrument=instrument,
        provider=provider,
        timeframe=str(timeframe).upper(),
        provenance="ccxt_binance_spot",
        quality_flags=["proxy_spread"],
    )


def fetch_ohlcv_frame(
    *,
    symbol: str,
    timeframe: str = "5m",
    limit: int = 500,
    exchange_id: str = "binance",
) -> pd.DataFrame:
    ccxt = _require_ccxt()
    exchange_cls = getattr(ccxt, str(exchange_id), None)
    if exchange_cls is None:
        raise RuntimeError(f"unknown ccxt exchange '{exchange_id}'")
    exchange = exchange_cls({"enableRateLimit": True})
    normalized_timeframe = normalize_exchange_timeframe(str(timeframe))
    rows = exchange.fetch_ohlcv(
        str(symbol).upper(),
        timeframe=str(normalized_timeframe),
        limit=max(1, min(int(limit), 2000)),
    )
    return normalize_ohlcv_rows(
        rows,
        symbol=str(symbol).upper(),
        timeframe=str(timeframe).upper(),
        provider="binance_spot",
    )
