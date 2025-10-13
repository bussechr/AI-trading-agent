"""Demo script showing market analysis capabilities."""

import numpy as np
from src.agent.decision_engine import DecisionEngine


def demo_market_analysis():
    """Demonstrate market analysis."""
    
    print("="*60)
    print("AI Trading Agent - Market Analysis Demo")
    print("="*60)
    
    # Configure decision engine
    config = {
        'rsi_oversold': 30,
        'rsi_overbought': 70,
        'min_confidence': 0.6,
        'stop_loss_pct': 0.01,
        'take_profit_pct': 0.02
    }
    
    engine = DecisionEngine(config)
    
    # Example 1: Bullish scenario
    print("\n1. BULLISH SCENARIO")
    print("-" * 40)
    
    bullish_analysis = {
        'symbol': 'EURUSD',
        'indicators': {
            'rsi': 28,  # Oversold
            'sma_20': 1.2150,
            'sma_50': 1.2100,  # Golden cross
            'sma_200': 1.2050,
            'volatility': 0.0008
        },
        'current_price': {'bid': 1.2155, 'ask': 1.2157}
    }
    
    decision = engine.make_decision(bullish_analysis)
    print_decision(decision)
    
    # Example 2: Bearish scenario
    print("\n2. BEARISH SCENARIO")
    print("-" * 40)
    
    bearish_analysis = {
        'symbol': 'GBPUSD',
        'indicators': {
            'rsi': 75,  # Overbought
            'sma_20': 1.3050,
            'sma_50': 1.3100,  # Death cross
            'sma_200': 1.3150,
            'volatility': 0.0012
        },
        'current_price': {'bid': 1.3045, 'ask': 1.3047}
    }
    
    decision = engine.make_decision(bearish_analysis)
    print_decision(decision)
    
    # Example 3: Neutral scenario
    print("\n3. NEUTRAL SCENARIO")
    print("-" * 40)
    
    neutral_analysis = {
        'symbol': 'USDJPY',
        'indicators': {
            'rsi': 52,  # Neutral
            'sma_20': 110.50,
            'sma_50': 110.45,  # Flat
            'sma_200': 110.40,
            'volatility': 0.0005
        },
        'current_price': {'bid': 110.52, 'ask': 110.54}
    }
    
    decision = engine.make_decision(neutral_analysis)
    print_decision(decision)
    
    print("\n" + "="*60)


def print_decision(decision):
    """Print decision in formatted manner."""
    print(f"Action: {decision['action']}")
    print(f"Confidence: {decision['confidence']:.2%}")
    print(f"Reason: {decision['reason']}")
    
    if decision.get('stop_loss'):
        print(f"Stop Loss: {decision['stop_loss']:.5f}")
    if decision.get('take_profit'):
        print(f"Take Profit: {decision['take_profit']:.5f}")
    
    if decision.get('signals'):
        print("\nSignal Breakdown:")
        for signal_name, value in decision['signals'].items():
            print(f"  {signal_name}: {value:+.3f}")


if __name__ == "__main__":
    demo_market_analysis()
