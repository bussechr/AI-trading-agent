
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch

def test_reversal_exit_logic(agent):
    """
    Test that the agent exits a position when the signal reverses strongly.
    Scenario:
    - Held SHORT on EURUSD.
    - Market pumps (Price moves up).
    - Score flips from Negative (Sell) to Positive (Buy).
    - Expect: close_position() called.
    """
    # 1. Setup Mock Market Data (Bullish Pump)
    # create a strong uptrend
    prices = [1.1000 * (1 + 0.0001 * i) for i in range(100)] 
    df = pd.DataFrame({"close": prices})
    df.attrs["spread"] = 0.5
    
    md = {"EURUSD": df}
    
    # 2. Setup Held Position (SHORT)
    # We are short from 1.1000. Current price is ~1.1100. loss.
    # But more importantly, the SIGNAL should be BUY now.
    positions = [{
        "symbol": "EURUSD",
        "type": 1, # SELL
        "open_price": 1.1000,
        "open_time": 1234567890
    }]
    
    # 3. Mock Bridge Client
    with patch("src.agents.fx_el_hawkes_agent.bridge_client.close_position") as mock_close:
        # Inject positions into agent's view via argument or mock
        # The agent._manage_exits takes positions list
        
        # We need to force a high score first.
        # Let's inspect what score we get
        score, _ = agent.score_symbol(df, "EURUSD")
        print(f"DEBUG: Score for Pump = {score}")
        
        # Only run if our synthetic data actually generates a buy signal
        # If not, we force a buy signal by mocking score_symbol temporarily?
        # Better to trust the data, but let's see. 
        # If score < agent.score_th, it won't trigger reversal.
        
        # FORCE SCORE for the test to ensure we test the LOGIC, not the math.
        agent.score_symbol = MagicMock(return_value=(0.50, {"vol": 0.01, "p_trend": 0.8}))
        
        # 4. Run Manage Exits
        agent._manage_exits(positions, md)
        
        # 5. Assert Close
        mock_close.assert_called_with("EURUSD", magic=246810)


def test_reversal_exit_uses_regime_threshold_not_base_score_threshold(agent):
    """
    Regression:
    Reversal exit should use regime-aware thresholding, not raw self.score_th.
    """
    # Make base score threshold intentionally strict.
    agent.score_th = 0.50
    agent.base_score_th = 0.50
    agent.regime_score_mult_trend = 0.20  # trend reversal threshold => 0.10
    agent.use_score_distribution_adaptation = False

    prices = [1.1000 + (0.0001 * i) for i in range(300)]
    df = pd.DataFrame({"close": prices})
    df.attrs["spread"] = 0.5
    md = {"EURUSD": df}
    positions = [
        {
            "symbol": "EURUSD",
            "type": 1,  # SELL
            "open_price": 1.1000,
            "open_time": 1234567890,
        }
    ]

    # Ensure risk manager does not close first; we want to test reversal threshold path.
    agent.risk_manager.check_exit = MagicMock(return_value=(False, ""))
    agent.score_symbol = MagicMock(
        return_value=(
            0.20,  # below base 0.50 but above regime-aware 0.10
            {
                "vol": 0.01,
                "p_trend": 0.80,
                "direction_samples": 0,
                "direction_buy_samples": 0,
                "direction_sell_samples": 0,
                "direction_buy_hit_rate": 0.5,
                "direction_sell_hit_rate": 0.5,
            },
        )
    )

    with patch("src.agents.fx_el_hawkes_agent.bridge_client.close_position") as mock_close:
        agent._manage_exits(positions, md)
        mock_close.assert_called_with("EURUSD", magic=246810)


def test_manage_exits_applies_horizon_hold_overrides(agent):
    """Exit manager should pass horizon-derived hold overrides into RiskManager."""
    prices = [1.1000 + (0.00005 * i) for i in range(300)]
    df = pd.DataFrame({"close": prices})
    md = {"EURUSD": df}
    positions = [
        {
            "symbol": "EURUSD",
            "type": 0,  # BUY
            "open_price": 1.1000,
            "open_time": 1234567890,
        }
    ]

    base_min_hold = float(agent.risk_manager.min_hold_secs)
    base_time_limit = float(agent.risk_manager.time_limit_hours)
    base_stagnation = float(agent.risk_manager.stagnation_minutes)
    agent.score_symbol = MagicMock(
        return_value=(
            0.30,
            {
                "vol": 0.01,
                "p_trend": 0.90,
                "direction_samples": 0,
                "direction_buy_samples": 0,
                "direction_sell_samples": 0,
                "direction_buy_hit_rate": 0.5,
                "direction_sell_hit_rate": 0.5,
                "primary_horizon_hours": 24.0,
                "horizon_confidence": 0.90,
                "horizon_strength": 1.10,
                "horizon_side": "BUY",
            },
        )
    )
    agent.risk_manager.check_exit = MagicMock(return_value=(False, ""))

    with patch("src.agents.fx_el_hawkes_agent.bridge_client.close_position"):
        agent._manage_exits(positions, md)

    assert agent.risk_manager.check_exit.called
    _, kwargs = agent.risk_manager.check_exit.call_args
    assert float(kwargs.get("min_hold_secs_override", 0.0)) >= base_min_hold
    assert float(kwargs.get("time_limit_hours_override", 0.0)) >= base_time_limit
    assert float(kwargs.get("stagnation_minutes_override", 0.0)) >= base_stagnation


def test_soft_reversal_exit_requires_persistence_and_loss_threshold(agent):
    prices = [1.1000 + (0.0001 * i) for i in range(300)]
    df = pd.DataFrame({"close": prices})
    md = {"EURUSD": df}
    positions = [
        {
            "symbol": "EURUSD",
            "type": 1,  # SELL
            "open_price": 1.1000,
            "open_time": 1.0,
            "profit": -2.0,
            "magic": 246810,
        }
    ]

    agent.entry_gate_mode = "soft"
    agent.soft_reversal_exit_enabled = True
    agent.soft_reversal_exit_min_hold_hours = 0.0
    agent.soft_reversal_exit_score_ratio = 0.03
    agent.soft_reversal_exit_min_aligned_sharpe = 0.20
    agent.soft_reversal_exit_persistence_cycles = 3
    agent.soft_reversal_exit_loss_threshold = -0.50
    agent.risk_manager.check_exit = MagicMock(return_value=(False, ""))
    agent.score_symbol = MagicMock(
        return_value=(
            0.01,  # opposite to held SELL but below hard reversal threshold
            {
                "vol": 0.01,
                "p_trend": 0.80,
                "predictive_sharpe": 0.50,
                "horizon_side": "BUY",
                "horizon_confidence": 0.90,
                "direction_samples": 0,
                "direction_buy_samples": 0,
                "direction_sell_samples": 0,
                "direction_buy_hit_rate": 0.5,
                "direction_sell_hit_rate": 0.5,
            },
        )
    )

    with patch("src.agents.fx_el_hawkes_agent.bridge_client.close_position") as mock_close:
        agent._manage_exits(positions, md)
        agent._manage_exits(positions, md)
        assert mock_close.call_count == 0
        agent._manage_exits(positions, md)
        assert mock_close.call_count == 1
