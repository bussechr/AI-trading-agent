from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import pandas as pd

from fxstack.io.parquet_store import ParquetStore


@dataclass(slots=True)
class IngestResult:
    pair: str
    timeframe: str
    rows: int
    path: str
    provider: str = ""


def _norm_col(name: str) -> str:
    return str(name).strip().lower().replace("_", " ")


def _find_col(col_map: dict[str, str], aliases: Sequence[str]) -> str | None:
    for alias in aliases:
        key = _norm_col(alias)
        value = col_map.get(key)
        if value:
            return value
    return None


def _require_col(col_map: dict[str, str], aliases: Sequence[str], field_name: str) -> str:
    col = _find_col(col_map, aliases)
    if not col:
        raise ValueError(f"missing required column for {field_name}")
    return col


def _parse_ts(df: pd.DataFrame, col_map: dict[str, str]) -> pd.Series:
    dt_col = _find_col(
        col_map,
        (
            "timestamp",
            "datetime",
            "date time",
            "gmt time",
            "gmt_time",
            "ts",
        ),
    )
    if dt_col:
        return pd.to_datetime(df[dt_col], utc=True, errors="coerce")

    date_col = _find_col(col_map, ("date",))
    time_col = _find_col(col_map, ("time", "hour", "clock", "tod"))
    if date_col and time_col:
        dt = df[date_col].astype(str).str.strip() + " " + df[time_col].astype(str).str.strip()
        return pd.to_datetime(dt, utc=True, errors="coerce")
    if date_col:
        return pd.to_datetime(df[date_col], utc=True, errors="coerce")

    time_only_col = _find_col(col_map, ("time",))
    if time_only_col:
        return pd.to_datetime(df[time_only_col], utc=True, errors="coerce")

    raise ValueError("missing required timestamp column")


def _to_float_series(df: pd.DataFrame, col_name: str) -> pd.Series:
    return pd.to_numeric(df[col_name], errors="coerce").astype(float)


def _extract_side(
    df: pd.DataFrame,
    col_map: dict[str, str],
    *,
    prefix: str,
    allow_generic: bool,
) -> tuple[pd.Series | None, pd.Series | None, pd.Series | None, pd.Series | None]:
    def _aliases(field: str) -> tuple[str, ...]:
        if prefix:
            return (
                f"{prefix}_{field}",
                f"{prefix} {field}",
                f"{prefix}{field}",
                f"{field}_{prefix}",
                f"{field} {prefix}",
                f"{field}{prefix}",
            )
        return (field,)

    open_col = _find_col(col_map, _aliases("open"))
    high_col = _find_col(col_map, _aliases("high"))
    low_col = _find_col(col_map, _aliases("low"))
    close_col = _find_col(col_map, _aliases("close"))

    if allow_generic:
        open_col = open_col or _find_col(col_map, ("open",))
        high_col = high_col or _find_col(col_map, ("high",))
        low_col = low_col or _find_col(col_map, ("low",))
        close_col = close_col or _find_col(col_map, ("close",))

    if not (open_col and high_col and low_col and close_col):
        return None, None, None, None

    return (
        _to_float_series(df, open_col),
        _to_float_series(df, high_col),
        _to_float_series(df, low_col),
        _to_float_series(df, close_col),
    )


