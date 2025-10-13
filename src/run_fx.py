from __future__ import annotations
import argparse, os, time, yaml, glob
import pandas as pd

from agents.fx_el_hawkes_agent import FXELAgent
from agents.fx_el_hawkes_agent import MINI_SUFFIXES_DEFAULT

def load_cfg(path: str) -> dict:
    with open(path, "r") as f: return yaml.safe_load(f)

def read_csv_bars(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # expected columns: time, open, high, low, close
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time")
    return df

def build_catalog(data_dir: str, roots: list[str], mini_suffixes: list[str]) -> list[str]:
    files = glob.glob(os.path.join(data_dir, "*.csv"))
    cats = []
    lowers = [s.lower() for s in mini_suffixes]
    for f in files:
        sym = os.path.splitext(os.path.basename(f))[0]  # e.g., EURUSD.MINI or EURUSD (for IG)
        if not any(r in sym.upper() for r in roots): continue
        # For IG (no suffixes), all symbols in roots are valid minis (traded at 0.10 lot)
        if not mini_suffixes:  # IG mode - no suffix filtering
            cats.append(sym)
        else:  # Generic mode with suffixes
            low = sym.lower()
            if any(low.endswith(suf) or (suf in low) for suf in lowers):
                cats.append(sym)
    return sorted(list(dict.fromkeys(cats)))

def fetch_market_data(data_dir: str, symbols: list[str], lookback: int) -> dict[str, pd.DataFrame]:
    md = {}
    for s in symbols:
        p = os.path.join(data_dir, f"{s}.csv")
        if not os.path.exists(p): continue
        df = read_csv_bars(p)
        md[s] = df.iloc[-lookback:] if len(df) > lookback else df
    return md

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--sleep", type=int, default=55)
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    agent = FXELAgent(cfg)

    data_dir = cfg["data_dir"]
    mini_suffixes = cfg.get("mini_suffixes", MINI_SUFFIXES_DEFAULT)
    catalog = build_catalog(data_dir, cfg["symbols_roots"], mini_suffixes)

    print(f"FX EL Agent starting...")
    print(f"Account equity: ${args.equity:,.2f}")
    print(f"Universe: {len(catalog)} symbols")
    print(f"Update interval: {args.sleep}s")
    
    while True:
        md = fetch_market_data(data_dir, catalog, cfg["lookback_bars"])
        agent.act(args.equity, md, all_symbols_catalog=catalog)
        time.sleep(args.sleep)

if __name__ == "__main__":
    main()
