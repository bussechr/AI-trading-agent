from __future__ import annotations
import os, json, math
from dataclasses import dataclass
import numpy as np
import pandas as pd

from .risk_utils import el_pz, regime_tilt, dynamic_target_pct, realised_vol, cost_gate, low_corr_pick
from ..execution.mt4_bridge_client import send
import requests

MINI_SUFFIXES_DEFAULT = [".MINI", "-MINI", "m", ".m"]

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
    def score_symbol(self, df: pd.DataFrame) -> float:
        """
        Our EL-momentum + regime proxy score at the *H1* chart horizon.
        score_t = pz_t * (2*tilt_t - 1), where:
          pz_t   = EMA of z-scored log-return (EL generalised momentum, display variant)
          tilt_t = regime tilt proxy in [-1, 1] (trend probability proxy)
        """
        close = df["close"]
        r = np.log(close).diff()
        pz = el_pz(close, self.cfg["el_window"], self.cfg["el_ema_span"])
        tilt = regime_tilt(r)
        s = float((pz * tilt).iloc[-1])  # variant: multiply by (2P-1); here tilt∈[-1,1]
        return s if math.isfinite(s) else 0.0

    # ---------- Decide and act ----------
    def decisions(self, md: dict[str, pd.DataFrame]) -> list[Decision]:
        raw: list[Decision] = []
        for sym, df in md.items():
            if df is None or df.empty or len(df) < max(64, self.cfg["el_window"]+5): continue
            sc = self.score_symbol(df)
            if abs(sc) < self.score_th: continue
            raw.append(Decision(sym=sym, side=("BUY" if sc>0 else "SELL"), score=abs(sc)))

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
            if not cost_gate(expected_move, self.avg_spread_pips, self.pip_value_per_lot, equity, lot_fraction):
                continue
            # Send with lots=0.0 so EA enforces minimum (0.10 for IG minis)
            send(d.side, d.symbol, lots=0.0, tp_cash=equity * target_pct)
    
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
                json={"decisions": decisions_data, "vol": float(vol_now)},
                timeout=1
            )
        except Exception:
            pass  # Don't fail trading on dashboard errors
