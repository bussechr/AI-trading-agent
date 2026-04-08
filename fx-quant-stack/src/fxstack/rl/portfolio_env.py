from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from fxstack.risk.contracts import MarketState, PortfolioState, PolicyIntent, RiskDecision
from fxstack.risk.kernel import RiskKernelConfig, evaluate_risk_decision
from fxstack.rl.contracts import RLEpisodeEvent, RLPortfolioAction, RLPortfolioObservation, RLTradeAction
from fxstack.rl.reward import compute_reward_breakdown

try:  # pragma: no cover - gymnasium may be absent in some developer shells
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

    class _EnvBase:
        observation_space = None
        action_space = None

        def reset(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

        def step(self, *args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError

    gym = type("gym", (), {"Env": _EnvBase})


@dataclass(slots=True)
class _PairView:
    position: float = 0.0
    entry_price: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    drawdown_pct: float = 0.0


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _is_jsonish(value: Any) -> bool:
    return isinstance(value, str) and value[:1] in {"{", "["}


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, (np.floating, np.integer)):
            return float(value)
        return float(value)
    except Exception:
        return float(default)


def _prepare_frame(df: pd.DataFrame, *, pair: str | None = None) -> pd.DataFrame:
    if df.empty:
        base = pd.DataFrame(columns=["ts", "pair", "timeframe"])
        if pair:
            base["pair"] = pair
        return base
    out = df.copy()
    if "ts" in out.columns:
        out["ts"] = pd.to_datetime(out["ts"], utc=True, errors="coerce")
    else:
        out["ts"] = pd.RangeIndex(len(out)).astype(str)
    out = out.dropna(subset=["ts"]).sort_values("ts").reset_index(drop=True)
    if "pair" not in out.columns:
        out["pair"] = str(pair or "")
    else:
        out["pair"] = out["pair"].astype(str).str.upper()
    if "timeframe" not in out.columns:
        out["timeframe"] = ""
    return out


def _frame_map(
    observations: pd.DataFrame | dict[str, pd.DataFrame],
    *,
    pair_universe: list[str] | None = None,
) -> tuple[dict[str, pd.DataFrame], list[str]]:
    frames: dict[str, pd.DataFrame] = {}
    if isinstance(observations, dict):
        for pair, frame in observations.items():
            key = str(pair).upper()
            frames[key] = _prepare_frame(frame, pair=key)
    else:
        frame = _prepare_frame(observations)
        if "pair" in frame.columns and frame["pair"].astype(str).str.strip().any():
            for pair, group in frame.groupby(frame["pair"].astype(str).str.upper(), dropna=False):
                frames[str(pair).upper()] = _prepare_frame(group, pair=str(pair).upper())
        else:
            pairs = [str(pair).upper() for pair in list(pair_universe or [])] or ["PORTFOLIO"]
            if len(pairs) == 1:
                frames[pairs[0]] = _prepare_frame(frame, pair=pairs[0])
            else:
                for pair in pairs:
                    frames[pair] = _prepare_frame(frame.copy(), pair=pair)
    universe = [str(pair).upper() for pair in list(pair_universe or []) if str(pair).strip()]
    if not universe:
        universe = sorted(frames)
    for pair in universe:
        frames.setdefault(pair, _prepare_frame(pd.DataFrame(), pair=pair))
    return frames, universe or sorted(frames)


def _timeline_from_frames(frames: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
    stamps: list[pd.Timestamp] = []
    for frame in frames.values():
        if "ts" not in frame.columns or frame.empty:
            continue
        for ts in frame["ts"].tolist():
            if isinstance(ts, pd.Timestamp):
                stamps.append(ts)
            else:
                parsed = pd.to_datetime(ts, utc=True, errors="coerce")
                if pd.notna(parsed):
                    stamps.append(pd.Timestamp(parsed))
    if not stamps:
        return []
    return sorted({pd.Timestamp(ts) for ts in stamps})


def _latest_row(frame: pd.DataFrame, ts: pd.Timestamp) -> dict[str, Any]:
    if frame.empty:
        return {}
    ordered = frame.sort_values("ts").reset_index(drop=True)
    ts_series = pd.to_datetime(ordered["ts"], utc=True, errors="coerce")
    mask = ts_series <= ts
    if mask.any():
        return dict(ordered.loc[mask].iloc[-1].to_dict())
    return dict(ordered.iloc[0].to_dict())


def _flatten_numeric(row: dict[str, Any], keys: list[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for key in keys:
        out[key] = _to_float(row.get(key, 0.0), 0.0)
    return out


def _feature_keys(frames: dict[str, pd.DataFrame]) -> list[str]:
    keys: set[str] = set()
    for frame in frames.values():
        if frame.empty:
            continue
        numeric_cols = list(frame.select_dtypes(include=[np.number]).columns)
        for col in numeric_cols:
            if col not in {"ts"}:
                keys.add(str(col))
    return sorted(keys)


def _portfolio_from_positions(
    *,
    positions: dict[str, _PairView],
    equity: float,
    initial_equity: float,
    pair_universe: list[str],
    frames: dict[str, pd.DataFrame],
    focus_pair: str | None = None,
) -> PortfolioState:
    open_positions = {pair: view for pair, view in positions.items() if abs(view.position) > 1e-9}
    gross_exposure = sum(abs(view.position) for view in positions.values())
    net_exposure = sum(view.position for view in positions.values())
    current_session = ""
    session_counts: dict[str, int] = {}
    for pair, view in positions.items():
        row = frames.get(pair, pd.DataFrame())
        if not row.empty:
            last = dict(row.iloc[-1].to_dict())
            session = str(last.get("session_bucket") or "")
            if session:
                session_counts[session] = session_counts.get(session, 0) + (1 if abs(view.position) > 1e-9 else 0)
                if not current_session:
                    current_session = session
    concentration = 0.0
    if gross_exposure > 1e-9:
        largest = max((abs(view.position) for view in positions.values()), default=0.0)
        concentration = float(largest / gross_exposure)
    return PortfolioState(
        equity=float(equity),
        balance=float(equity),
        peak_equity=float(max(initial_equity, equity)),
        drawdown_pct=float(max(0.0, (initial_equity - equity) / max(1e-9, initial_equity))),
        open_position_count=int(len(open_positions)),
        pair_position_count=int(1 if focus_pair and abs(positions.get(focus_pair, _PairView()).position) > 1e-9 else (len(open_positions) if focus_pair is None else 0)),
        max_total_positions=int(max(1, len(pair_universe))),
        max_pair_positions=1,
        gross_exposure=float(gross_exposure),
        net_exposure=float(net_exposure),
        capital_at_risk_pct=float(_clip01(gross_exposure / max(1e-9, float(initial_equity or 1.0)))),
        sleeve=str(current_session or "portfolio"),
        replacement_pressure=float(concentration),
        metadata={
            "pair_exposure": {pair: float(view.position) for pair, view in positions.items()},
            "session_counts": dict(session_counts),
            "concentration": float(concentration),
        },
    )


class PortfolioFxTradingEnv(gym.Env):  # type: ignore[misc]
    metadata = {"render_modes": []}

    def __init__(
        self,
        *,
        observations: pd.DataFrame | dict[str, pd.DataFrame],
        pair_universe: list[str] | None = None,
        timeframe: str = "M5",
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
        self.timeframe = str(timeframe).upper()
        self.initial_equity = float(initial_equity)
        self.max_position_abs = float(max(1e-9, max_position_abs))
        self.action_deadband = float(max(0.0, action_deadband))
        self.reward_scale = float(reward_scale)
        self.max_drawdown_pct = float(max_drawdown_pct)
        self.max_freshness_secs = float(max_freshness_secs)
        self.risk_config = risk_config or RiskKernelConfig(
            max_drawdown_pct=max_drawdown_pct,
            freshness_limit_secs=max_freshness_secs,
        )
        self.metadata = dict(metadata or {})
        self._frames, self._pair_universe = _frame_map(observations, pair_universe=pair_universe)
        self._timeline = _timeline_from_frames(self._frames)
        self._feature_keys = _feature_keys(self._frames)
        self._positions = {pair: _PairView() for pair in self._pair_universe}
        self._cursor = 0
        self._equity = float(initial_equity)
        self._terminated = False
        self._truncated = False
        self._last_obs: dict[str, Any] | None = None

        pair_market_space = spaces.Dict(
            {
                "spread_bps": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "freshness_secs": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "volatility": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "liquidity_score": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "regime": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "session_bucket": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "market_open": spaces.Box(0.0, 1.0, shape=(), dtype=np.float32),
                "data_fresh": spaces.Box(0.0, 1.0, shape=(), dtype=np.float32),
            }
        )
        pair_feature_space = spaces.Box(
            -np.inf,
            np.inf,
            shape=(len(self._feature_keys),),
            dtype=np.float32,
        )
        self.observation_space = spaces.Dict(
            {
                "ts": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                "pair_universe": spaces.Box(-np.inf, np.inf, shape=(max(1, len(self._pair_universe)),), dtype=np.float32),
                "market_by_pair": spaces.Dict({pair: pair_market_space for pair in self._pair_universe}),
                "features_by_pair": spaces.Dict({pair: pair_feature_space for pair in self._pair_universe}),
                "portfolio": spaces.Dict(
                    {
                        "equity": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "balance": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "open_position_count": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "pair_position_count": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "gross_exposure": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "net_exposure": spaces.Box(-np.inf, np.inf, shape=(), dtype=np.float32),
                        "capital_at_risk_pct": spaces.Box(0.0, 1.0, shape=(), dtype=np.float32),
                    }
                ),
                "policy_context": spaces.Dict({}),
                "action_mask": spaces.Dict({pair: spaces.Dict({}) for pair in self._pair_universe}),
                "metadata": spaces.Dict({}),
            }
        )
        self.action_space = spaces.Dict(
            {
                "portfolio_bias": spaces.Box(-1.0, 1.0, shape=(), dtype=np.float32),
                "pair_actions": spaces.Dict({pair: spaces.Box(-1.0, 1.0, shape=(5,), dtype=np.float32) for pair in self._pair_universe}),
            }
        )

    def _row_for_pair(self, pair: str) -> dict[str, Any]:
        if self._cursor >= len(self._timeline):
            return {}
        frame = self._frames.get(pair, pd.DataFrame())
        return _latest_row(frame, self._timeline[self._cursor])

    def _build_observation(self) -> dict[str, Any]:
        market_by_pair: dict[str, dict[str, Any]] = {}
        features_by_pair: dict[str, dict[str, float]] = {}
        action_mask: dict[str, dict[str, Any]] = {}
        for pair in self._pair_universe:
            row = self._row_for_pair(pair)
            feature_payload = _flatten_numeric(row, self._feature_keys)
            features_by_pair[pair] = feature_payload
            market_by_pair[pair] = {
                "spread_bps": _to_float(row.get("spread_bps", 0.0), 0.0),
                "freshness_secs": _to_float(row.get("freshness_secs", 0.0), 0.0),
                "volatility": _to_float(row.get("vol_20", row.get("volatility", 0.0)), 0.0),
                "liquidity_score": _to_float(row.get("liquidity_score", 0.0), 0.0),
                "regime": str(row.get("regime_bucket") or row.get("regime") or ""),
                "session_bucket": str(row.get("session_bucket") or ""),
                "market_open": bool(row.get("market_open", True)),
                "data_fresh": bool(row.get("data_fresh", True)),
            }
            action_mask[pair] = {
                "can_open": bool(abs(self._positions[pair].position) <= 1e-9),
                "can_close": bool(abs(self._positions[pair].position) > 1e-9),
                "can_tighten_stop": bool(abs(self._positions[pair].position) > 1e-9),
                "max_position_abs": float(self.max_position_abs),
            }
        portfolio = _portfolio_from_positions(
            positions=self._positions,
            equity=self._equity,
            initial_equity=self.initial_equity,
            pair_universe=self._pair_universe,
            frames=self._frames,
        )
        policy_context = {
            "pair_universe": list(self._pair_universe),
            "step_index": int(self._cursor),
            "timeline_length": int(len(self._timeline)),
            "supervised_fallback": bool(self.metadata.get("supervised_fallback", True)),
            "portfolio_concentration": float(portfolio.metadata.get("concentration", 0.0)),
            "session_counts": dict(portfolio.metadata.get("session_counts") or {}),
            "feature_contract_hash": str(self.metadata.get("feature_contract_hash") or ""),
        }
        payload = RLPortfolioObservation(
            ts=str(self._timeline[self._cursor].isoformat() if self._cursor < len(self._timeline) else ""),
            pair_universe=list(self._pair_universe),
            market_by_pair=market_by_pair,
            features_by_pair=features_by_pair,
            portfolio=portfolio,
            policy_context=policy_context,
            action_mask=action_mask,
            metadata=dict(self.metadata),
        ).to_dict()
        payload["features_by_pair"] = {
            pair: {name: float(value) for name, value in payload["features_by_pair"][pair].items()}
            for pair in payload["features_by_pair"]
        }
        return payload

    def _parse_action_for_pair(self, pair: str, raw_action: Any) -> RLTradeAction:
        if isinstance(raw_action, RLTradeAction):
            return raw_action
        if isinstance(raw_action, dict):
            return RLTradeAction.from_dict(raw_action)
        if isinstance(raw_action, (list, tuple, np.ndarray)):
            values = list(np.asarray(raw_action, dtype=float).reshape(-1).tolist())
            payload = {
                "target_position": float(values[0]) if values else 0.0,
                "close_position": bool(values[1]) if len(values) > 1 else False,
                "tighten_stop": bool(values[2]) if len(values) > 2 else False,
                "stop_loss": float(values[3]) if len(values) > 3 else 0.0,
                "take_profit": float(values[4]) if len(values) > 4 else 0.0,
            }
            return RLTradeAction.from_dict(payload)
        if raw_action is None:
            return RLTradeAction(target_position=0.0)
        return RLTradeAction(target_position=float(raw_action))

    def _parse_action(self, action: Any) -> RLPortfolioAction:
        if isinstance(action, RLPortfolioAction):
            return action
        if isinstance(action, dict) and ("pair_actions" in action or "actions_by_pair" in action or "actions" in action):
            return RLPortfolioAction.from_dict(action)
        if isinstance(action, dict) and len(self._pair_universe) == 1 and self._pair_universe[0] in action:
            return RLPortfolioAction.from_dict({"pair_actions": action})
        if isinstance(action, (list, tuple, np.ndarray)) and len(self._pair_universe) == 1:
            pair = self._pair_universe[0]
            return RLPortfolioAction(pair_actions={pair: self._parse_action_for_pair(pair, action)})
        if isinstance(action, dict):
            pair_actions: dict[str, RLTradeAction] = {}
            for pair in self._pair_universe:
                if pair in action:
                    pair_actions[pair] = self._parse_action_for_pair(pair, action[pair])
            return RLPortfolioAction(pair_actions=pair_actions, metadata={"raw_action": dict(action)})
        return RLPortfolioAction(pair_actions={pair: RLTradeAction(target_position=0.0) for pair in self._pair_universe})

    def _risk_decision(self, pair: str, row: dict[str, Any], action: RLTradeAction, portfolio: PortfolioState) -> RiskDecision:
        policy = PolicyIntent(
            pair=pair,
            side=str(row.get("side") or row.get("signal_side") or "BUY").upper(),
            intent="ENTRY" if abs(float(action.target_position)) > self.action_deadband else "HOLD",
            action="entry" if abs(float(action.target_position)) > self.action_deadband else "hold",
            action_score=float(row.get("trade_prob", row.get("entry_prob", 0.0)) or 0.0),
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
                "has_open_position": bool(abs(self._positions[pair].position) > 1e-9),
            },
        )
        market = MarketState(
            pair=pair,
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
        return evaluate_risk_decision(policy_intent=policy, market_state=market, portfolio_state=portfolio, config=self.risk_config)

    def reset(self, *, seed: int | None = None, options: dict[str, Any] | None = None):
        if _HAS_GYMNASIUM:
            super().reset(seed=seed)
        self._positions = {pair: _PairView() for pair in self._pair_universe}
        self._cursor = 0
        self._equity = float(self.initial_equity)
        self._terminated = False
        self._truncated = False
        obs = self._build_observation() if self._timeline else {
            "ts": "",
            "pair_universe": list(self._pair_universe),
            "market_by_pair": {pair: {"spread_bps": 0.0, "freshness_secs": 0.0, "volatility": 0.0, "liquidity_score": 0.0, "regime": "", "session_bucket": "", "market_open": True, "data_fresh": True} for pair in self._pair_universe},
            "features_by_pair": {pair: {key: 0.0 for key in self._feature_keys} for pair in self._pair_universe},
            "portfolio": {
                "equity": self._equity,
                "balance": self._equity,
                "peak_equity": self._equity,
                "drawdown_pct": 0.0,
                "open_position_count": 0,
                "pair_position_count": 0,
                "max_total_positions": max(1, len(self._pair_universe)),
                "max_pair_positions": 1,
                "gross_exposure": 0.0,
                "net_exposure": 0.0,
                "capital_at_risk_pct": 0.0,
                "sleeve": "portfolio",
                "replacement_pressure": 0.0,
                "metadata": {},
            },
            "policy_context": {"pair_universe": list(self._pair_universe), "step_index": 0, "timeline_length": 0, "supervised_fallback": True},
            "action_mask": {pair: {"can_open": True, "can_close": False, "can_tighten_stop": False, "max_position_abs": float(self.max_position_abs)} for pair in self._pair_universe},
            "metadata": dict(self.metadata),
        }
        self._last_obs = obs
        info = {
            "pair_universe": list(self._pair_universe),
            "timeframe": self.timeframe,
            "rows": int(sum(len(frame) for frame in self._frames.values())),
            "timeline": int(len(self._timeline)),
        }
        return obs, info

    def step(self, action: RLPortfolioAction | dict[str, Any] | list[float] | tuple[float, ...] | np.ndarray):
        if self._terminated or self._truncated:
            raise RuntimeError("episode has terminated; call reset()")
        if self._cursor >= len(self._timeline):
            self._truncated = True
            empty_obs = self._last_obs or self.reset()[0]
            return empty_obs, 0.0, False, True, {"reason": "no_more_rows"}
        row_snapshot = {pair: self._row_for_pair(pair) for pair in self._pair_universe}
        parsed_action = self._parse_action(action)
        portfolio_before = _portfolio_from_positions(
            positions=self._positions,
            equity=self._equity,
            initial_equity=self.initial_equity,
            pair_universe=self._pair_universe,
            frames=self._frames,
        )
        pair_rewards: dict[str, float] = {}
        risk_decisions: dict[str, dict[str, Any]] = {}
        reward_breakdowns: dict[str, dict[str, Any]] = {}
        executed_targets: dict[str, float] = {}
        for pair in self._pair_universe:
            row = row_snapshot.get(pair, {})
            action_obj = parsed_action.pair_actions.get(pair, RLTradeAction(target_position=0.0))
            target = float(np.clip(action_obj.target_position + (parsed_action.portfolio_bias * 0.10), -1.0, 1.0))
            if action_obj.close_position:
                target = 0.0
            action_obj = RLTradeAction(
                target_position=target,
                close_position=bool(action_obj.close_position or abs(target) <= self.action_deadband),
                tighten_stop=bool(action_obj.tighten_stop),
                stop_loss=float(action_obj.stop_loss),
                take_profit=float(action_obj.take_profit),
                metadata={**dict(action_obj.metadata or {}), "portfolio_bias": float(parsed_action.portfolio_bias)},
            )
            current_portfolio = _portfolio_from_positions(
                positions=self._positions,
                equity=self._equity,
                initial_equity=self.initial_equity,
                pair_universe=self._pair_universe,
                frames=self._frames,
                focus_pair=pair,
            )
            risk_decision = self._risk_decision(pair, row, action_obj, current_portfolio)
            current_position = float(self._positions[pair].position)
            if risk_decision.verdict == "block" and abs(target) > self.action_deadband:
                executed_target = 0.0
            elif risk_decision.lifecycle_action in {"exit", "partial_tp"}:
                executed_target = 0.0 if risk_decision.lifecycle_action == "exit" else float(np.sign(current_position) * max(0.0, abs(current_position) * 0.5))
            else:
                executed_target = float(target)
            executed_target = float(np.clip(executed_target, -self.max_position_abs, self.max_position_abs))
            delta = float(executed_target - current_position)
            spread_bps = float(row.get("spread_bps", 0.0) or 0.0)
            slippage_bps = float(getattr(self.risk_config, "slippage_bps", 0.0) or 0.0)
            cost_bps = spread_bps + slippage_bps + (0.25 if abs(delta) > 1e-9 else 0.0)
            pnl_move = float(row.get("ret_1", 0.0) or 0.0) * current_position
            realized_pnl_usd = float(row.get("realized_pnl_usd", 0.0) or 0.0)
            unrealized_pnl_usd = float(row.get("unrealized_pnl_usd", 0.0) or 0.0) + pnl_move
            drawdown_pct = float(row.get("drawdown_pct", self._positions[pair].drawdown_pct) or self._positions[pair].drawdown_pct)
            self._positions[pair].position = float(executed_target)
            self._positions[pair].entry_price = float(row.get("mid_close", row.get("price", 0.0)) or 0.0)
            self._positions[pair].realized_pnl_usd += float(realized_pnl_usd)
            self._positions[pair].unrealized_pnl_usd = float(unrealized_pnl_usd)
            self._positions[pair].drawdown_pct = float(drawdown_pct)
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
                metadata={
                    "risk_verdict": risk_decision.verdict,
                    "risk_reason": risk_decision.reason,
                    "turnover": abs(delta),
                    "pair": pair,
                },
            )
            reward = float(reward_breakdown.total)
            pair_rewards[pair] = reward
            risk_decisions[pair] = risk_decision.to_dict()
            reward_breakdowns[pair] = reward_breakdown.to_dict()
            executed_targets[pair] = float(executed_target)
        self._equity += sum(float(v.get("realized_pnl_usd", 0.0) or 0.0) for v in row_snapshot.values())
        self._cursor += 1
        truncated = self._cursor >= len(self._timeline)
        portfolio_after = _portfolio_from_positions(
            positions=self._positions,
            equity=self._equity,
            initial_equity=self.initial_equity,
            pair_universe=self._pair_universe,
            frames=self._frames,
            focus_pair=None,
        )
        terminated = bool(
            self.risk_config.max_drawdown_pct > 0.0
            and portfolio_after.drawdown_pct >= float(self.risk_config.max_drawdown_pct)
            and self.risk_config.allow_lifecycle_overrides
        )
        total_reward = float(sum(pair_rewards.values()))
        next_obs = self._build_observation() if not truncated else None
        event = RLEpisodeEvent(
            step=int(self._cursor),
            ts=str(self._timeline[self._cursor - 1].isoformat()) if self._cursor - 1 < len(self._timeline) else "",
            pair="PORTFOLIO",
            observation=self._last_obs or {},
            action=parsed_action.to_dict(),
            reward=float(total_reward),
            terminated=bool(terminated),
            truncated=bool(truncated),
            next_observation=next_obs,
            info={
                "risk": risk_decisions,
                "risk_by_pair": risk_decisions,
                "reward_breakdown": reward_breakdowns,
                "reward_by_pair": pair_rewards,
                "executed_targets": executed_targets,
                "portfolio_before": portfolio_before.to_dict(),
                "portfolio_after": portfolio_after.to_dict(),
            },
        )
        self._terminated = bool(terminated)
        self._truncated = bool(truncated)
        self._last_obs = next_obs or self._last_obs or self._build_observation()
        info = event.to_dict()["info"]
        info["event"] = event.to_dict()
        return (next_obs or self._last_obs), float(total_reward), bool(terminated), bool(truncated), info
