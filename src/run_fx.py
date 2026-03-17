from __future__ import annotations
import argparse, os, time, yaml, glob, logging, json, random
from pathlib import Path
import pandas as pd

try:
    from agents.fx_el_hawkes_agent import FXELAgent
    from agents.fx_el_hawkes_agent import MINI_SUFFIXES_DEFAULT
    from validation.agent_validator import AgentValidator
except ImportError:  # Package mode: import via src.*
    from src.agents.fx_el_hawkes_agent import FXELAgent
    from src.agents.fx_el_hawkes_agent import MINI_SUFFIXES_DEFAULT
    from src.validation.agent_validator import AgentValidator

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

MAX_BARS = 500  # Rolling window cap — keeps RAM constant


def _interop_mode(raw: str) -> str:
    mode = str(raw or "live_shadow").strip().lower()
    if mode not in {"live_shadow", "replay_live_like", "replay_offline"}:
        return "live_shadow"
    return mode


def _emit_compute_trace(path: str, row: dict, *, sample_rate: float = 1.0) -> None:
    if not path:
        return
    sr = float(max(0.0, min(1.0, sample_rate)))
    if sr < 1.0 and random.random() > sr:
        return
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(dict(row), sort_keys=True) + "\n")
    except Exception as exc:
        logger.debug("compute trace write failed: %s", exc)

def load_cfg(path: str) -> dict:
    with open(path, "r") as f: return yaml.safe_load(f)

def _parse_mixed_timestamps(ts: pd.Series) -> pd.Series:
    # MT4 historical files can mix second and microsecond precision timestamps.
    parsed = pd.to_datetime(ts, format="%Y-%m-%d %H:%M:%S", errors="coerce")
    missing = parsed.isna()
    if missing.any():
        parsed.loc[missing] = pd.to_datetime(
            ts.loc[missing], format="%Y-%m-%d %H:%M:%S.%f", errors="coerce"
        )
    if parsed.isna().any():
        # Final tolerant pass for any remaining valid datetime strings.
        still_missing = parsed.isna()
        parsed.loc[still_missing] = pd.to_datetime(ts.loc[still_missing], errors="coerce")
    return parsed

