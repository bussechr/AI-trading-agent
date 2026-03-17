from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class PositionState:
    symbol: str
    side: str
    entry_price: float
    entry_time: float
    vol_entry: float
    r_distance: float
    peak_price: float
    trough_price: float
    best_favorable_dist: float = 0.0
    last_price: float = 0.0
    last_update_ts: float = 0.0


class RiskManager:
    """
    Lightweight risk-exit manager used by FXELAgent.
    Tracks per-position state and returns (should_close, reason).
    """

    def __init__(self, cfg: dict[str, Any]):
        self.trailing_mult = float(max(cfg.get("risk_trailing_mult", 3.0), 0.1))
        self.risk_per_trade = float(max(cfg.get("risk_per_trade_pct", 0.01), 0.0))
        self.target_r = float(max(cfg.get("risk_reward_target", 2.0), 0.1))
        self.time_limit_hours = float(max(cfg.get("risk_time_limit_hours", 24.0), 0.5))
        self.stagnation_minutes = float(max(cfg.get("risk_stagnation_minutes", 60.0), 1.0))
        self.regime_exit_th = float(max(cfg.get("risk_regime_exit_th", 0.0), 0.0))
        self.min_r_pips = float(max(cfg.get("risk_min_r_pips", 10.0), 0.0))
        # Small grace period after entry to avoid immediate churn.
        self.min_hold_secs = float(max(cfg.get("risk_exit_min_hold_secs", 300.0), 0.0))
        self.positions: dict[str, PositionState] = {}

    @staticmethod
    def _pip_size(symbol: str) -> float:
        return 0.01 if "JPY" in str(symbol or "").upper() else 0.0001

    def _min_stop_distance(self, symbol: str) -> float:
        return float(max(self.min_r_pips, 0.0) * self._pip_size(symbol))

    def _compute_r_distance(self, symbol: str, entry_price: float, vol: float) -> float:
        base = abs(float(entry_price)) * max(float(vol), 1e-6) * max(float(self.trailing_mult), 0.1)
        return float(max(base, self._min_stop_distance(symbol), 1e-8))

    def update_position_state(
        self,
        *,
        symbol: str,
        current_price: float,
        entry_price: float,
        side: str,
        vol: float,
        entry_time: float,
    ) -> None:
        sym = str(symbol or "").upper()
        sd = str(side or "").upper()
        if sym == "" or sd not in {"BUY", "SELL"}:
            return
        if (not math.isfinite(current_price)) or (current_price <= 0.0):
            return
        if (not math.isfinite(entry_price)) or (entry_price <= 0.0):
            return
        if not math.isfinite(entry_time):
            entry_time = 0.0

        r_dist = self._compute_r_distance(sym, float(entry_price), float(vol))
        st = self.positions.get(sym)
        reset = (
            st is None
            or st.side != sd
            or abs(float(st.entry_price) - float(entry_price)) > 1e-10
            or abs(float(st.entry_time) - float(entry_time)) > 1e-6
        )
        if reset:
            st = PositionState(
                symbol=sym,
                side=sd,
                entry_price=float(entry_price),
                entry_time=float(entry_time),
                vol_entry=float(max(vol, 0.0)),
                r_distance=float(r_dist),
                peak_price=float(current_price),
                trough_price=float(current_price),
                best_favorable_dist=0.0,
                last_price=float(current_price),
                last_update_ts=float(entry_time),
            )
            self.positions[sym] = st
            return

        # Update in-place for an existing position.
        st.last_price = float(current_price)
        st.last_update_ts = float(entry_time if entry_time > 0 else st.last_update_ts)
        # Keep current risk distance responsive to trailing multiplier changes, but never below pip floor.
        st.r_distance = float(max(st.r_distance, self._min_stop_distance(sym), r_dist))
        st.peak_price = float(max(st.peak_price, current_price))
        st.trough_price = float(min(st.trough_price, current_price))
        if st.side == "BUY":
            st.best_favorable_dist = float(max(st.best_favorable_dist, st.peak_price - st.entry_price))
        else:
            st.best_favorable_dist = float(max(st.best_favorable_dist, st.entry_price - st.trough_price))

    def check_exit(
        self,
        symbol: str,
        current_price: float,
        vol: float,
        p_trend: float,
        now_ts: float,
        *,
        min_hold_secs_override: float | None = None,
        time_limit_hours_override: float | None = None,
        stagnation_minutes_override: float | None = None,
        regime_exit_th_override: float | None = None,
    ) -> tuple[bool, str]:
        sym = str(symbol or "").upper()
        st = self.positions.get(sym)
        if st is None:
            return False, ""
        if (not math.isfinite(current_price)) or current_price <= 0.0:
            return False, ""
        if not math.isfinite(now_ts):
            now_ts = st.entry_time

        # Refresh dynamic state with latest marks.
        st.last_price = float(current_price)
        st.peak_price = float(max(st.peak_price, current_price))
        st.trough_price = float(min(st.trough_price, current_price))
        st.r_distance = float(max(st.r_distance, self._compute_r_distance(sym, st.entry_price, vol)))

        min_hold_secs = float(max(self.min_hold_secs, 0.0))
        time_limit_hours = float(max(self.time_limit_hours, 0.1))
        stagnation_minutes = float(max(self.stagnation_minutes, 0.1))
        regime_exit_th = float(max(self.regime_exit_th, 0.0))
        if min_hold_secs_override is not None:
            try:
                min_hold_secs = float(max(float(min_hold_secs_override), 0.0))
            except Exception:
                pass
        if time_limit_hours_override is not None:
            try:
                time_limit_hours = float(max(float(time_limit_hours_override), 0.1))
            except Exception:
                pass
        if stagnation_minutes_override is not None:
            try:
                stagnation_minutes = float(max(float(stagnation_minutes_override), 0.1))
            except Exception:
                pass
        if regime_exit_th_override is not None:
            try:
                regime_exit_th = float(max(float(regime_exit_th_override), 0.0))
            except Exception:
                pass

        elapsed = float(max(now_ts - float(st.entry_time), 0.0))
        if elapsed < min_hold_secs:
            return False, ""

        r = max(float(st.r_distance), 1e-9)
        if st.side == "BUY":
            pnl_dist = float(current_price - st.entry_price)
            trailing_stop = float(st.peak_price - r)
            if current_price <= trailing_stop:
                return True, "risk_trailing_stop"
            if pnl_dist <= -r:
                return True, "risk_hard_stop"
        else:
            pnl_dist = float(st.entry_price - current_price)
            trailing_stop = float(st.trough_price + r)
            if current_price >= trailing_stop:
                return True, "risk_trailing_stop"
            if pnl_dist <= -r:
                return True, "risk_hard_stop"

        # Regime deterioration exit.
        try:
            p_trend_f = float(p_trend)
        except Exception:
            p_trend_f = 0.5
        if math.isfinite(p_trend_f) and p_trend_f < regime_exit_th:
            return True, "risk_regime_exit"

        # Time/stagnation exit after minimum holding horizon.
        if elapsed >= time_limit_hours * 3600.0 and elapsed >= stagnation_minutes * 60.0:
            if abs(pnl_dist) < 0.25 * r:
                return True, "risk_time_stagnation"

        return False, ""
