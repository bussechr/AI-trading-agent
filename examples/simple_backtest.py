"""Simple backtest example."""

import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import logging

from src.strategies.momentum_strategy import MomentumStrategy


def generate_sample_data(days=365):
    """
    Generate sample OHLC data for testing.
    
    Args:
        days: Number of days of data
        
    Returns:
        DataFrame with OHLC data
    """
    dates = pd.date_range(end=datetime.now(), periods=days, freq='D')
    
    # Generate random walk price data
    returns = np.random.normal(0.0001, 0.01, days)
    prices = 1.2000 * np.exp(np.cumsum(returns))
    
    data = pd.DataFrame({
        'timestamp': dates,
        'open': prices * (1 + np.random.uniform(-0.001, 0.001, days)),
        'high': prices * (1 + np.random.uniform(0, 0.002, days)),
        'low': prices * (1 + np.random.uniform(-0.002, 0, days)),
        'close': prices
    })
    
    return data


def simple_backtest():
    """Run a simple backtest."""
    logging.basicConfig(level=logging.INFO)
    
    # Generate data
    print("Generating sample data...")
    data = generate_sample_data(days=365)
    
    # Initialize strategy
    config = {
        'rsi_period': 14,
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'fast_ma_period': 20,
        'slow_ma_period': 50,
        'min_confidence': 0.6
    }
    
    strategy = MomentumStrategy(config)
    
    # Run backtest
    print("\nRunning backtest...")
    signals = []
    
    for i in range(50, len(data)):
        # Get historical window
        window = data.iloc[i-50:i]
        
        market_data = {
            'close': window['close'].values.tolist()
        }
        
        # Analyze
        analysis = strategy.analyze(market_data)
        
        if 'error' not in analysis:
            signal = strategy.generate_signal(analysis)
            signals.append({
                'date': data.iloc[i]['timestamp'],
                'price': data.iloc[i]['close'],
                'signal': signal,
                'rsi': analysis.get('rsi'),
                'trend': analysis.get('trend')
            })
    
    # Convert to DataFrame
    signals_df = pd.DataFrame(signals)
    
    # Summary statistics
    print("\n" + "="*60)
    print("BACKTEST RESULTS")
    print("="*60)
    print(f"Total signals analyzed: {len(signals_df)}")
    print(f"BUY signals: {(signals_df['signal'] == 'BUY').sum()}")
    print(f"SELL signals: {(signals_df['signal'] == 'SELL').sum()}")
    print(f"HOLD signals: {(signals_df['signal'] == 'HOLD').sum()}")
    print("\nSample signals:")
    print(signals_df[signals_df['signal'] != 'HOLD'].head(10))
    print("="*60)


if __name__ == "__main__":
    simple_backtest()
