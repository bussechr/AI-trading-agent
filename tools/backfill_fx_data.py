#!/usr/bin/env python3
from __future__ import annotations

import argparse
import random
import time
from pathlib import Path

import pandas as pd
import requests
import yaml


YAHOO_HOSTS = (
    "https://query1.finance.yahoo.com",
    "https://query2.finance.yahoo.com",
)


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def parse_symbols(cfg: dict, symbols_arg: str | None) -> list[str]:
    if symbols_arg:
        return sorted({s.strip().upper() for s in symbols_arg.split(",") if s.strip()})
    roots = cfg.get("symbols_roots", [])
    return sorted({str(s).strip().upper() for s in roots if str(s).strip()})


def to_float(v: object, default: float = 0.0) -> float:
    try:
        if v is None:
            return float(default)
        f = float(v)
        if pd.isna(f):
            return float(default)
        return f
    except Exception:
        return float(default)


def fetch_yahoo_ohlc(
    session: requests.Session,
    symbol: str,
    interval: str,
    range_value: str,
    timeout_secs: float,
    retries: int,
) -> tuple[pd.DataFrame | None, str]:
    ticker = f"{symbol}=X"
    endpoint = "/v8/finance/chart/" + ticker
    params = {
        "interval": interval,
        "range": range_value,
    }
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        host = random.choice(YAHOO_HOSTS)
        url = host + endpoint
        try:
            resp = session.get(url, params=params, timeout=timeout_secs)
            if resp.status_code == 429:
                last_err = "rate_limited"
                time.sleep(min(1.5 * attempt, 6.0))
                continue
            resp.raise_for_status()
            payload = resp.json()
            chart = payload.get("chart", {})
            err = chart.get("error")
            if err:
                return None, f"api_error:{err}"
            result = (chart.get("result") or [None])[0]
            if not result:
                return None, "no_result"

            ts = result.get("timestamp") or []
            quote = ((result.get("indicators") or {}).get("quote") or [{}])[0]
            opens = quote.get("open") or []
            highs = quote.get("high") or []
            lows = quote.get("low") or []
            closes = quote.get("close") or []
            vols = quote.get("volume") or []

            rows: list[dict] = []
            n = min(len(ts), len(opens), len(highs), len(lows), len(closes))
            for i in range(n):
                o = opens[i]
                h = highs[i]
                l = lows[i]
                c = closes[i]
                if o is None or h is None or l is None or c is None:
                    continue
                rows.append(
                    {
                        "time": pd.to_datetime(int(ts[i]), unit="s", utc=True).tz_localize(None),
                        "open": to_float(o),
                        "high": to_float(h),
                        "low": to_float(l),
                        "close": to_float(c),
                        "volume": int(to_float(vols[i] if i < len(vols) else 0.0, 0.0)),
                    }
                )

            if not rows:
                return None, "no_ohlc_rows"

            df = pd.DataFrame(rows)
            df = df.dropna(subset=["time", "open", "high", "low", "close"])
            df = df.drop_duplicates(subset=["time"], keep="last").sort_values("time")
            if df.empty:
                return None, "empty_after_clean"
            return df, "ok"
        except Exception as exc:
            last_err = str(exc)
            time.sleep(min(0.75 * attempt, 4.0))
    return None, last_err


def load_existing_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "time"])
    df = pd.read_csv(path)
    cols = {c.lower(): c for c in df.columns}
    out = pd.DataFrame()
    if "time" in cols:
        out["time"] = pd.to_datetime(df[cols["time"]], errors="coerce")
    else:
        out["time"] = pd.NaT
    for col in ("open", "high", "low", "close"):
        out[col] = pd.to_numeric(df.get(cols.get(col, ""), 0.0), errors="coerce")
    if "volume" in cols:
        out["volume"] = pd.to_numeric(df[cols["volume"]], errors="coerce").fillna(0).astype(int)
    else:
        out["volume"] = 0
    out = out.dropna(subset=["time", "open", "high", "low", "close"])
    out = out.drop_duplicates(subset=["time"], keep="last").sort_values("time")
    return out[["open", "high", "low", "close", "volume", "time"]]


def save_merged(path: Path, old_df: pd.DataFrame, new_df: pd.DataFrame, overwrite: bool) -> tuple[int, int]:
    if overwrite or old_df.empty:
        merged = new_df.copy()
    else:
        merged = pd.concat([old_df, new_df], axis=0, ignore_index=True)
        merged = merged.drop_duplicates(subset=["time"], keep="last").sort_values("time")
    merged = merged[["open", "high", "low", "close", "volume", "time"]]
    before = int(len(old_df))
    after = int(len(merged))
    path.parent.mkdir(parents=True, exist_ok=True)
    merged.to_csv(path, index=False)
    return before, after


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill FX OHLC CSVs from Yahoo Finance chart API.")
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--out-dir", default="")
    ap.add_argument("--symbols", default="", help="Comma-separated symbol roots (e.g., EURUSD,GBPUSD).")
    ap.add_argument("--interval", default="60m")
    ap.add_argument("--range", default="730d")
    ap.add_argument("--sleep-ms", type=int, default=220)
    ap.add_argument("--timeout-secs", type=float, default=20.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Optional symbol limit for smoke runs.")
    args = ap.parse_args()

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    out_dir = Path(args.out_dir) if args.out_dir else Path(str(cfg.get("data_dir", "data/fx_minis")))
    symbols = parse_symbols(cfg, args.symbols if args.symbols.strip() else None)
    if args.limit and args.limit > 0:
        symbols = symbols[: int(args.limit)]

    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "keep-alive",
        }
    )

    ok = 0
    fail = 0
    total_added = 0
    started = time.time()
    print(f"Backfill start: symbols={len(symbols)} interval={args.interval} range={args.range} out={out_dir}")
    for i, sym in enumerate(symbols, start=1):
        df_new, status = fetch_yahoo_ohlc(
            sess,
            sym,
            args.interval,
            args.range,
            timeout_secs=float(args.timeout_secs),
            retries=int(args.retries),
        )
        if df_new is None:
            fail += 1
            print(f"[{i:03d}/{len(symbols):03d}] {sym}: FAIL ({status})")
            time.sleep(max(args.sleep_ms, 0) / 1000.0)
            continue

        path = out_dir / f"{sym}.csv"
        old_df = load_existing_csv(path)
        before, after = save_merged(path, old_df, df_new, overwrite=bool(args.overwrite))
        added = max(after - before, 0)
        total_added += added
        ok += 1
        print(
            f"[{i:03d}/{len(symbols):03d}] {sym}: OK "
            f"new={len(df_new)} merged={after} added={added}"
        )
        time.sleep(max(args.sleep_ms, 0) / 1000.0)

    elapsed = time.time() - started
    print(
        f"Backfill complete: ok={ok} fail={fail} total={len(symbols)} "
        f"added_rows={total_added} elapsed={elapsed:.1f}s"
    )


if __name__ == "__main__":
    main()
