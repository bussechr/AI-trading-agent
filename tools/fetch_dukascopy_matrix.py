from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pandas as pd


DEFAULT_PAIRS = [
    "EURUSD",
    "USDJPY",
    "GBPUSD",
    "AUDUSD",
    "USDCHF",
    "USDCAD",
    "NZDUSD",
    "EURJPY",
    "EURGBP",
    "GBPJPY",
    "EURCHF",
    "AUDJPY",
    "EURAUD",
    "CADJPY",
    "CHFJPY",
    "GBPCHF",
    "EURCAD",
    "GBPCAD",
]
DEFAULT_TIMEFRAMES = ["M1", "M5", "M15", "H4", "D"]
RESUME_OVERLAP_MINUTES = 60
RESAMPLE_RULES = {
    "M1": "1min",
    "M5": "5min",
    "M15": "15min",
    "H4": "4h",
    "D": "1d",
}


@dataclass(slots=True)
class PairResult:
    pair: str
    instrument: str
    status: str
    files: dict[str, dict[str, Any]]
    error: str = ""



def _parse_csv_list(raw: str) -> list[str]:
    out: list[str] = []
    for part in str(raw or "").split(","):
        sym = str(part).strip().upper()
        if sym:
            out.append(sym)
    return out



def _parse_dt(raw: str, *, default_now: bool) -> datetime:
    txt = str(raw or "").strip()
    if not txt:
        if default_now:
            return datetime.now(timezone.utc)
        raise ValueError("datetime is required")

    if txt.endswith("Z"):
        txt = txt[:-1] + "+00:00"

    try:
        dt = datetime.fromisoformat(txt)
    except Exception:
        dt = datetime.fromisoformat(f"{txt}T00:00:00+00:00")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)



def _resolve_instrument(pair: str) -> tuple[str, str]:
    import dukascopy_python.instruments as inst

    p = str(pair).strip().upper()
    if len(p) != 6:
        raise ValueError(f"invalid pair '{pair}'")
    base = p[:3]
    quote = p[3:]

    candidates = [
        f"INSTRUMENT_FX_MAJORS_{base}_{quote}",
        f"INSTRUMENT_FX_MINORS_{base}_{quote}",
        f"INSTRUMENT_FX_EXOTICS_{base}_{quote}",
    ]
    for key in candidates:
        if hasattr(inst, key):
            return str(getattr(inst, key)), key

    suffix = f"_{base}_{quote}"
    for key in dir(inst):
        if key.startswith("INSTRUMENT_FX_") and key.endswith(suffix):
            return str(getattr(inst, key)), key

    raise ValueError(f"dukascopy instrument not found for pair '{pair}'")



