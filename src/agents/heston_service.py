from __future__ import annotations
import os
import json
import math
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple

from ..marketdata.http_fx_options import Chain
from ..quant.iv import implied_vol_gk


class HestonService:
    """
    Lightweight options surface service with caching.
    - Fetches an FX option chain via a provider (must implement get_chain(symbol_root)->Chain)
    - Computes GK implied vols per quote and simple smile/term summaries
    - Caches JSON results to disk and refreshes at a configured cadence

    Note: This service does NOT fit Heston parameters. It provides stable inputs and
    scalers the agent can consume. Hook in your own Heston calibrator if desired by
    extending _compute_summary to store calibrated params alongside IVs.
    """

    def __init__(self, outdir: str, provider, recalc_after_secs: int = 18 * 3600):
        self.outdir = outdir
        self.provider = provider
        self.recalc_after_secs = int(recalc_after_secs)
        os.makedirs(self.outdir, exist_ok=True)

    # ---------- Public API ----------
    def ensure_latest(self, symbol_root: str) -> Dict[str, Any]:
        path = self._cache_path(symbol_root)
        if self._is_fresh(path):
            try:
                with open(path, "r") as f:
                    return json.load(f)
            except Exception:
                pass

        # Refresh cache
        chain: Chain = self.provider.get_chain(symbol_root)
        result = self._chain_to_json(chain)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(result, f, separators=(",", ":"))
        os.replace(tmp, path)
        return result

    def get_scalers(self, symbol_root: str) -> Dict[str, float]:
        js = self.ensure_latest(symbol_root)
        atm_map: Dict[str, float] = js.get("atm_iv_by_T", {})
        slope_map: Dict[str, float] = js.get("slope_by_T", {})
        if not atm_map:
            return {"atm_iv_short": 0.0, "smile_slope_short": 0.0, "term_ratio": 1.0}

        # Keys are str(T). Find min/max T
        Ts = sorted(float(k) for k in atm_map.keys())
        short_T, long_T = Ts[0], Ts[-1]
        atm_short = float(atm_map.get(str(short_T), 0.0))
        atm_long = float(atm_map.get(str(long_T), atm_short))
        slope_short = float(slope_map.get(str(short_T), 0.0))
        term_ratio = (atm_long / atm_short) if (atm_short > 0) else 1.0
        return {
            "atm_iv_short": atm_short,
            "smile_slope_short": slope_short,
            "term_ratio": float(term_ratio),
        }

    # ---------- Internals ----------
    def _cache_path(self, symbol_root: str) -> str:
        return os.path.join(self.outdir, f"{symbol_root}.json")

    def _is_fresh(self, path: str) -> bool:
        if not os.path.exists(path):
            return False
        try:
            mtime = os.path.getmtime(path)
        except Exception:
            return False
        return (time.time() - mtime) < self.recalc_after_secs

    def _chain_to_json(self, chain: Chain) -> Dict[str, Any]:
        rows_iv: List[Dict[str, float]] = []
        # Compute IV for each row using GK with mid price
        for r in chain.rows:
            mid = max(0.0, 0.5 * (float(r.bid) + float(r.ask)))
            F = float(chain.S0) * math.exp((float(chain.rd) - float(chain.rf)) * float(r.T))
            cp_sign = +1 if str(r.cp).upper().startswith("C") else -1
            try:
                iv = float(implied_vol_gk(F, float(r.K), float(r.T), float(chain.rd), float(chain.rf), mid, cp_sign))
            except Exception:
                iv = 0.0
            rows_iv.append({
                "K": float(r.K),
                "T": float(r.T),
                "cp": "C" if cp_sign > 0 else "P",
                "bid": float(r.bid),
                "ask": float(r.ask),
                "mid": float(mid),
                "iv": float(iv),
            })

        atm_by_T, slope_by_T = self._compute_summary(rows_iv, float(chain.S0), float(chain.rd), float(chain.rf))
        result = {
            "symbol_root": chain.symbol_root,
            "asof": datetime.now(timezone.utc).isoformat(),
            "S0": float(chain.S0),
            "rd": float(chain.rd),
            "rf": float(chain.rf),
            "rows": rows_iv,
            "atm_iv_by_T": {str(k): v for k, v in atm_by_T.items()},
            "slope_by_T": {str(k): v for k, v in slope_by_T.items()},
        }
        return result

    def _compute_summary(self, rows_iv: List[Dict[str, float]], S0: float, rd: float, rf: float) -> Tuple[Dict[float, float], Dict[float, float]]:
        # Group by maturity T (exact float matches). If needed, can bucket/round.
        buckets: Dict[float, List[Dict[str, float]]] = {}
        for row in rows_iv:
            T = float(row["T"]) if math.isfinite(row["T"]) else 0.0
            buckets.setdefault(T, []).append(row)

        atm_by_T: Dict[float, float] = {}
        slope_by_T: Dict[float, float] = {}
        for T, rows in buckets.items():
            if T <= 0 or not rows:
                continue
            F = S0 * math.exp((rd - rf) * T)
            # ATM IV: pick K closest to F
            rows_sorted = sorted(rows, key=lambda x: abs(float(x["K"]) - F))
            atm_iv = float(rows_sorted[0]["iv"]) if rows_sorted else 0.0
            atm_by_T[T] = atm_iv

            # Smile slope: linear regression of iv vs moneyness m = ln(K/F)
            xs: List[float] = []
            ys: List[float] = []
            for rr in rows:
                K = float(rr["K"]) or 1e-12
                m = math.log(max(K, 1e-12) / max(F, 1e-12))
                iv = float(rr["iv"]) or 0.0
                if math.isfinite(m) and math.isfinite(iv) and iv > 0.0:
                    xs.append(m)
                    ys.append(iv)
            slope = 0.0
            if len(xs) >= 2:
                xbar = sum(xs) / len(xs)
                ybar = sum(ys) / len(ys)
                num = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
                den = sum((x - xbar) ** 2 for x in xs) or 1e-12
                slope = num / den
            slope_by_T[T] = float(slope)

        return atm_by_T, slope_by_T
