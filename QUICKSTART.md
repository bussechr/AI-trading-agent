# Quick Start Guide

Get started with the AI Trading Agent in 5 minutes!

> **IG MT4 Account:** This system is configured for IG account BXAWM (MT4 Login: 96940)  
> **Important:** See [IG MT4 Setup Guide](docs/IG_MT4_SETUP.md) for complete IG-specific instructions

## Step 1: Install Dependencies

```bash
pip install -r requirements.txt
```

## Step 2: Setup IG MT4 Account (Optional for Demo)

### For IG MT4 Connection:

**Complete setup instructions:** [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)

**Quick version:**
1. Login to MT4 with: Login `96940`, Server `IG-LIVE2` or `IG-DEMO`
2. Install [mql-zmq library](https://github.com/dingmaotu/mql-zmq)
3. Copy `mt4_ea/zmq_bridge.mq4` to MT4's Experts folder
4. Compile and attach to a chart
5. Enable AutoTrading (green button)

**For demo/testing without MT4**, you can skip this step and run the examples.

## Step 3: Run Demo Analysis

Test the decision engine without MT4:

```bash
python examples/demo_analysis.py
```

You should see output like:

```
====================================
AI Trading Agent - Market Analysis Demo
====================================

1. BULLISH SCENARIO
----------------------------------------
Action: BUY
Confidence: 75.00%
Reason: RSI oversold, Bullish MA crossover
...
```

## Step 4: Run Simple Backtest

Test the strategy on historical data:

```bash
python examples/simple_backtest.py
```

This generates sample data and runs the momentum strategy.

## Step 5: Configure for Your Trading

Edit `config/config.yaml`:

```yaml
symbols:
  - EURUSD
  - GBPUSD

max_positions: 3
risk_per_trade: 0.01  # 1% risk per trade
```

## Step 6: Run the Agent

### Option A: Analysis Mode (Safe)

Just analyze markets without trading:

```bash
python main.py --mode analyze
```

### Option B: Live Mode (Requires MT4)

Connect to MT4 and trade:

```bash
python main.py --mode live
```

**⚠️ Warning**: Test on demo account first!

## Using Make Commands

If you have `make` installed:

```bash
make install    # Install dependencies
make demo       # Run demo
make backtest   # Run backtest
make analyze    # Run analysis mode
make run        # Run live mode
make test       # Run tests
```

## Docker Quick Start

```bash
# Build and run
docker-compose up -d

# View logs
docker-compose logs -f

# Stop
docker-compose down
```

## Next Steps

1. **Customize Strategy**: Edit `src/strategies/momentum_strategy.py`
2. **Adjust Risk**: Modify risk parameters in `config/config.yaml`
3. **Add Symbols**: Add more trading pairs to the config
4. **Create Strategy**: Inherit from `BaseStrategy` to create your own
5. **Backtest**: Test your strategy on historical data

## Troubleshooting

### "ModuleNotFoundError"
```bash
pip install -r requirements.txt
```

### "Failed to connect to MT4"
- Ensure MT4 EA is running
- Check ports in config match MT4
- Verify MT4 allows DLL imports

### "No signals generated"
- Check if min_confidence is too high
- Verify market data is available
- Review indicator parameters

## Getting Help

- Check `README.md` for detailed documentation
- Review example scripts in `examples/`
- Run tests: `pytest`
- Check logs: `tail -f trading_agent.log`

## Safety Tips

✅ **DO**:
- Test on demo account first
- Start with small position sizes
- Monitor the agent regularly
- Set proper risk limits
- Keep logs

❌ **DON'T**:
- Run on live account without testing
- Risk more than you can afford to lose
- Leave unmonitored for extended periods
- Ignore error messages
- Skip backtesting

Happy Trading! 🚀
