from __future__ import annotations
import os, json, math
from dataclasses import dataclass
import numpy as np
import pandas as pd
import logging

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from .heston_service import HestonService  # optional
from ..marketdata.http_fx_options import HTTPFXOptionProvider  # optional
from ..marketdata.proxy_provider import ProxyOptionProvider  # optional
from ..execution.mt4_bridge_client import send
import requests

MINI_SUFFIXES_DEFAULT = [".MINI", "-MINI", "m", ".m"]

logger = logging.getLogger(__name__)

@dataclass
class Decision:
    symbol: str
    side: str
    score: float

class FXELAgent:
    """
    EL momentum + regime tilt + cost/correlation gates.
    Sends min-lot orders to EA with TP in *cash* (~1% of equity; EA computes price TP).
    *Minis only* — universe is filtered to symbols with mini suffixes.
    """

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mini_suffixes = cfg.get("mini_suffixes", MINI_SUFFIXES_DEFAULT)
        self.roots = cfg["symbols_roots"]
        self.maxK = int(cfg.get("max_concurrent", 4))
        self.corr_max = float(cfg.get("corr_max", 0.70))
        self.score_th = float(cfg.get("score_threshold", 0.40))
        self.use_dyn_target = bool(cfg.get("use_dynamic_target", True))
        self.vol_ref = float(cfg.get("vol_ref", 0.010))
        self.target_base = float(cfg.get("target_base_pct", 0.010))
        self.avg_spread_pips = float(cfg.get("avg_spread_pips", 0.8))
        self.pip_value_per_lot = float(cfg.get("pip_value_per_lot", 10.0))
        # IG mini lot size (0.10 for IG, vs 1.0 standard or 0.01 micro)
        self.ig_mini_lot = float(cfg.get("ig_mini_lot_size", 0.10))
        # Data directory for proxy provider / CSVs
        self.data_dir = cfg.get("data_dir", "data/fx_minis")
        # Optional Heston/options surface service
        self.heston: HestonService | None = None
        self._init_options_provider(cfg)
        
        # Runtime validation tracking
        self.rejection_stats = {}
        self.decision_log = []

    # ---------- Options provider wiring (optional) ----------
    def _init_options_provider(self, cfg: dict) -> None:
        opts = cfg.get("options", {}) or {}
        if not bool(opts.get("enable", False)):
            return
        provider_name = str(opts.get("provider", "http")).lower()
        recalc_after_secs = int(opts.get("recalc_after_secs", 18 * 3600))
        outdir = str(opts.get("outdir", "data/heston"))

        try:
            provider = None
            if provider_name == "http":
                http_cfg = opts.get("http", {}) or {}
                url_template = http_cfg.get("url_template")
                headers = http_cfg.get("headers", {})
                field_map = http_cfg.get("field_map", None)
                if not url_template:
                    raise ValueError("options.http.url_template missing")
                provider = HTTPFXOptionProvider(url_template=url_template, headers=headers, field_map=field_map)
            elif provider_name == "proxy":
                proxy_cfg = opts.get("proxy", {}) or {}
                rd = float(proxy_cfg.get("rd", 0.0))
                rf = float(proxy_cfg.get("rf", 0.0))

                def get_close_series(root: str):
                    import pandas as pd, os
                    # try exact filename
                    p1 = os.path.join(self.data_dir, f"{root}.csv")
                    if os.path.exists(p1):
                        return pd.read_csv(p1)["close"]
                    # fallback: search for any CSV containing the root (MINI or other)
                    for fn in os.listdir(self.data_dir):
                        if fn.lower().endswith(".csv") and root.upper() in fn.upper():
                            df = pd.read_csv(os.path.join(self.data_dir, fn))
                            if "close" in df.columns:
                                return df["close"]
                    raise FileNotFoundError(f"No CSV found for root {root} in {self.data_dir}")

                def get_spot(root: str) -> float:
                    import pandas as pd
                    s = get_close_series(root)
                    return float(pd.Series(s).iloc[-1])

                provider = ProxyOptionProvider(get_close=get_close_series, get_s0=get_spot, rd=rd, rf=rf)
            else:
                raise ValueError(f"Unknown options provider: {provider_name}")

            if provider is not None:
                self.heston = HestonService(outdir=outdir, provider=provider, recalc_after_secs=recalc_after_secs)
        except Exception as e:
            logger.warning(f"Options provider init failed: {e}")

    def _symbol_root(self, symbol: str) -> str:
        s_up = symbol.upper()
        for r in self.roots:
            if r in s_up:
                return r
        return symbol

    # ---------- Universe (minis only) ----------
    def build_universe(self, all_symbols: list[str]) -> list[str]:
        """
        Keep only minis: names that end with or contain a configured mini suffix.
        Also require that the name contains one of the FX roots (EURUSD, etc.).
        
        For IG: no suffixes (empty mini_suffixes list), so we just filter by roots.
        All IG FX symbols are minis when traded at 0.10 lot size.
        """
        minis: list[str] = []
        
        # IG mode: no suffixes, all root symbols are valid (traded as minis via lot size)
        if not self.mini_suffixes:
            for s in all_symbols:
                s_up = s.upper()
                # Match any symbol that contains one of our roots
                if any(r in s_up for r in self.roots):
                    minis.append(s)
        else:
            # Generic mode: filter by suffixes
            lowers = [s.lower() for s in self.mini_suffixes]
            for s in all_symbols:
                s_up = s.upper()
                if not any(r in s_up for r in self.roots):  # must be a major root
                    continue
                s_low = s.lower()
                if any(s_low.endswith(suf.lower()) or (suf.lower() in s_low) for suf in lowers):
                    minis.append(s)
        
        return sorted(list(dict.fromkeys(minis)))  # unique, stable order

    # ---------- Core scoring ----------
    def score_symbol(self, df: pd.DataFrame, symbol: str) -> tuple[float, dict]:
        """
        Our EL-momentum + regime proxy score at the *H1* chart horizon.
        score_t = pz_t * (2*tilt_t - 1), where:
          pz_t   = EMA of z-scored log-return (EL generalised momentum, display variant)
          tilt_t = regime tilt proxy in [-1, 1] (trend probability proxy)
        
        Returns: (score, diagnostics_dict)
        """
        close = df["close"]
        r = np.log(close).diff()
        
        # Check for data quality
        if len(close) < max(64, self.cfg["el_window"] + 5):
            return 0.0, {"error": "insufficient_bars"}
        
        pz_series = el_pz(close, self.cfg["el_window"], self.cfg["el_ema_span"])
        tilt_series = regime_tilt(r)
        
        pz_val = float(pz_series.iloc[-1])
        tilt_val = float(tilt_series.iloc[-1])
        
        # B. Validate EL momentum is well-formed
        if not np.isfinite(pz_val):
            logger.warning(f"{symbol}: pz is NaN/Inf, skipping")
            self._log_rejection("pz_invalid")
            return 0.0, {"error": "pz_invalid", "pz": pz_val}
        
        # B. Validate regime tilt
        if not np.isfinite(tilt_val) or not (-1.0 <= tilt_val <= 1.0):
            logger.warning(f"{symbol}: tilt {tilt_val} invalid, skipping")
            self._log_rejection("tilt_invalid")
            return 0.0, {"error": "tilt_invalid", "tilt": tilt_val}
        
        # B. Compute score
        s = float(pz_val * tilt_val)
        
        if not math.isfinite(s):
            logger.warning(f"{symbol}: score is NaN/Inf, skipping")
            self._log_rejection("score_invalid")
            return 0.0, {"error": "score_invalid"}
        
        # Return diagnostics for logging
        diagnostics = {
            "pz": pz_val,
            "tilt": tilt_val,
            "score": s,
            "vol": float(r.rolling(96).std(ddof=0).iloc[-1]) if len(r) > 96 else 0.0
        }
        
        return s, diagnostics

    # ---------- Decide and act ----------
    def decisions(self, md: dict[str, pd.DataFrame]) -> list[Decision]:
        raw: list[Decision] = []
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(64, self.cfg["el_window"]+5):
                self._log_rejection("insufficient_bars")
                continue
            
            sc, diag = self.score_symbol(df, sym)
            
            # Log decision diagnostics
            self._log_decision(sym, diag)
            
            # C. Score threshold gate
            if abs(sc) < self.score_th:
                logger.debug(f"{sym}: |score|={abs(sc):.3f} < threshold={self.score_th}, rejected")
                self._log_rejection("low_score")
                continue
            
            # B. Verify side matches score sign
            side = "BUY" if sc > 0 else "SELL"
            logger.info(f"{sym}: score={sc:.3f}, pz={diag.get('pz',0):.3f}, tilt={diag.get('tilt',0):.3f} → {side}")
            
            raw.append(Decision(sym=sym, side=side, score=abs(sc)))

        if not raw: return []

        # correlation filter on H1 returns
        rets = {s: np.log(md[s]["close"]).diff().dropna() for s,_ in [(d.symbol, d.score) for d in raw]}
        corr = pd.DataFrame(rets).corr()

        # sort by |score| desc, pick low-corr set
        ranked = sorted([(d.symbol, d.score) for d in raw], key=lambda x: x[1], reverse=True)
        chosen_syms = low_corr_pick(ranked, corr, self.maxK, self.corr_max)

        # map back to full decision objects
        chosen = [d for d in raw if d.symbol in chosen_syms]
        return chosen

    def act(self, equity: float, market_data: dict[str, pd.DataFrame], *,
            all_symbols_catalog: list[str]) -> None:
        """
        Build mini-only universe, compute targets, apply cost gates, and send commands.
        """
        universe = self.build_universe(all_symbols_catalog)
        md = {s: market_data.get(s) for s in universe if s in market_data}

        # realised vol for dynamic target scaling — average across chosen minis
        try:
            vols = [realised_vol(np.log(df["close"]).diff()) for df in md.values() if df is not None]
            vol_now = float(np.nanmean(vols)) if vols else self.vol_ref
        except Exception:
            vol_now = self.vol_ref

        target_pct = dynamic_target_pct(vol_now, self.vol_ref, self.target_base) if self.use_dyn_target else self.target_base

        decs = self.decisions(md)
        
        # Post decisions to bridge for dashboard monitoring
        self._post_decisions_to_dashboard(decs, md, vol_now, target_pct)
        
        if not decs: return

        # expected move proxy from |score| scaled by small constant; adjust if you like
        for d in decs:
            df = md[d.symbol]
            # gate by expected move vs cost
            # For IG: lot_fraction = 0.10 (mini lot size)
            lot_fraction = self.ig_mini_lot if not self.mini_suffixes else 0.01
            expected_move = min(0.006, 0.003 + 0.004 * min(1.0, d.score))  # ~30-60 bps proxy
            
            # C. Cost gate check with logging
            cost_fraction = (self.avg_spread_pips * self.pip_value_per_lot * lot_fraction) / max(equity, 1e-9)
            cost_threshold = 3.0 * cost_fraction
            
            if expected_move <= cost_threshold:
                logger.info(f"{d.symbol}: rejected: cost gate - exp_move={expected_move*100:.3f}% < 3×cost={cost_threshold*100:.3f}%")
                self._log_rejection("cost_gate")
                continue
            
            # Optional: adjust target by options term ratio (mild clamp)
            sym_target_pct = target_pct
            if self.heston is not None:
                try:
                    root = self._symbol_root(d.symbol)
                    scalers = self.heston.get_scalers(root)
                    term_ratio = float(scalers.get("term_ratio", 1.0))
                    adj = max(0.8, min(1.2, term_ratio))
                    sym_target_pct = float(target_pct * adj)
                except Exception:
                    sym_target_pct = target_pct

            logger.info(
                f"{d.symbol}: SIGNAL {d.side} - score={d.score:.3f}, exp_move={expected_move*100:.2f}%, cost={cost_fraction*100:.4f}%, target={sym_target_pct*100:.2f}%"
            )
            
            # Send with lots=0.0 so EA enforces minimum (0.10 for IG minis)
            send(d.side, d.symbol, lots=0.0, tp_cash=equity * sym_target_pct)
    
    def _post_decisions_to_dashboard(self, decisions: list[Decision], 
                                     md: dict[str, pd.DataFrame],
                                     vol_now: float, target_pct: float) -> None:
        """Post current decisions to bridge for dashboard display."""
        try:
            decisions_data = []
            for d in decisions:
                df = md.get(d.symbol)
                if df is not None and not df.empty:
                    close_price = float(df["close"].iloc[-1])
                    decisions_data.append({
                        "symbol": d.symbol,
                        "side": d.side,
                        "score": float(d.score),
                        "price": close_price,
                        "target_pct": float(target_pct)
                    })
            
            requests.post(
                "http://127.0.0.1:5000/state/decisions",
                json={
                    "decisions": decisions_data, 
                    "vol": float(vol_now),
                    "diagnostics": {
                        "rejection_stats": dict(self.rejection_stats),
                        "recent_decisions": self.decision_log[-10:] if self.decision_log else []
                    }
                },
                timeout=1
            )
        except Exception:
            pass  # Don't fail trading on dashboard errors
    
    def _log_rejection(self, reason: str):
        """Track rejection reasons for diagnostics."""
        self.rejection_stats[reason] = self.rejection_stats.get(reason, 0) + 1
    
    def _log_decision(self, symbol: str, diagnostics: dict):
        """Log decision data for audit trail."""
        from datetime import datetime
        self.decision_log.append({
            "time": datetime.now().isoformat(),
            "symbol": symbol,
            **diagnostics
        })
        # Keep last 1000 decisions
        if len(self.decision_log) > 1000:
            self.decision_log.pop(0)