def _normalize_side_df(df: pd.DataFrame, *, prefix: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()

    x = df.copy()
    if "timestamp" in x.columns:
        ts = pd.to_datetime(x["timestamp"], utc=True, errors="coerce")
    else:
        ts = pd.to_datetime(x.index, utc=True, errors="coerce")
    x = x.reset_index(drop=True)
    x["timestamp"] = ts
    x = x.dropna(subset=["timestamp"])

    for col in ["open", "high", "low", "close", "volume"]:
        if col not in x.columns:
            x[col] = 0.0
        x[col] = pd.to_numeric(x[col], errors="coerce")

    out = x[["timestamp", "open", "high", "low", "close", "volume"]].rename(
        columns={
            "open": f"{prefix}_open",
            "high": f"{prefix}_high",
            "low": f"{prefix}_low",
            "close": f"{prefix}_close",
            "volume": f"{prefix}_volume",
        }
    )
    out = out.dropna(subset=[f"{prefix}_open", f"{prefix}_high", f"{prefix}_low", f"{prefix}_close"])
    out = out.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return out



def _load_existing_m1(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_csv(path)
    if "timestamp" not in df.columns:
        return pd.DataFrame()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    cols = [
        "bid_open",
        "bid_high",
        "bid_low",
        "bid_close",
        "ask_open",
        "ask_high",
        "ask_low",
        "ask_close",
    ]
    for col in cols + ["volume"]:
        if col not in df.columns:
            return pd.DataFrame()
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["timestamp"] + cols)
    df = df.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return df


def _merge_m1_frames(existing: pd.DataFrame, fetched: pd.DataFrame) -> pd.DataFrame:
    if existing.empty:
        return fetched.copy()
    if fetched.empty:
        return existing.copy()
    merged = pd.concat([existing, fetched], ignore_index=True)
    merged["timestamp"] = pd.to_datetime(merged["timestamp"], utc=True, errors="coerce")
    merged = merged.dropna(subset=["timestamp"])
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return merged



def _fetch_m1_bid_ask(
    *,
    instrument: str,
    start: datetime,
    end: datetime,
    max_retries: int,
    limit: int,
    debug: bool,
    mid_only_fallback: bool,
) -> pd.DataFrame:
    import dukascopy_python as d

    bid = d.fetch(
        instrument,
        d.INTERVAL_MIN_1,
        d.OFFER_SIDE_BID,
        start,
        end,
        max_retries=max_retries,
        limit=limit,
        debug=debug,
    )
    ask = d.fetch(
        instrument,
        d.INTERVAL_MIN_1,
        d.OFFER_SIDE_ASK,
        start,
        end,
        max_retries=max_retries,
        limit=limit,
        debug=debug,
    )

    bid_n = _normalize_side_df(bid, prefix="bid")
    ask_n = _normalize_side_df(ask, prefix="ask")

    if bid_n.empty and ask_n.empty:
        raise RuntimeError("dukascopy fetch returned empty bid/ask frames")
    if bid_n.empty or ask_n.empty:
        if not mid_only_fallback:
            raise RuntimeError("one of bid/ask frames is empty; use --mid-only-fallback to allow synthetic side")
        if bid_n.empty:
            bid_n = ask_n.rename(
                columns={
                    "ask_open": "bid_open",
                    "ask_high": "bid_high",
                    "ask_low": "bid_low",
                    "ask_close": "bid_close",
                    "ask_volume": "bid_volume",
                }
            )
        if ask_n.empty:
            ask_n = bid_n.rename(
                columns={
                    "bid_open": "ask_open",
                    "bid_high": "ask_high",
                    "bid_low": "ask_low",
                    "bid_close": "ask_close",
                    "bid_volume": "ask_volume",
                }
            )

    merged = bid_n.merge(ask_n, on="timestamp", how="inner")
    if merged.empty:
        raise RuntimeError("bid/ask merge produced zero rows")

    merged["volume"] = merged["bid_volume"].fillna(0.0) + merged["ask_volume"].fillna(0.0)
    merged = merged[
        [
            "timestamp",
            "bid_open",
            "bid_high",
            "bid_low",
            "bid_close",
            "ask_open",
            "ask_high",
            "ask_low",
            "ask_close",
            "volume",
        ]
    ]
    merged = merged.dropna(
        subset=["bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"]
    )
    merged = merged.drop_duplicates(subset=["timestamp"], keep="last").sort_values("timestamp")
    return merged



def _resample_bid_ask(df_m1: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    tf = str(timeframe).upper()
    if tf not in RESAMPLE_RULES:
        raise ValueError(f"unsupported timeframe '{timeframe}'")
    if tf == "M1":
        return df_m1.copy()

    rule = RESAMPLE_RULES[tf]
    x = df_m1.copy()
    x = x.dropna(subset=["timestamp"])
    x["timestamp"] = pd.to_datetime(x["timestamp"], utc=True, errors="coerce")
    x = x.dropna(subset=["timestamp"])
    x = x.sort_values("timestamp").set_index("timestamp")

    agg = x.resample(rule).agg(
        {
            "bid_open": "first",
            "bid_high": "max",
            "bid_low": "min",
            "bid_close": "last",
            "ask_open": "first",
            "ask_high": "max",
            "ask_low": "min",
            "ask_close": "last",
            "volume": "sum",
        }
    )
    agg = agg.dropna(
        subset=["bid_open", "bid_high", "bid_low", "bid_close", "ask_open", "ask_high", "ask_low", "ask_close"]
    )
    agg = agg.reset_index()
    return agg



def _write_csv(path: Path, df: pd.DataFrame) -> int:
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.dropna(subset=["timestamp"])
    out = out.sort_values("timestamp")
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    return int(len(out))



def run(args: argparse.Namespace) -> int:
    pairs = _parse_csv_list(args.pairs) or list(DEFAULT_PAIRS)
    timeframes = _parse_csv_list(args.timeframes) or list(DEFAULT_TIMEFRAMES)
    source_root = Path(str(args.source_root)).expanduser()
    source_root.mkdir(parents=True, exist_ok=True)

    start = _parse_dt(str(args.start), default_now=False)
    end = _parse_dt(str(args.end), default_now=True)
    if end <= start:
        raise SystemExit("end must be after start")

    summary: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_root": str(source_root),
        "pairs": pairs,
        "timeframes": timeframes,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "resume": bool(args.resume),
        "overwrite": bool(args.overwrite),
        "results": [],
    }

    failures = 0
    for pair in pairs:
        files: dict[str, dict[str, Any]] = {}
        try:
            instrument, instrument_key = _resolve_instrument(pair)
            m1_path = source_root / f"{pair}_M1.csv"

            df_existing = pd.DataFrame()
            if m1_path.exists() and bool(args.resume) and (not bool(args.overwrite)):
                df_existing = _load_existing_m1(m1_path)

            df_m1 = df_existing.copy()
            refreshed = False
            if df_existing.empty:
                df_m1 = _fetch_m1_bid_ask(
                    instrument=instrument,
                    start=start,
                    end=end,
                    max_retries=int(args.max_retries),
                    limit=int(args.limit),
                    debug=bool(args.debug),
                    mid_only_fallback=bool(args.mid_only_fallback),
                )
                refreshed = True
            else:
                last_ts = pd.to_datetime(df_existing["timestamp"].max(), utc=True, errors="coerce")
                fetch_start = max(start, (last_ts - timedelta(minutes=RESUME_OVERLAP_MINUTES)).to_pydatetime())
                if end > fetch_start:
                    try:
                        fetched = _fetch_m1_bid_ask(
                            instrument=instrument,
                            start=fetch_start,
                            end=end,
                            max_retries=int(args.max_retries),
                            limit=int(args.limit),
                            debug=bool(args.debug),
                            mid_only_fallback=bool(args.mid_only_fallback),
                        )
                    except RuntimeError as exc:
                        msg = str(exc).lower()
                        if "empty bid/ask" in msg or "zero rows" in msg:
                            fetched = pd.DataFrame()
                        else:
                            raise
                    if not fetched.empty:
                        df_m1 = _merge_m1_frames(df_existing, fetched)
                        refreshed = len(df_m1) > len(df_existing) or not df_m1.equals(df_existing)

            rows = _write_csv(m1_path, df_m1)
            files["M1"] = {
                "path": str(m1_path),
                "rows": rows,
                "source": "refreshed" if refreshed else ("existing" if not df_existing.empty else "fetched"),
            }

            for timeframe in [tf for tf in timeframes if tf != "M1"]:
                tf_u = str(timeframe).upper()
                out_path = source_root / f"{pair}_{tf_u}.csv"

                df_tf = _resample_bid_ask(df_m1, tf_u)
                rows = _write_csv(out_path, df_tf)
                files[tf_u] = {"path": str(out_path), "rows": rows, "source": "resampled" if refreshed or not out_path.exists() else "rebuilt"}

            summary["results"].append(
                asdict(
                    PairResult(
                        pair=pair,
                        instrument=instrument_key,
                        status="ok",
                        files=files,
                        error="",
                    )
                )
            )
        except Exception as exc:
            failures += 1
            summary["results"].append(
                asdict(
                    PairResult(
                        pair=pair,
                        instrument="",
                        status="error",
                        files=files,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
            )

    summary["failed_pairs"] = int(failures)
    summary["passed"] = bool(failures == 0)
    print(json.dumps(summary, indent=2, sort_keys=True))

    out_path = str(args.out or "").strip()
    if out_path:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")

    return 0 if bool(summary["passed"]) else 2



def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Download Dukascopy M1 bid/ask and build M5/M15/H4/D matrix CSVs.")
    ap.add_argument("--source-root", default="fx-quant-stack/data/dukascopy")
    ap.add_argument("--pairs", default=",".join(DEFAULT_PAIRS))
    ap.add_argument("--timeframes", default=",".join(DEFAULT_TIMEFRAMES))
    ap.add_argument("--start", default="2024-01-01T00:00:00Z")
    ap.add_argument("--end", default="")
    ap.add_argument("--max-retries", type=int, default=7)
    ap.add_argument("--limit", type=int, default=30000)
    ap.add_argument("--debug", action="store_true", default=False)
    ap.add_argument("--mid-only-fallback", action="store_true", default=False)
    ap.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    ap.add_argument("--overwrite", action="store_true", default=False)
    ap.add_argument("--out", default="")
    return ap



def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(int(run(args) or 0))


if __name__ == "__main__":
    main()