def normalize_dukascopy_bars(*, raw: pd.DataFrame, pair: str, timeframe: str) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()

    col_map: dict[str, str] = {}
    for col in raw.columns:
        key = _norm_col(col)
        if key and key not in col_map:
            col_map[key] = str(col)

    ts = _parse_ts(raw, col_map)
    vol_col = _find_col(col_map, ("volume", "vol", "tick volume", "ticks", "quantity"))
    volume = _to_float_series(raw, vol_col) if vol_col else pd.Series([0.0] * len(raw), index=raw.index)

    mid_o, mid_h, mid_l, mid_c = _extract_side(raw, col_map, prefix="mid", allow_generic=True)
    bid_o, bid_h, bid_l, bid_c = _extract_side(raw, col_map, prefix="bid", allow_generic=False)
    ask_o, ask_h, ask_l, ask_c = _extract_side(raw, col_map, prefix="ask", allow_generic=False)

    has_mid = all(x is not None for x in (mid_o, mid_h, mid_l, mid_c))
    has_bid = all(x is not None for x in (bid_o, bid_h, bid_l, bid_c))
    has_ask = all(x is not None for x in (ask_o, ask_h, ask_l, ask_c))
    if not has_mid and not (has_bid and has_ask):
        raise ValueError("csv must provide either mid OHLC or both bid/ask OHLC")

    if not has_mid and has_bid and has_ask:
        assert bid_o is not None and bid_h is not None and bid_l is not None and bid_c is not None
        assert ask_o is not None and ask_h is not None and ask_l is not None and ask_c is not None
        mid_o = (bid_o + ask_o) / 2.0
        mid_h = (bid_h + ask_h) / 2.0
        mid_l = (bid_l + ask_l) / 2.0
        mid_c = (bid_c + ask_c) / 2.0
        has_mid = True

    if has_mid and not has_bid:
        assert mid_o is not None and mid_h is not None and mid_l is not None and mid_c is not None
        bid_o, bid_h, bid_l, bid_c = mid_o.copy(), mid_h.copy(), mid_l.copy(), mid_c.copy()
        has_bid = True

    if has_mid and not has_ask:
        assert mid_o is not None and mid_h is not None and mid_l is not None and mid_c is not None
        ask_o, ask_h, ask_l, ask_c = mid_o.copy(), mid_h.copy(), mid_l.copy(), mid_c.copy()
        has_ask = True

    assert has_mid and has_bid and has_ask
    assert mid_o is not None and mid_h is not None and mid_l is not None and mid_c is not None
    assert bid_o is not None and bid_h is not None and bid_l is not None and bid_c is not None
    assert ask_o is not None and ask_h is not None and ask_l is not None and ask_c is not None

    pair_u = str(pair).upper()
    tf_u = str(timeframe).upper()
    out = pd.DataFrame(
        {
            "pair": pair_u,
            "ts": ts,
            "timeframe": tf_u,
            "bid_open": bid_o,
            "bid_high": bid_h,
            "bid_low": bid_l,
            "bid_close": bid_c,
            "ask_open": ask_o,
            "ask_high": ask_h,
            "ask_low": ask_l,
            "ask_close": ask_c,
            "mid_open": mid_o,
            "mid_high": mid_h,
            "mid_low": mid_l,
            "mid_close": mid_c,
            "volume": volume,
        }
    )
    out["spread"] = (out["ask_close"] - out["bid_close"]).astype(float)
    out = out.dropna(subset=["ts", "mid_open", "mid_high", "mid_low", "mid_close"])
    out = out.drop_duplicates(subset=["pair", "ts", "timeframe"], keep="last")
    out = out.sort_values("ts").reset_index(drop=True)
    out["date"] = pd.to_datetime(out["ts"], utc=True).dt.strftime("%Y-%m-%d")
    return out


def load_dukascopy_csv(*, csv_path: Path, pair: str, timeframe: str) -> pd.DataFrame:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"csv file not found: {path}")
    raw = pd.read_csv(path)
    return normalize_dukascopy_bars(raw=raw, pair=pair, timeframe=timeframe)


def ingest_dukascopy_csv(
    *,
    store_root: Path,
    pair: str,
    timeframe: str,
    csv_path: Path,
    provider: str = "dukascopy",
) -> IngestResult:
    df = load_dukascopy_csv(csv_path=Path(csv_path), pair=pair, timeframe=timeframe)
    if df.empty:
        return IngestResult(pair=pair, timeframe=timeframe, rows=0, path=str(store_root), provider=str(provider).strip().lower())
    store = ParquetStore(store_root)
    out = store.write_partitioned(df, provider=str(provider).strip().lower(), pair=pair, timeframe=timeframe)
    return IngestResult(pair=pair, timeframe=timeframe, rows=int(len(df)), path=str(out), provider=str(provider).strip().lower())


def ingest_provider_frame(
    *,
    store_root: Path,
    provider: str,
    pair: str,
    timeframe: str,
    frame: pd.DataFrame,
) -> IngestResult:
    df = pd.DataFrame(frame or {}).copy()
    if df.empty:
        return IngestResult(pair=pair, timeframe=timeframe, rows=0, path=str(store_root), provider=str(provider).strip().lower())
    provider_key = str(provider).strip().lower()
    pair_key = str(pair).upper()
    timeframe_key = str(timeframe).upper()
    df["pair"] = pair_key
    df["timeframe"] = timeframe_key
    store = ParquetStore(store_root)
    out = store.write_partitioned(df, provider=provider_key, pair=pair_key, timeframe=timeframe_key)
    return IngestResult(pair=pair_key, timeframe=timeframe_key, rows=int(len(df)), path=str(out), provider=provider_key)


def load_silver_bars(*, store_root: Path, pair: str, timeframe: str, provider: str) -> pd.DataFrame:
    store = ParquetStore(store_root)
    return store.read_pair_timeframe(provider=str(provider).strip().lower(), pair=pair, timeframe=timeframe)
