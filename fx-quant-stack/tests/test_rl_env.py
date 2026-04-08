from __future__ import annotations

import pandas as pd

from fxstack.rl import FxTradingEnv, RLTradeAction, build_episode_from_rows, compute_step_reward, normalize_episode_rows


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts": "2024-01-01T00:00:00Z",
                "pair": "EURUSD",
                "timeframe": "M5",
                "spread_bps": 1.0,
                "freshness_secs": 12.0,
                "vol_20": 0.2,
                "liquidity_score": 0.8,
                "regime_bucket": "trend",
                "session_bucket": "london_open",
                "expected_edge_bps": 12.0,
                "trade_prob": 0.75,
                "ret_1": 0.001,
            },
            {
                "ts": "2024-01-01T00:05:00Z",
                "pair": "EURUSD",
                "timeframe": "M5",
                "spread_bps": 1.1,
                "freshness_secs": 12.0,
                "vol_20": 0.25,
                "liquidity_score": 0.78,
                "regime_bucket": "trend",
                "session_bucket": "london_open",
                "expected_edge_bps": 11.0,
                "trade_prob": 0.80,
                "ret_1": -0.0004,
            },
        ]
    )


def test_rl_action_and_reward_contracts() -> None:
    action = RLTradeAction.from_dict({"target_position": 0.5, "tighten_stop": True})
    assert action.target_position == 0.5
    reward, breakdown = compute_step_reward(
        realized_pnl_usd=5.0,
        unrealized_pnl_usd=2.0,
        cost_bps=1.5,
        drawdown_pct=0.02,
        target_position=0.5,
        filled_position=0.5,
    )
    assert reward == breakdown.total
    assert breakdown.total > 0.0


def test_env_reset_and_step_returns_dict_obs() -> None:
    env = FxTradingEnv(observations=_frame(), pair="EURUSD", timeframe="M5", initial_equity=10_000.0)
    obs, info = env.reset()
    assert "market" in obs and "portfolio" in obs and "policy" in obs and "features" in obs
    assert info["pair"] == "EURUSD"
    next_obs, reward, terminated, truncated, step_info = env.step([0.5])
    assert isinstance(next_obs, dict)
    assert isinstance(reward, float)
    assert terminated is False
    assert truncated is False
    assert "risk" in step_info and "reward_breakdown" in step_info


def test_episode_exports_normalize_rows() -> None:
    env = FxTradingEnv(observations=_frame(), pair="EURUSD", timeframe="M5", initial_equity=10_000.0)
    env.reset()
    rows = []
    for _ in range(2):
        obs, reward, terminated, truncated, info = env.step([0.0])
        rows.append(info["event"])
    df = normalize_episode_rows(rows)
    assert list(df["pair"]) == ["EURUSD", "EURUSD"]
    report = build_episode_from_rows(rows)
    assert report["summary"]["steps"] == 2

