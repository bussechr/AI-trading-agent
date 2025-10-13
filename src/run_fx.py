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
    
    iteration = 0
    while True:
        iteration += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"ITERATION {iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")
        
        md = fetch_market_data(data_dir, catalog, cfg["lookback_bars"])
        logger.info(f"Loaded market data for {len(md)} symbols")
        
        agent.act(args.equity, md, all_symbols_catalog=catalog)
        
        # F. Log rejection statistics
        if iteration % 10 == 0 and agent.rejection_stats:
            logger.info("\nREJECTION STATS (last 10 iterations):")
            for reason, count in sorted(agent.rejection_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {reason}: {count}")
        
        time.sleep(args.sleep)

if __name__ == "__main__":
    main()
