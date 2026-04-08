from __future__ import annotations

import pandas as pd

from fxstack.rl import PortfolioFxTradingEnv, RLPortfolioAction


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
                "ts": "2024-01-01T00:00:00Z",
                "pair": "GBPUSD",
                "timeframe": "M5",
                "spread_bps": 1.3,
                "freshness_secs": 9.0,
                "vol_20": 0.24,
                "liquidity_score": 0.76,
                "regime_bucket": "mean_revert",
                "session_bucket": "london_open",
                "expected_edge_bps": 9.5,
                "trade_prob": 0.61,
                "ret_1": -0.0002,
            },
            {
                "ts": "2024-01-01T00:05:00Z",
                "pair": "EURUSD",
                "timeframe": "M5",
                "spread_bps": 1.1,
                "freshness_secs": 11.0,
                "vol_20": 0.23,
                "liquidity_score": 0.79,
                "regime_bucket": "trend",
                "session_bucket": "london_open",
                "expected_edge_bps": 11.0,
                "trade_prob": 0.80,
                "ret_1": -0.0004,
            },
            {
                "ts": "2024-01-01T00:05:00Z",
                "pair": "GBPUSD",
                "timeframe": "M5",
                "spread_bps": 1.2,
                "freshness_secs": 10.0,
                "vol_20": 0.25,
                "liquidity_score": 0.74,
                "regime_bucket": "mean_revert",
                "session_bucket": "london_open",
                "expected_edge_bps": 8.8,
                "trade_prob": 0.59,
                "ret_1": 0.0003,
            },
        ]
    )


def test_portfolio_env_exposes_joint_observation_and_risk_outputs() -> None:
    env = PortfolioFxTradingEnv(
        observations=_frame(),
        pair_universe=["EURUSD", "GBPUSD"],
        initial_equity=10_000.0,
    )
    obs, info = env.reset()
    assert {"market_by_pair", "features_by_pair", "portfolio", "policy_context", "action_mask"}.issubset(obs)
    assert info["pair_universe"] == ["EURUSD", "GBPUSD"]

    action = RLPortfolioAction.from_dict(
        {
            "portfolio_bias": 0.2,
            "pair_actions": {
                "EURUSD": {"target_position": 0.5, "tighten_stop": True},
                "GBPUSD": {"target_position": -0.25},
            },
        }
    )
    next_obs, reward, terminated, truncated, step_info = env.step(action)
    assert isinstance(next_obs, dict)
    assert isinstance(reward, float)
    assert terminated is False
    assert truncated is False
    assert step_info["event"]["info"]["risk_by_pair"]["EURUSD"]["pair"] == "EURUSD"
    assert set(step_info["event"]["info"]["executed_targets"]) == {"EURUSD", "GBPUSD"}
    assert step_info["event"]["info"]["portfolio_after"]["open_position_count"] >= 0
