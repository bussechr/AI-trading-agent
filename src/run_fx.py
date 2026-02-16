from __future__ import annotations
import argparse, os, time, yaml, glob, logging
import pandas as pd

from agents.fx_el_hawkes_agent import FXELAgent
from agents.fx_el_hawkes_agent import MINI_SUFFIXES_DEFAULT
from validation.agent_validator import AgentValidator

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

MAX_BARS = 500  # Rolling window cap — keeps RAM constant

def load_cfg(path: str) -> dict:
    with open(path, "r") as f: return yaml.safe_load(f)

def read_csv_bars(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # expected columns: time, open, high, low, close
    if "time" in df.columns:
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time")
        df = df.set_index("time")
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

# Simple in-memory cache: {symbol: (mtime, dataframe)}
_MD_CACHE = {}

def fetch_market_data(data_dir: str, symbols: list[str], lookback: int) -> dict[str, pd.DataFrame]:
    """
    Fetch market data with caching.
    Only re-reads file if modification time has changed.
    """
    global _MD_CACHE
    md = {}
    
    for s in symbols:
        p = os.path.join(data_dir, f"{s}.csv")
        if not os.path.exists(p): continue
        
        try:
            mtime = os.path.getmtime(p)
            
            # Check cache
            cached_mtime, cached_df = _MD_CACHE.get(s, (0, None))
            
            if mtime > cached_mtime or cached_df is None:
                # File changed or not in cache -> read it
                df = read_csv_bars(p)
                _MD_CACHE[s] = (mtime, df)
            else:
                # Use cached version
                df = cached_df
            
            # Take last MAX_BARS rows only — keeps RAM bounded
            md[s] = df.iloc[-MAX_BARS:].copy() if len(df) > MAX_BARS else df.copy()
            
        except Exception as e:
            logger.error(f"Error reading {s}: {e}")
            
    return md

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="src/config/fx_el_minis.yaml")
    ap.add_argument("--equity", type=float, required=True)
    ap.add_argument("--sleep", type=int, default=5)  # sped up from 55s for live reactivity
    ap.add_argument("--skip-validation", action="store_true", help="Skip startup validation")
    args = ap.parse_args()

    cfg = load_cfg(args.config)
    
    # A. VALIDATION CHECKLIST - Run before starting
    if not args.skip_validation:
        logger.info("Running startup validation...")
        validator = AgentValidator(cfg)
        if not validator.validate_all():
            logger.error("Validation failed! Fix issues before starting.")
            logger.error("Use --skip-validation to bypass (not recommended)")
            return 1
        logger.info("✓ All validation checks passed\n")
    
    agent = FXELAgent(cfg)

    data_dir = cfg["data_dir"]
    mini_suffixes = cfg.get("mini_suffixes", MINI_SUFFIXES_DEFAULT)
    catalog = build_catalog(data_dir, cfg["symbols_roots"], mini_suffixes)

    # A. Log mini-only universe on startup
    logger.info("=" * 60)
    logger.info("FX EL HAWKES AGENT - STARTUP")
    logger.info("=" * 60)
    logger.info(f"Account equity: ${args.equity:,.2f}")
    logger.info(f"Config: {args.config}")
    logger.info(f"Update interval: {args.sleep}s")
    logger.info(f"\nMINI UNIVERSE ({len(catalog)} symbols):")
    for sym in catalog:
        logger.info(f"  • {sym}")
    logger.info("=" * 60 + "\n")
    
    # B. CONNECTION CHECK - Ensure Bridge Server is running
    from execution.mt4_bridge_client import check_connection
    print("Checking connection to MT4 Bridge Server...", end="", flush=True)
    if not check_connection():
        print(" FAILED!")
        logger.error("CRITICAL: Bridge Server is NOT running at http://127.0.0.1:58710")
        logger.error("Please start 'start.bat' or 'run_bridge.bat' FIRST.")
        # We exit to prevent blind running
        return
    print(" OK.")

    # Load historical data ONCE at startup
    logger.info("Loading historical market data...")
    md = fetch_market_data(data_dir, catalog, cfg["lookback_bars"])
    logger.info(f"Loaded market data for {len(md)} symbols")

    iteration = 0
    while True:
        iteration += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"ITERATION {iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")
        
        # md is now persistent across iterations
        
        # --- LIVE DATA PATCH ---
        # --- LIVE DATA PATCH ---
        from execution.mt4_bridge_client import get_ticks, get_account_info
        
        # 1. Update Equity from Bridge
        acct = get_account_info()
        if "equity" in acct:
            current_equity = float(acct["equity"])
            # User request: "take the margin". We log it for now as agent uses equity for sizing.
            margin = float(acct.get("margin", 0))
            free = float(acct.get("freemargin", 0))
            logger.info(f"ACCOUNT: Equity={current_equity:.2f}, Margin={margin:.2f}, Free={free:.2f}")
        else:
            current_equity = args.equity

        ticks = get_ticks()
        for sym, data in ticks.items():
            if sym in md:
                try:
                    bid = data["bid"]
                    ask = data["ask"]
                    mid = (bid + ask) / 2.0
                    
                    # 2. RAM DATABASE / BAR BUILDING
                    # Check if we need to start a new bar (H1)
                    last_time = md[sym].index[-1]
                    # Assuming index is naive datetime matching system time
                    import pandas as pd
                    current_bar_time = pd.Timestamp.now().replace(minute=0, second=0, microsecond=0)
                    
                    if current_bar_time > last_time:
                        # Close the previous bar (final update) and start new one
                        logger.info(f"{sym}: NEW BAR {current_bar_time} (Prev: {last_time})")
                        # Create new row starting at previous close
                        prev_close = md[sym].iloc[-1]["close"]
                        new_row = pd.DataFrame({
                            "open": [prev_close],
                            "high": [prev_close],
                            "low": [prev_close],
                            "close": [prev_close],
                            "volume": [0]
                        }, index=[current_bar_time])
                        
                        # Append to dataframe and trim to rolling window
                        saved_attrs = md[sym].attrs.copy()
                        md[sym] = pd.concat([md[sym], new_row]).iloc[-MAX_BARS:]
                        md[sym].attrs = saved_attrs

                    # 3. Update current bar (live patch)
                    # Update close
                    md[sym].loc[md[sym].index[-1], "close"] = mid
                    # Update high/low
                    if mid > md[sym].loc[md[sym].index[-1], "high"]:
                        md[sym].loc[md[sym].index[-1], "high"] = mid
                    if mid < md[sym].loc[md[sym].index[-1], "low"]:
                        md[sym].loc[md[sym].index[-1], "low"] = mid
                    
                    # Store live spread for agent to use
                    if "spread" in data:
                        md[sym].attrs["spread"] = float(data["spread"])
                        
                except Exception as e:
                    logger.warning(f"Failed to patch {sym}: {e}")
        # -----------------------
        
        logger.info(f"Loaded market data for {len(md)} symbols")
        
        agent.act(current_equity, md, all_symbols_catalog=catalog)
        
        # F. Log rejection statistics
        if iteration % 10 == 0 and agent.rejection_stats:
            logger.info("\nREJECTION STATS (last 10 iterations):")
            for reason, count in sorted(agent.rejection_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {reason}: {count}")
        
        time.sleep(args.sleep)

if __name__ == "__main__":
    main()
