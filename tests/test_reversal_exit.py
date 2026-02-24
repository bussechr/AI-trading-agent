
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
    with patch("execution.mt4_bridge_client.close_position") as mock_close:
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

    with patch("execution.mt4_bridge_client.close_position") as mock_close:
        agent._manage_exits(positions, md)
        mock_close.assert_called_with("EURUSD", magic=246810)