def read_csv_bars(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    # expected columns: time, open, high, low, close
    if "time" in df.columns:
        parsed_time = _parse_mixed_timestamps(df["time"])
        bad_rows = int(parsed_time.isna().sum())
        if bad_rows:
            logger.warning(
                "Dropping %d rows with invalid timestamps in %s",
                bad_rows,
                os.path.basename(path),
            )
        df = df.loc[parsed_time.notna()].copy()
        df["time"] = parsed_time.loc[parsed_time.notna()]
        df = df.sort_values("time").set_index("time")
    return df


def _bridge_bars_to_df(rows: list[dict]) -> pd.DataFrame:
    """Convert bridge /bars payload rows into OHLC dataframe indexed by time."""
    if not rows:
        return pd.DataFrame()
    try:
        df = pd.DataFrame(list(rows))
    except Exception:
        return pd.DataFrame()
    if df.empty or "time" not in df.columns:
        return pd.DataFrame()
    ts = pd.to_datetime(df["time"], errors="coerce", utc=True)
    df = df.loc[ts.notna()].copy()
    if df.empty:
        return pd.DataFrame()
    df["time"] = ts.loc[ts.notna()].dt.tz_localize(None)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        else:
            df[col] = 0.0
    df = df.dropna(subset=["open", "high", "low", "close"]).copy()
    if df.empty:
        return pd.DataFrame()
    return df.sort_values("time").set_index("time")[["open", "high", "low", "close", "volume"]]


def _resolve_synthetic_fill_steps(
    *,
    gap_hours: int,
    gap_recovery_enabled: bool,
    max_synth_bars: int,
) -> tuple[int, bool]:
    """Return synthetic fill length and whether original gap was truncated."""
    if gap_hours <= 0:
        return 0, False
    fill_steps = int(gap_hours)
    if (not bool(gap_recovery_enabled)) and fill_steps > 1:
        fill_steps = 1
    else:
        fill_steps = min(fill_steps, int(max(1, max_synth_bars)))
    return int(fill_steps), bool(fill_steps < int(gap_hours))


def _synthetic_fill_times(current_bar_time: pd.Timestamp, fill_steps: int) -> list[pd.Timestamp]:
    """
    Build synthetic timestamps ending at current_bar_time.
    This keeps only the most recent synthetic window when a large gap is truncated.
    """
    steps = int(max(0, fill_steps))
    if steps <= 0:
        return []
    start = current_bar_time - pd.Timedelta(hours=steps - 1)
    return [start + pd.Timedelta(hours=i) for i in range(steps)]


def _synthetic_seed_close(
    *,
    prev_close: float,
    live_mid: float,
    recovery_source: str,
    gap_fill_truncated: bool,
) -> float:
    """
    For truncated synthetic recovery, anchor to live mid to avoid stale-price shock bars.
    """
    if str(recovery_source) == "synthetic_capped" and bool(gap_fill_truncated):
        try:
            mid = float(live_mid)
            if pd.notna(mid):
                return mid
        except Exception:
            pass
    return float(prev_close)


def _startup_warmup_strategy(raw: str) -> str:
    mode = str(raw or "live").strip().lower()
    if mode not in {"live", "backward_bridge"}:
        return "live"
    return mode


def _startup_backfill_state_default() -> dict:
    return {
        "pending": False,
        "ready": False,
        "ready_processed": False,
        "bridge_bars": 0,
        "first_pending_ts": 0.0,
        "last_retry_ts": 0.0,
        "last_alert_ts": 0.0,
        "gap_hours_original": 0,
    }


def _startup_backfill_retry_age_secs(state: dict, now_ts: float) -> float:
    if not bool(state.get("pending", False)):
        return 0.0
    first_pending_ts = float(state.get("first_pending_ts", 0.0) or 0.0)
    if first_pending_ts <= 0.0:
        return 0.0
    return float(max(float(now_ts) - first_pending_ts, 0.0))


def _startup_backfill_retry_due(state: dict, now_ts: float, retry_secs: float) -> bool:
    if not bool(state.get("pending", False)):
        return False
    retry_gap = float(max(1.0, retry_secs))
    last_retry_ts = float(state.get("last_retry_ts", 0.0) or 0.0)
    return bool((float(now_ts) - last_retry_ts) >= retry_gap)


def _startup_backfill_mark_pending(
    *,
    state: dict,
    now_ts: float,
    gap_hours: int,
    bridge_bars: int,
    attempted_retry: bool = False,
) -> None:
    if not bool(state.get("pending", False)):
        state["first_pending_ts"] = float(now_ts)
        state["last_alert_ts"] = 0.0
    state["pending"] = True
    state["ready"] = False
    state["ready_processed"] = False
    state["bridge_bars"] = int(max(0, bridge_bars))
    state["gap_hours_original"] = int(max(0, gap_hours))
    if attempted_retry:
        state["last_retry_ts"] = float(now_ts)


def _startup_backfill_mark_ready(
    *,
    state: dict,
    now_ts: float,
    gap_hours: int,
    bridge_bars: int,
) -> None:
    state["pending"] = False
    state["ready"] = True
    state["ready_processed"] = False
    state["bridge_bars"] = int(max(0, bridge_bars))
    state["gap_hours_original"] = int(max(0, gap_hours))
    state["last_retry_ts"] = float(now_ts)


def apply_active_symbol_filter(catalog: list[str], cfg: dict) -> list[str]:
    active_raw = cfg.get("active_symbols", []) or []
    active = [str(s).strip().upper() for s in active_raw if str(s).strip()]
    if not active:
        return catalog

    allow = set(active)
    filtered = [s for s in catalog if str(s).upper() in allow]
    present = {str(s).upper() for s in filtered}
    missing = sorted(allow - present)

    if missing:
        logger.warning(
            "active_symbols contains %d symbol(s) not in discovered catalog: %s",
            len(missing),
            ", ".join(missing[:10]) + ("..." if len(missing) > 10 else ""),
        )
    if not filtered:
        logger.error("active_symbols filter matched 0 symbols; using full catalog instead")
        return catalog

    logger.info(
        "Active symbol filter enabled: %d/%d symbols",
        len(filtered),
        len(catalog),
    )
    return filtered

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
    
    row_cap = MAX_BARS
    try:
        lookback_int = int(lookback)
        if lookback_int > 0:
            row_cap = min(MAX_BARS, lookback_int)
    except Exception:
        row_cap = MAX_BARS

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
            
            # Respect configured lookback while preserving hard memory bound.
            md[s] = df.iloc[-row_cap:].copy() if len(df) > row_cap else df.copy()
            
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
    interop_audit_enabled = bool(cfg.get("interop_audit_enabled", False))
    interop_compute_trace_path = str(
        cfg.get("interop_compute_trace_path", "data/state/audit/interop/compute_trace.jsonl")
    ).strip()
    interop_audit_sample_rate = float(max(0.0, min(1.0, float(cfg.get("interop_audit_sample_rate", 1.0)))))
    interop_audit_mode = _interop_mode(cfg.get("interop_audit_mode", "live_shadow"))
    
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
    catalog_all = build_catalog(data_dir, cfg["symbols_roots"], mini_suffixes)
    catalog = apply_active_symbol_filter(catalog_all, cfg)

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
    try:
        from execution.mt4_bridge_client import check_connection_details
    except ImportError:  # Package mode: import via src.*
        from src.execution.mt4_bridge_client import check_connection_details
    print("Checking connection to MT4 Bridge Server...", end="", flush=True)
    ok, reason = check_connection_details()
    if not ok:
        print(" FAILED!")
        logger.error("CRITICAL: Bridge Server is NOT running at http://127.0.0.1:58710")
        if reason:
            logger.error("Bridge health detail: %s", str(reason))
        logger.error("Please start 'start.bat' or 'run_bridge.bat' FIRST.")
        # We exit to prevent blind running
        return
    print(" OK.")

    # Load historical data ONCE at startup
    logger.info("Loading historical market data...")
    md = fetch_market_data(data_dir, catalog, cfg["lookback_bars"])
    logger.info(f"Loaded market data for {len(md)} symbols")

    gap_recovery_enabled = bool(cfg.get("bridge_gap_recovery", False))
    gap_recovery_hours = int(max(1, cfg.get("bridge_gap_recovery_hours", 6)))
    gap_recovery_max_synth_bars = int(max(1, cfg.get("bridge_gap_recovery_max_synth_bars", 3)))
    gap_recovery_min_bridge_bars = int(max(1, cfg.get("bridge_gap_recovery_min_bridge_bars", 48)))
    first_bar_jump_cap_bps = float(max(1.0, cfg.get("bridge_first_bar_jump_cap_bps", 250.0)))
    startup_rehydrate_from_bridge = bool(cfg.get("startup_rehydrate_from_bridge", True))
    startup_rehydrate_limit_bars = int(max(32, cfg.get("startup_rehydrate_limit_bars", cfg.get("lookback_bars", 400))))
    startup_major_gap_hours = int(max(1, cfg.get("startup_major_gap_hours", 24)))
    startup_warmup_strategy = _startup_warmup_strategy(cfg.get("startup_warmup_strategy", "live"))
    startup_backward_replay_bars = int(max(24, cfg.get("startup_backward_replay_bars", 96)))
    startup_backfill_retry_secs = float(max(1.0, cfg.get("startup_backfill_retry_secs", 10.0)))
    startup_backfill_alert_after_secs = float(max(10.0, cfg.get("startup_backfill_alert_after_secs", 300.0)))
    startup_backfill_block_entries = bool(cfg.get("startup_backfill_block_entries", True))
    live_bars_since_startup: dict[str, int] = {str(s): 0 for s in list(catalog)}
    startup_backfill_state: dict[str, dict] = {
        str(s): _startup_backfill_state_default() for s in list(catalog)
    }

    iteration = 0
    gap_recovery_events = 0
    while True:
        cycle_t0 = time.perf_counter()
        iteration += 1
        logger.info(f"\n{'='*60}")
        logger.info(f"ITERATION {iteration} - {time.strftime('%Y-%m-%d %H:%M:%S')}")
        logger.info(f"{'='*60}")
        
        # md is now persistent across iterations
        
        # --- LIVE DATA PATCH ---
        # --- LIVE DATA PATCH ---
        try:
            from execution.mt4_bridge_client import get_ticks, get_account_info, get_recent_bars
        except ImportError:  # Package mode: import via src.*
            from src.execution.mt4_bridge_client import get_ticks, get_account_info, get_recent_bars
        
        # 1. Update Equity from Bridge
        acct_t0 = time.perf_counter()
        acct = get_account_info()
        acct_ms = float((time.perf_counter() - acct_t0) * 1000.0)
        if "equity" in acct:
            current_equity = float(acct["equity"])
            # User request: "take the margin". We log it for now as agent uses equity for sizing.
            margin = float(acct.get("margin", 0))
            free = float(acct.get("freemargin", 0))
            logger.info(f"ACCOUNT: Equity={current_equity:.2f}, Margin={margin:.2f}, Free={free:.2f}")
        else:
            current_equity = args.equity

        ticks_t0 = time.perf_counter()
        ticks = get_ticks()
        ticks_ms = float((time.perf_counter() - ticks_t0) * 1000.0)
        for sym, data in ticks.items():
            if sym in md:
                try:
                    bid = data["bid"]
                    ask = data["ask"]
                    mid = (bid + ask) / 2.0
                    
                    # 2. RAM DATABASE / BAR BUILDING
                    # Use UTC-naive H1 bars consistently.
                    last_time = md[sym].index[-1]
                    if getattr(last_time, "tzinfo", None) is not None:
                        last_time = last_time.tz_convert("UTC").tz_localize(None)
                    current_bar_time = pd.Timestamp.now(tz="UTC").floor("h").tz_localize(None)

                    # Gap-aware bar extension with bridge-history-first recovery.
                    gap_hours = int(max(0, (current_bar_time - last_time).total_seconds() // 3600))
                    now_ts = float(time.time())
                    recovered_gap = False
                    recovered_gap_source = "none"
                    gap_fill_truncated = False
                    bridge_df = pd.DataFrame()
                    major_gap_recovered = False
                    backfill_state_sym = startup_backfill_state.setdefault(
                        str(sym), _startup_backfill_state_default()
                    )
                    if gap_hours > 0:
                        rehydrated = False
                        bridge_fetch_on_gap = bool(
                            gap_hours >= gap_recovery_hours
                            and (
                                startup_rehydrate_from_bridge
                                or startup_warmup_strategy == "backward_bridge"
                            )
                        )
                        if bridge_fetch_on_gap:
                            bars = get_recent_bars(
                                sym,
                                timeframe="H1",
                                limit=startup_rehydrate_limit_bars,
                                max_retries=1,
                            )
                            bridge_df = _bridge_bars_to_df(bars)
                            bridge_bar_count = int(len(bridge_df))
                            if bridge_bar_count >= gap_recovery_min_bridge_bars:
                                saved_attrs = md[sym].attrs.copy()
                                md[sym] = bridge_df.iloc[-MAX_BARS:].copy()
                                md[sym].attrs = saved_attrs
                                recovered_gap = True
                                recovered_gap_source = "bridge_history"
                                rehydrated = True
                                if gap_hours >= gap_recovery_hours:
                                    gap_recovery_events += 1
                                logger.info(
                                    "%s: BAR GAP %dh -> recovered from bridge history (%d bars)",
                                    sym,
                                    gap_hours,
                                    len(md[sym]),
                                )
                            elif (
                                startup_warmup_strategy == "backward_bridge"
                                and gap_hours >= startup_major_gap_hours
                                and startup_backfill_block_entries
                            ):
                                pending_was = bool(backfill_state_sym.get("pending", False))
                                _startup_backfill_mark_pending(
                                    state=backfill_state_sym,
                                    now_ts=now_ts,
                                    gap_hours=gap_hours,
                                    bridge_bars=bridge_bar_count,
                                    attempted_retry=True,
                                )
                                if not pending_was:
                                    logger.warning(
                                        "%s: startup backfill pending (%d/%d bridge bars)",
                                        sym,
                                        bridge_bar_count,
                                        gap_recovery_min_bridge_bars,
                                    )
                                    if interop_audit_enabled:
                                        _emit_compute_trace(
                                            interop_compute_trace_path,
                                            {
                                                "ts": float(now_ts),
                                                "phase": "startup_backfill",
                                                "event": "pending_start",
                                                "symbol": str(sym),
                                                "warmup_strategy": str(startup_warmup_strategy),
                                                "startup_backfill_pending": True,
                                                "startup_backfill_ready": False,
                                                "startup_backfill_bars": int(bridge_bar_count),
                                                "startup_backfill_retry_age_secs": 0.0,
                                                "startup_backward_replay_done": False,
                                                "gap_hours_original": int(gap_hours),
                                            },
                                            sample_rate=interop_audit_sample_rate,
                                        )
                        if not rehydrated:
                            fill_steps, gap_fill_truncated = _resolve_synthetic_fill_steps(
                                gap_hours=gap_hours,
                                gap_recovery_enabled=gap_recovery_enabled,
                                max_synth_bars=gap_recovery_max_synth_bars,
                            )

                            if fill_steps > 1:
                                recovered_gap = True
                                recovered_gap_source = "synthetic_capped"
                                if gap_hours >= gap_recovery_hours:
                                    gap_recovery_events += 1
                                logger.info(
                                    "%s: BAR GAP %dh -> recovering %d synthetic bar(s)%s",
                                    sym,
                                    gap_hours,
                                    fill_steps,
                                    " [truncated]" if gap_fill_truncated else "",
                                )
                            elif gap_fill_truncated:
                                recovered_gap_source = "synthetic_capped"
                                logger.info(
                                    "%s: BAR GAP %dh -> synthetic fill truncated to %d bar(s)",
                                    sym,
                                    gap_hours,
                                    fill_steps,
                                )
                            else:
                                logger.info(f"{sym}: NEW BAR {current_bar_time} (Prev: {last_time})")
                                live_bars_since_startup[sym] = int(live_bars_since_startup.get(sym, 0)) + 1

                            saved_attrs = md[sym].attrs.copy()
                            prev_close = float(md[sym].iloc[-1]["close"])
                            seed_close = _synthetic_seed_close(
                                prev_close=prev_close,
                                live_mid=float(mid),
                                recovery_source=recovered_gap_source,
                                gap_fill_truncated=gap_fill_truncated,
                            )
                            new_times = _synthetic_fill_times(current_bar_time, fill_steps)
                            new_rows = pd.DataFrame(
                                {
                                    "open": [seed_close] * len(new_times),
                                    "high": [seed_close] * len(new_times),
                                    "low": [seed_close] * len(new_times),
                                    "close": [seed_close] * len(new_times),
                                    "volume": [0] * len(new_times),
                                },
                                index=new_times,
                            )
                            md[sym] = pd.concat([md[sym], new_rows]).iloc[-MAX_BARS:]
                            md[sym].attrs = saved_attrs

                        if startup_warmup_strategy == "backward_bridge" and gap_hours >= startup_major_gap_hours:
                            if rehydrated and int(len(md[sym])) >= gap_recovery_min_bridge_bars:
                                _startup_backfill_mark_ready(
                                    state=backfill_state_sym,
                                    now_ts=now_ts,
                                    gap_hours=gap_hours,
                                    bridge_bars=int(len(md[sym])),
                                )
                                if interop_audit_enabled:
                                    _emit_compute_trace(
                                        interop_compute_trace_path,
                                        {
                                            "ts": float(now_ts),
                                            "phase": "startup_backfill",
                                            "event": "ready",
                                            "symbol": str(sym),
                                            "warmup_strategy": str(startup_warmup_strategy),
                                            "startup_backfill_pending": False,
                                            "startup_backfill_ready": True,
                                            "startup_backfill_bars": int(len(md[sym])),
                                            "startup_backfill_retry_age_secs": 0.0,
                                            "startup_backward_replay_done": False,
                                            "gap_hours_original": int(gap_hours),
                                        },
                                        sample_rate=interop_audit_sample_rate,
                                    )
                            elif startup_backfill_block_entries:
                                pending_was = bool(backfill_state_sym.get("pending", False))
                                _startup_backfill_mark_pending(
                                    state=backfill_state_sym,
                                    now_ts=now_ts,
                                    gap_hours=gap_hours,
                                    bridge_bars=int(len(bridge_df)),
                                    attempted_retry=bridge_fetch_on_gap,
                                )
                                if not pending_was:
                                    logger.warning(
                                        "%s: startup backfill pending (%d/%d bridge bars)",
                                        sym,
                                        int(backfill_state_sym.get("bridge_bars", 0)),
                                        gap_recovery_min_bridge_bars,
                                    )
                                    if interop_audit_enabled:
                                        _emit_compute_trace(
                                            interop_compute_trace_path,
                                            {
                                                "ts": float(now_ts),
                                                "phase": "startup_backfill",
                                                "event": "pending_start",
                                                "symbol": str(sym),
                                                "warmup_strategy": str(startup_warmup_strategy),
                                                "startup_backfill_pending": True,
                                                "startup_backfill_ready": False,
                                                "startup_backfill_bars": int(backfill_state_sym.get("bridge_bars", 0)),
                                                "startup_backfill_retry_age_secs": 0.0,
                                                "startup_backward_replay_done": False,
                                                "gap_hours_original": int(gap_hours),
                                            },
                                            sample_rate=interop_audit_sample_rate,
                                        )
                        md[sym].attrs["gap_recovered"] = bool(recovered_gap)
                        md[sym].attrs["gap_recovery_source"] = str(recovered_gap_source)
                        md[sym].attrs["gap_fill_truncated"] = bool(gap_fill_truncated)
                        md[sym].attrs["gap_hours_original"] = int(gap_hours)
                    else:
                        md[sym].attrs["gap_recovered"] = False
                        md[sym].attrs["gap_recovery_source"] = "none"
                        md[sym].attrs["gap_fill_truncated"] = False
                        md[sym].attrs["gap_hours_original"] = 0

                    if (
                        startup_warmup_strategy == "backward_bridge"
                        and bool(backfill_state_sym.get("pending", False))
                        and startup_backfill_block_entries
                    ):
                        retry_due = _startup_backfill_retry_due(
                            backfill_state_sym,
                            now_ts,
                            startup_backfill_retry_secs,
                        )
                        if retry_due:
                            bars_retry = get_recent_bars(
                                sym,
                                timeframe="H1",
                                limit=startup_rehydrate_limit_bars,
                                max_retries=1,
                            )
                            backfill_state_sym["last_retry_ts"] = float(now_ts)
                            bridge_df_retry = _bridge_bars_to_df(bars_retry)
                            bridge_retry_count = int(len(bridge_df_retry))
                            if bridge_retry_count >= gap_recovery_min_bridge_bars:
                                saved_attrs = md[sym].attrs.copy()
                                md[sym] = bridge_df_retry.iloc[-MAX_BARS:].copy()
                                md[sym].attrs = saved_attrs
                                recovered_gap = True
                                recovered_gap_source = "bridge_history"
                                pending_age = _startup_backfill_retry_age_secs(backfill_state_sym, now_ts)
                                _startup_backfill_mark_ready(
                                    state=backfill_state_sym,
                                    now_ts=now_ts,
                                    gap_hours=int(backfill_state_sym.get("gap_hours_original", gap_hours)),
                                    bridge_bars=bridge_retry_count,
                                )
                                logger.info(
                                    "%s: startup backfill ready after %.1fs (%d bars)",
                                    sym,
                                    pending_age,
                                    bridge_retry_count,
                                )
                                if interop_audit_enabled:
                                    _emit_compute_trace(
                                        interop_compute_trace_path,
                                        {
                                            "ts": float(now_ts),
                                            "phase": "startup_backfill",
                                            "event": "ready",
                                            "symbol": str(sym),
                                            "warmup_strategy": str(startup_warmup_strategy),
                                            "startup_backfill_pending": False,
                                            "startup_backfill_ready": True,
                                            "startup_backfill_bars": int(bridge_retry_count),
                                            "startup_backfill_retry_age_secs": 0.0,
                                            "startup_backward_replay_done": False,
                                            "gap_hours_original": int(backfill_state_sym.get("gap_hours_original", gap_hours)),
                                        },
                                        sample_rate=interop_audit_sample_rate,
                                    )
                            else:
                                backfill_state_sym["bridge_bars"] = bridge_retry_count
                                pending_age = _startup_backfill_retry_age_secs(backfill_state_sym, now_ts)
                                logger.info(
                                    "%s: startup backfill retry (%d/%d bridge bars, age %.1fs)",
                                    sym,
                                    bridge_retry_count,
                                    gap_recovery_min_bridge_bars,
                                    pending_age,
                                )
                                if interop_audit_enabled:
                                    _emit_compute_trace(
                                        interop_compute_trace_path,
                                        {
                                            "ts": float(now_ts),
                                            "phase": "startup_backfill",
                                            "event": "retry",
                                            "symbol": str(sym),
                                            "warmup_strategy": str(startup_warmup_strategy),
                                            "startup_backfill_pending": True,
                                            "startup_backfill_ready": False,
                                            "startup_backfill_bars": int(bridge_retry_count),
                                            "startup_backfill_retry_age_secs": float(pending_age),
                                            "startup_backward_replay_done": False,
                                            "gap_hours_original": int(backfill_state_sym.get("gap_hours_original", gap_hours)),
                                        },
                                        sample_rate=interop_audit_sample_rate,
                                    )

                        pending_age = _startup_backfill_retry_age_secs(backfill_state_sym, now_ts)
                        last_alert_ts = float(backfill_state_sym.get("last_alert_ts", 0.0))
                        if (
                            pending_age >= startup_backfill_alert_after_secs
                            and (last_alert_ts <= 0.0 or (now_ts - last_alert_ts) >= startup_backfill_alert_after_secs)
                        ):
                            backfill_state_sym["last_alert_ts"] = float(now_ts)
                            logger.warning(
                                "%s: startup backfill pending for %.0fs (%d/%d bridge bars); entries remain blocked",
                                sym,
                                pending_age,
                                int(backfill_state_sym.get("bridge_bars", 0)),
                                gap_recovery_min_bridge_bars,
                            )
                            if interop_audit_enabled:
                                _emit_compute_trace(
                                    interop_compute_trace_path,
                                    {
                                        "ts": float(now_ts),
                                        "phase": "startup_backfill",
                                        "event": "pending_alert",
                                        "symbol": str(sym),
                                        "warmup_strategy": str(startup_warmup_strategy),
                                        "startup_backfill_pending": True,
                                        "startup_backfill_ready": False,
                                        "startup_backfill_bars": int(backfill_state_sym.get("bridge_bars", 0)),
                                        "startup_backfill_retry_age_secs": float(pending_age),
                                        "startup_backward_replay_done": False,
                                        "gap_hours_original": int(backfill_state_sym.get("gap_hours_original", gap_hours)),
                                    },
                                    sample_rate=interop_audit_sample_rate,
                                )

                    # 3. Update current bar (live patch)
                    # First-bar outlier guard after long recovery to avoid startup vol shocks.
                    if recovered_gap and not (
                        str(recovered_gap_source) == "synthetic_capped" and bool(gap_fill_truncated)
                    ):
                        ref_close = float(md[sym].iloc[-1]["close"])
                        cap_abs = abs(ref_close) * (first_bar_jump_cap_bps / 10000.0)
                        max_up = ref_close + cap_abs
                        max_dn = ref_close - cap_abs
                        if mid > max_up:
                            logger.warning(
                                "%s: clipping first recovered tick jump %.5f -> %.5f",
                                sym,
                                mid,
                                max_up,
                            )
                            mid = max_up
                        elif mid < max_dn:
                            logger.warning(
                                "%s: clipping first recovered tick jump %.5f -> %.5f",
                                sym,
                                mid,
                                max_dn,
                            )
                            mid = max_dn

                    if startup_warmup_strategy == "live":
                        major_gap_recovered = bool(recovered_gap and gap_hours >= startup_major_gap_hours)
                    else:
                        major_gap_recovered = bool(backfill_state_sym.get("ready", False))

                    if startup_warmup_strategy == "live" and major_gap_recovered:
                        live_bars_since_startup[sym] = 0
                        # Reset model internals after stale-gap recovery and activate warmup guard.
                        if hasattr(agent, "reset_symbol_model_state"):
                            try:
                                agent.reset_symbol_model_state(sym, reason=f"major_gap_{gap_hours}h")
                            except Exception as exc:
                                logger.debug("%s: reset_symbol_model_state failed (%s)", sym, exc)
                        if hasattr(agent, "activate_startup_warmup"):
                            try:
                                agent.activate_startup_warmup(sym, gap_hours=gap_hours)
                            except Exception as exc:
                                logger.debug("%s: activate_startup_warmup failed (%s)", sym, exc)
                    elif (
                        startup_warmup_strategy == "backward_bridge"
                        and bool(backfill_state_sym.get("ready", False))
                        and (not bool(backfill_state_sym.get("ready_processed", False)))
                    ):
                        live_bars_since_startup[sym] = 0
                        gap_reason_hours = int(backfill_state_sym.get("gap_hours_original", gap_hours))
                        if hasattr(agent, "reset_symbol_model_state"):
                            try:
                                agent.reset_symbol_model_state(sym, reason=f"major_gap_{gap_reason_hours}h")
                            except Exception as exc:
                                logger.debug("%s: reset_symbol_model_state failed (%s)", sym, exc)
                        if hasattr(agent, "activate_startup_backward_warmup"):
                            try:
                                agent.activate_startup_backward_warmup(
                                    sym,
                                    gap_hours=gap_reason_hours,
                                    backfill_bars=int(backfill_state_sym.get("bridge_bars", len(md[sym]))),
                                    replay_bars=int(startup_backward_replay_bars),
                                )
                            except TypeError:
                                agent.activate_startup_backward_warmup(
                                    sym,
                                    gap_hours=gap_reason_hours,
                                    backfill_bars=int(backfill_state_sym.get("bridge_bars", len(md[sym]))),
                                )
                            except Exception as exc:
                                logger.debug("%s: activate_startup_backward_warmup failed (%s)", sym, exc)
                        backfill_state_sym["ready_processed"] = True

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
                    # Store live tick timestamp for strict stale-tick gating.
                    tick_ts = time.time()
                    raw_tick_time = data.get("time")
                    if raw_tick_time:
                        try:
                            tick_ts = pd.Timestamp(raw_tick_time).timestamp()
                        except Exception:
                            tick_ts = time.time()
                    md[sym].attrs["last_tick_ts"] = float(tick_ts)
                    md[sym].attrs["bar_integrity_ok"] = True
                    md[sym].attrs["gap_recovery_events"] = int(gap_recovery_events)
                    md[sym].attrs["live_bars_since_startup"] = int(live_bars_since_startup.get(sym, 0))
                    md[sym].attrs["major_gap_recovered"] = bool(major_gap_recovered)
                    md[sym].attrs["warmup_strategy"] = str(startup_warmup_strategy)
                    if startup_warmup_strategy == "backward_bridge":
                        md[sym].attrs["startup_backfill_pending"] = bool(backfill_state_sym.get("pending", False))
                        md[sym].attrs["startup_backfill_ready"] = bool(backfill_state_sym.get("ready", False))
                        md[sym].attrs["startup_backfill_bars"] = int(backfill_state_sym.get("bridge_bars", 0))
                        md[sym].attrs["startup_backfill_retry_age_secs"] = float(
                            _startup_backfill_retry_age_secs(backfill_state_sym, now_ts)
                        )
                        md[sym].attrs["startup_backfill_block_entries"] = bool(startup_backfill_block_entries)
                        md[sym].attrs["startup_backward_replay_done"] = bool(
                            md[sym].attrs.get("startup_backward_replay_done", False)
                        )
                    else:
                        md[sym].attrs["startup_backfill_pending"] = False
                        md[sym].attrs["startup_backfill_ready"] = True
                        md[sym].attrs["startup_backfill_bars"] = 0
                        md[sym].attrs["startup_backfill_retry_age_secs"] = 0.0
                        md[sym].attrs["startup_backfill_block_entries"] = False
                        md[sym].attrs["startup_backward_replay_done"] = False
                        
                except Exception as e:
                    try:
                        md[sym].attrs["bar_integrity_ok"] = False
                    except Exception:
                        pass
                    logger.warning(f"Failed to patch {sym}: {e}")
        # -----------------------
        
        logger.info(f"Loaded market data for {len(md)} symbols")
        
        act_t0 = time.perf_counter()
        agent.act(current_equity, md, all_symbols_catalog=catalog)
        act_ms = float((time.perf_counter() - act_t0) * 1000.0)
        
        # F. Log rejection statistics
        if iteration % 10 == 0 and agent.rejection_stats:
            logger.info("\nREJECTION STATS (last 10 iterations):")
            for reason, count in sorted(agent.rejection_stats.items(), key=lambda x: -x[1]):
                logger.info(f"  {reason}: {count}")

        cycle_ms = float((time.perf_counter() - cycle_t0) * 1000.0)
        if interop_audit_enabled:
            dec_timing = dict(getattr(agent, "_interop_last_decisions_timing", {}) or {})
            _emit_compute_trace(
                interop_compute_trace_path,
                {
                    "ts": float(time.time()),
                    "phase": "agent_cycle",
                    "mode": str(interop_audit_mode),
                    "cycle_id": int(iteration),
                    "symbol_count": int(len(md)),
                    "tick_count": int(len(ticks)),
                    "decision_count": int(dec_timing.get("decision_count", 0)),
                    "candidate_count": int(dec_timing.get("candidate_count", 0)),
                    "score_symbol_calls": int(dec_timing.get("score_symbol_calls", 0)),
                    "score_symbol_ms_total": float(dec_timing.get("score_symbol_ms_total", 0.0)),
                    "score_symbol_ms_mean": float(dec_timing.get("score_symbol_ms_mean", 0.0)),
                    "agent_cycle_ms": float(cycle_ms),
                    "agent_act_ms": float(act_ms),
                    "bridge_get_ticks_ms": float(ticks_ms),
                    "bridge_get_account_ms": float(acct_ms),
                    "sleep_interval_secs": float(args.sleep),
                    "rejection_stats_cycle": dict(getattr(agent, "rejection_stats_cycle", {}) or {}),
                },
                sample_rate=interop_audit_sample_rate,
            )
        
        time.sleep(args.sleep)

if __name__ == "__main__":
    main()
