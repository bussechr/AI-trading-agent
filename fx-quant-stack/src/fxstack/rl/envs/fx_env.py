from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from fxstack.risk.contracts import MarketState, PortfolioState, PolicyIntent, RiskDecision
from fxstack.risk.kernel import RiskKernelConfig, evaluate_risk_decision
from fxstack.rl.contracts import RLEpisodeEvent, RLEpisodeRow, RLObservation, RLRunConfig, RLTradeAction
from fxstack.rl.reward import compute_reward_breakdown

try:  # pragma: no cover - gymnasium may not be installed in all developer environments
    import gymnasium as gym
    from gymnasium import spaces
    _HAS_GYMNASIUM = True
except Exception:  # pragma: no cover
    gym = None
    _HAS_GYMNASIUM = False

    class _Space:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self.args = args
            self.kwargs = kwargs

    class spaces:  # type: ignore[no-redef]
        class Box(_Space):
            pass

        class Dict(_Space):
            pass

        class Discrete(_Space):
            pass

    class _EnvBase:
        observation_space = None
        action_space = None

        def reset(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def step(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    gym = type("gym", (), {"Env": _EnvBase})


@dataclass(slots=True)
class _PortfolioView:
    position: float = 0.0
    entry_price: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    drawdown_pct: float = 0.0
    open_position_count: int = 0


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _as_float_series(row: dict[str, Any], keys: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in keys:
        try:
            out[key] = float(row.get(key, 0.0) or 0.0)
        except Exception:
            out[key] = 0.0
    return out


class FxTradingEnv(gym.Env):  # type: ignore[misc]
    metadata = {"render_modes": []}

    def __init__(
        self,
        *,
        observations: pd.DataFrame,
        pair: str,
        timeframe: str,
        initial_equity: float = 0.0,
        max_position_abs: float = 1.0,
        action_deadband: float = 0.05,
        reward_scale: float = 1.0,
        max_drawdown_pct: float = 0.25,
        max_freshness_secs: float = 3600.0,
        risk_config: RiskKernelConfig | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.pair = str(pair).upper()
        self.timeframe = str(timeframe).upper()
        self.initial_equity = float(initial_equity)
        self.max_position_abs = float(max(1e-9, max_position_abs))
        self.action_deadband = float(max(0.0, action_deadband))
        self.reward_scale = float(reward_scale)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.max_freshness_secs = float(max_freshness_secs)
        self.risk_config = risk_config or RiskKernelConfig(max_drawdown_pct=max_drawdown_pct, freshness_limit_secs=max_freshness_secs)
        self.metadata = dict(metadata or {})
        self._frame = self._prepare_frame(observations)
        self._cursor = 0
        self._position = _PortfolioView()
        self._equity = float(initial_equity)
        self._last_obs: dict[str, Any] | None = None
        self._terminated = False
        self._truncated = False

        numeric_cols = list(self._frame.select_dtypes(include=[np.number]).columns)
        feature_keys = [col for col in numeric_cols if col not in {"ts", "pair", "timeframe"}]
        self._feature_keys = feature_keys
        self.observation_space = spaces.Dict(
            {
                "market": spaces.Dict(
                    {
                        "spread_bps": spaces.Box(-np.inf, np.inf, shape=()),
                        "freshness_secs": spaces.Box(-np.inf, np.inf, shape=()),
                        "volatility": spaces.Box(-np.inf, np.inf, shape=()),
                        "liquidity_score": spaces.Box(-np.inf, np.inf, shape=()),
                        "regime": spaces.Box(-np.inf, np.inf, shape=()),
                        "session_bucket": spaces.Box(-np.inf, np.inf, shape=()),
                    }
                ),
                "portfolio": spaces.Dict(
                    {
                        "equity": spaces.Box(-np.inf, np.inf, shape=()),
                        "balance": spaces.Box(-np.inf, np.inf, shape=()),
                        "position": spaces.Box(-np.inf, np.inf, shape=()),
                        "drawdown_pct": spaces.Box(-np.inf, np.inf, shape=()),
                        "open_position_count": spaces.Box(-np.inf, np.inf, shape=()),
                        "gross_exposure": spaces.Box(-np.inf, np.inf, shape=()),
                        "net_exposure": spaces.Box(-np.inf, np.inf, shape=()),
                    }
                ),
                "policy": spaces.Dict(
                    {
                        "expected_edge_bps": spaces.Box(-np.inf, np.inf, shape=()),
                        "confidence": spaces.Box(-np.inf, np.inf, shape=()),
                        "action_deadband": spaces.Box(-np.inf, np.inf, shape=()),
                        "max_position_abs": spaces.Box(-np.inf, np.inf, shape=()),
                        "risk_verdict": spaces.Box(-np.inf, np.inf, shape=()),
                    }
                ),
                "features": spaces.Box(-np.inf, np.inf, shape=(len(feature_keys),), dtype=np.float32),
            }
        )
        self.action_space = spaces.Box(low=-1.0, high=1.0, shape=(1,), dtype=np.float32)

    @staticmethod
    def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return pd.DataFrame(columns=["ts", "pair", "timeframe"])
        out = df.copy()
        if "ts" in out.columns:
            out["ts"] = pd.to_datetime(out["ts"], utc=True, errors="coerce")
        else:
            out["ts"] = pd.RangeIndex(len(out)).astype(str)
        out = out.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
        if "pair" not in out.columns:
            out["pair"] = ""
        if "timeframe" not in out.columns:
            out["timeframe"] = ""
        return out

    def _current_row(self) -> dict[str, Any]:
        if self._cursor >= len(self._frame):
            return {}
        return dict(self._frame.iloc[self._cursor].to_dict())

    def _build_observation(self, row: dict[str, Any]) -> dict[str, Any]:
        features = _as_float_series(row, self._feature_keys)
        market = {
            "spread_bps": float(features.get("spread_bps", row.get("spread_bps", 0.0) or 0.0)),
            "freshness_secs": float(features.get("freshness_secs", row.get("freshness_secs", 0.0) or 0.0)),
            "volatility": float(features.get("vol_20", row.get("vol_20", 0.0) or 0.0)),
            "liquidity_score": float(features.get("liquidity_score", row.get("liquidity_score", 0.0) or 0.0)),
            "regime": str(row.get("regime_bucket") or row.get("regime") or ""),
            "session_bucket": str(row.get("session_bucket") or ""),
        }
        portfolio = {
            "equity": float(self._equity),
            "balance": float(self._equity - self._position.realized_pnl_usd),
            "position": float(self._position.position),
            "drawdown_pct": float(self._position.drawdown_pct),
            "open_position_count": int(self._position.open_position_count),
            "gross_exposure": abs(float(self._position.position)),
            "net_exposure": float(self._position.position),
        }
        policy = {
            "expected_edge_bps": float(row.get("expected_edge_bps", row.get("calibrated_ev_bps_shadow", 0.0)) or 0.0),
            "confidence": float(row.get("trade_prob", row.get("entry_prob", 0.0)) or 0.0),
            "action_deadband": float(self.action_deadband),
            "max_position_abs": float(self.max_position_abs),
            "risk_verdict": 1.0,
        }
        return {
            "market": market,
            "portfolio": portfolio,
            "policy": policy,
            "features": np.asarray([float(features.get(col, 0.0) or 0.0) for col in self._feature_keys], dtype=np.float32),
        }

    def _risk_decision(self, row: dict[str, Any], action: RLTradeAction) -> RiskDecision:
        policy = PolicyIntent(
            pair=self.pair,
            side=str(row.get("side") or row.get("signal_side") or "BUY").upper(),
            intent="ENTRY" if abs(float(action.target_position)) > self.action_deadband else "HOLD",
            action="entry" if abs(float(action.target_position)) > self.action_deadband else "hold",
            action_score=float(row.get("trade_prob", 0.0) or 0.0),
            strategy=str(row.get("playbook") or ""),
            playbook=str(row.get("playbook") or ""),
            thesis_id=str(row.get("thesis_id") or ""),
            campaign_state=str(row.get("campaign_state") or ""),
            conviction_band=str(row.get("conviction_band") or ""),
            thesis_stage=str(row.get("thesis_stage") or ""),
            portfolio_posture=str(row.get("portfolio_posture") or ""),
            expected_edge_bps=float(row.get("expected_edge_bps", row.get("calibrated_ev_bps_shadow", 0.0)) or 0.0),
            confidence=float(row.get("trade_prob", row.get("entry_prob", 0.0)) or 0.0),
            metadata={
                "target_position": float(action.target_position),
                "close_position": bool(action.close_position),
                "tighten_stop": bool(action.tighten_stop),
                "stop_loss": float(action.stop_loss),
                "take_profit": float(action.take_profit),
                "lifecycle_action": str(row.get("lifecycle_action") or "hold"),
                "has_open_position": bool(abs(self._position.position) > 1e-9),
            },
        )
        market = MarketState(
            pair=self.pair,
            ts=str(row.get("ts") or ""),
            session_bucket=str(row.get("session_bucket") or ""),
            regime=str(row.get("regime_bucket") or row.get("regime") or ""),
            spread_bps=float(row.get("spread_bps", 0.0) or 0.0),
            allowed_spread_bps=float(row.get("allowed_spread_bps", row.get("max_spread_bps", 0.0)) or 0.0),
            marketable=bool(row.get("marketable", True)),
            market_open=bool(row.get("market_open", True)),
            data_fresh=bool(row.get("data_fresh", True)),
            freshness_secs=(None if row.get("freshness_secs") is None else float(row.get("freshness_secs"))),
            freshness_limit_secs=float(self.max_freshness_secs),
            volatility=float(row.get("vol_20", 0.0) or 0.0),
            liquidity_score=float(row.get("liquidity_score", 0.0) or 0.0),
            metadata=dict(row),
        )
        portfolio = PortfolioState(
            equity=float(self._equity),
            balance=float(self._equity - self._position.realized_pnl_usd),
            peak_equity=float(max(self._equity, self.initial_equity)),
            drawdown_pct=float(self._position.drawdown_pct),
            open_position_count=int(self._position.open_position_count),
            pair_position_count=1 if abs(self._position.position) > 1e-9 else 0,
            max_total_positions=1,
            max_pair_positions=1,
            gross_exposure=abs(float(self._position.position)),
            net_exposure=float(self._position.position),
            capital_at_risk_pct=min(1.0, abs(float(self._position.position)) / max(1e-9, float(self.max_position_abs))),
            sleeve=str(row.get("playbook") or ""),
            replacement_pressure=float(row.get("replacement_pressure", 0.0) or 0.0),
            metadata=dict(row),
        )
        return evaluate_risk_decision(policy_intent=policy, market_state=market, portfolio_state=portfolio, config=self.risk_config)

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        if _HAS_GYMNASIUM:
            super().reset(seed=seed)
        self._cursor = 0
        self._position = _PortfolioView()
        self._equity = float(self.initial_equity)
        self._terminated = False
        self._truncated = False
        row = self._current_row()
        obs = self._build_observation(row) if row else {
            "market": {"spread_bps": 0.0, "freshness_secs": 0.0, "volatility": 0.0, "liquidity_score": 0.0, "regime": "", "session_bucket": ""},
            "portfolio": {"equity": self._equity, "balance": self._equity, "position": 0.0, "drawdown_pct": 0.0, "open_position_count": 0, "gross_exposure": 0.0, "net_exposure": 0.0},
            "policy": {"expected_edge_bps": 0.0, "confidence": 0.0, "action_deadband": self.action_deadband, "max_position_abs": self.max_position_abs, "risk_verdict": 1.0},
            "features": np.zeros((len(self._feature_keys),), dtype=np.float32),
        }
        self._last_obs = obs
        info = {"pair": self.pair, "timeframe": self.timeframe, "rows": int(len(self._frame))}
        return obs, info

    def step(self, action: np.ndarray | list[float] | tuple[float, ...]):
        if self._terminated or self._truncated:
            raise RuntimeError("episode has terminated; call reset()")
        row = self._current_row()
        if not row:
            self._truncated = True
            empty_obs = self._last_obs or self.reset()[0]
            return empty_obs, 0.0, False, True, {"reason": "no_more_rows"}
        target = float(np.asarray(action, dtype=float).reshape(-1)[0])
        target = max(-1.0, min(1.0, target))
        action_obj = RLTradeAction(
            target_position=target,
            close_position=abs(target) <= self.action_deadband,
            tighten_stop=bool(row.get("tighten_stop", False)),
            stop_loss=float(row.get("stop_loss", 0.0) or 0.0),
            take_profit=float(row.get("take_profit", 0.0) or 0.0),
            metadata={"raw_action": target},
        )
        risk_decision = self._risk_decision(row, action_obj)
        if risk_decision.verdict == "block" and abs(target) > self.action_deadband:
            executed_target = 0.0
        elif risk_decision.lifecycle_action in {"exit", "partial_tp"}:
            executed_target = 0.0 if risk_decision.lifecycle_action == "exit" else float(np.sign(self._position.position) * max(0.0, abs(self._position.position) * 0.5))
        else:
            executed_target = float(target)
        delta = float(executed_target - self._position.position)
        turnover = abs(delta)
        spread_bps = float(row.get("spread_bps", 0.0) or 0.0)
        slippage_bps = float(getattr(self.risk_config, "slippage_bps", 0.0) or 0.0)
        cost_bps = spread_bps + slippage_bps + (0.25 if abs(delta) > 1e-9 else 0.0)
        pnl_move = float(row.get("ret_1", 0.0) or 0.0) * float(self._position.position)
        realized_pnl_usd = float(row.get("realized_pnl_usd", 0.0) or 0.0)
        unrealized_pnl_usd = float(row.get("unrealized_pnl_usd", 0.0) or 0.0) + pnl_move
        drawdown_pct = float(row.get("drawdown_pct", self._position.drawdown_pct) or self._position.drawdown_pct)
        if abs(executed_target) > self.max_position_abs:
            executed_target = float(np.sign(executed_target) * self.max_position_abs)
        self._position.position = float(executed_target)
        self._position.entry_price = float(row.get("mid_close", row.get("price", 0.0)) or 0.0)
        self._position.realized_pnl_usd += float(realized_pnl_usd)
        self._position.unrealized_pnl_usd = float(unrealized_pnl_usd)
        self._position.drawdown_pct = float(drawdown_pct)
        self._position.open_position_count = 1 if abs(self._position.position) > 1e-9 else 0

        reward_breakdown = compute_reward_breakdown(
            realized_pnl_usd=realized_pnl_usd,
            unrealized_pnl_usd=unrealized_pnl_usd,
            cost_bps=cost_bps,
            drawdown_pct=drawdown_pct,
            target_position=target,
            filled_position=executed_target,
            terminated=False,
            truncated=False,
            reward_scale=self.reward_scale,
            metadata={"risk_verdict": risk_decision.verdict, "risk_reason": risk_decision.reason, "turnover": turnover},
        )
        reward = float(reward_breakdown.total)
        self._equity += realized_pnl_usd
        self._cursor += 1
        terminated = bool(self.risk_config.max_drawdown_pct > 0.0 and self._position.drawdown_pct >= self.risk_config.max_drawdown_pct and self.risk_config.allow_lifecycle_overrides)
        truncated = self._cursor >= len(self._frame)
        self._terminated = terminated
        self._truncated = truncated
        next_row = self._current_row()
        next_obs = self._build_observation(next_row) if next_row else None
        event = RLEpisodeEvent(
            step=int(self._cursor),
            ts=str(row.get("ts") or ""),
            pair=self.pair,
            observation=self._last_obs or {},
            action=action_obj.to_dict(),
            reward=float(reward),
            terminated=bool(terminated),
            truncated=bool(truncated),
            next_observation=next_obs,
            info={
                "risk": risk_decision.to_dict(),
                "reward_breakdown": reward_breakdown.to_dict(),
                "target_position": float(target),
                "filled_position": float(executed_target),
                "cost_bps": float(cost_bps),
            },
        )
        self._last_obs = next_obs or self._build_observation(row)
        info = event.to_dict()["info"]
        info["event"] = event.to_dict()
        return (next_obs or self._last_obs), float(reward), bool(terminated), bool(truncated), info
