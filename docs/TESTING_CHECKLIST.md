# Testing Checklist for IG MT4 Trading Agent

Complete testing checklist before going live with IG account 96940.

## 📋 Pre-Flight Checklist

### ✅ MT4 Setup (IG Account 96940)

- [ ] **MT4 Installed** and running
- [ ] **Logged in** with account 96940
- [ ] **Server:** IG-DEMO (for testing) ✓
- [ ] **Connection:** Green bars with ping in bottom-right
- [ ] **ZMQ Library** installed in MQL4 folders
- [ ] **Bridge EA** compiled without errors
- [ ] **EA attached** to a chart (any symbol)
- [ ] **AutoTrading** button is GREEN
- [ ] **EA shows smiley face** 😊 in chart corner
- [ ] **Market Watch** shows all symbols (Right-click → Show All)
- [ ] **DLL imports** enabled (Tools → Options → Expert Advisors)

### ✅ Python Environment

- [ ] **Python 3.8+** installed
- [ ] **Dependencies installed**: `pip install -r requirements.txt`
- [ ] **Config file** exists: `config/config.yaml`
- [ ] **Environment file** created: `.env` (from `.env.example`)

### ✅ Configuration

- [ ] **config.yaml** has correct symbols (matching Market Watch)
- [ ] **Risk settings** are conservative (1% max per trade)
- [ ] **Max positions** limited (3-5 max)
- [ ] **Stop loss** enabled (1-2%)
- [ ] **Take profit** set appropriately (2-3%)

## 🧪 Unit Tests

Run the test suite:

```bash
# Run all tests
make test

# Or with pytest directly
pytest -v
```

**Expected results:**
- [ ] All connector tests pass
- [ ] All decision engine tests pass
- [ ] All strategy tests pass
- [ ] No critical errors or warnings

## 🔌 Connection Tests

### Test 1: ZMQ Connection

```bash
python -c "
from src.mt4_bridge.connector import MT4Connector
import logging
logging.basicConfig(level=logging.INFO)
c = MT4Connector()
if c.connect():
    print('✓ Connection successful')
    c.disconnect()
else:
    print('✗ Connection failed')
"
```

**Expected:**
- [ ] "Connected to MT4" message
- [ ] No timeout errors
- [ ] Clean disconnect

### Test 2: Account Information

```bash
python -c "
from src.mt4_bridge.connector import MT4Connector
c = MT4Connector()
if c.connect():
    info = c.get_account_info()
    print(f'Account: {info}')
    print(f'Balance: {c.get_balance()}')
    print(f'Equity: {c.get_equity()}')
    c.disconnect()
"
```

**Expected:**
- [ ] Correct account number (96940)
- [ ] Balance shows demo amount
- [ ] Equity equals balance (no open positions)
- [ ] All values are reasonable numbers

### Test 3: Market Data Retrieval

```bash
python -c "
from src.mt4_bridge.connector import MT4Connector
c = MT4Connector()
if c.connect():
    price = c.get_current_price('EURUSD')
    print(f'EURUSD: {price}')
    data = c.get_market_data('EURUSD', 'H1', 10)
    print(f'Bars retrieved: {len(data.get(\"close\", []))}')
    c.disconnect()
"
```

**Expected:**
- [ ] Valid bid/ask prices for EURUSD
- [ ] Spread is reasonable (< 5 pips)
- [ ] Retrieved 10 bars of data
- [ ] Data has OHLC values

## 📊 Demo Analysis Tests

### Test 4: Run Demo Analysis

```bash
make demo
# Or: python examples/demo_analysis.py
```

**Expected:**
- [ ] Shows bullish/bearish/neutral scenarios
- [ ] Confidence scores between 0-100%
- [ ] Stop loss and take profit calculated
- [ ] Signal breakdown displayed
- [ ] No errors or exceptions

### Test 5: Run Backtest

```bash
make backtest
# Or: python examples/simple_backtest.py
```

**Expected:**
- [ ] Generates sample data
- [ ] Runs strategy analysis
- [ ] Shows BUY/SELL/HOLD signals
- [ ] Summary statistics displayed
- [ ] Reasonable signal distribution

### Test 6: Analysis Mode (Read-Only)

```bash
make analyze
# Or: python main.py --mode analyze
```

**Expected:**
- [ ] Connects to MT4
- [ ] Retrieves account info
- [ ] Analyzes configured symbols
- [ ] Shows trading decisions
- [ ] Does NOT place any trades
- [ ] Displays portfolio status
- [ ] Clean exit

## 🎯 Paper Trading Tests (Demo Account)

### Test 7: Open Test Position

Create a test script `test_order.py`:

```python
from src.mt4_bridge.connector import MT4Connector, OrderType
from src.mt4_bridge.order_manager import OrderManager

connector = MT4Connector()
if connector.connect():
    manager = OrderManager(connector)
    
    # Open a tiny position
    ticket = manager.open_market_order(
        symbol="EURUSD",
        order_type=OrderType.BUY,
        volume=0.01,  # Minimum lot size
        stop_loss=None,
        take_profit=None,
        comment="Test order"
    )
    
    print(f"Order ticket: {ticket}")
    
    if ticket:
        # Immediately close it
        success = manager.close_position(ticket)
        print(f"Closed: {success}")
    
    connector.disconnect()
```

Run: `python test_order.py`

**Expected:**
- [ ] Order opens successfully
- [ ] Returns valid ticket number
- [ ] Shows in MT4 Terminal → Trade tab
- [ ] Closes successfully
- [ ] Shows in MT4 Terminal → History tab
- [ ] No errors in EA log

### Test 8: Risk Management

```python
from src.mt4_bridge.connector import MT4Connector, OrderType
from src.mt4_bridge.order_manager import OrderManager

connector = MT4Connector()
if connector.connect():
    manager = OrderManager(
        connector,
        max_positions=2,
        max_risk_per_trade=0.02
    )
    
    # Try to open 3 positions (should fail on 3rd)
    tickets = []
    for i in range(3):
        ticket = manager.open_market_order(
            symbol="EURUSD",
            order_type=OrderType.BUY,
            volume=0.01,
            comment=f"Risk test {i+1}"
        )
        tickets.append(ticket)
        print(f"Position {i+1}: {ticket}")
    
    # Clean up
    manager.close_all_positions()
    connector.disconnect()
```

**Expected:**
- [ ] First 2 positions open
- [ ] 3rd position rejected (max positions reached)
- [ ] Warning logged about position limit
- [ ] All positions close successfully

## 🤖 Live Agent Tests (Demo Account)

### Test 9: Run Agent for 5 Minutes

```bash
# Set a short interval for testing
python main.py --mode live --interval 30
# Let it run for ~5 minutes, then Ctrl+C
```

**Monitor and verify:**
- [ ] Agent starts successfully
- [ ] Connects to MT4
- [ ] Analyzes symbols each cycle (30 sec)
- [ ] Logs decisions (BUY/SELL/HOLD)
- [ ] Respects max positions limit
- [ ] Respects risk limits
- [ ] No crashes or exceptions
- [ ] Graceful shutdown on Ctrl+C

### Test 10: Position Management

While agent is running (from Test 9):

**Check:**
- [ ] Positions appear in MT4 Terminal
- [ ] Stop loss is set correctly
- [ ] Take profit is set correctly
- [ ] Position size respects risk rules
- [ ] Can manually close position (agent detects it)
- [ ] Agent doesn't re-open immediately after manual close

## 🔒 Safety Checks

### Test 11: Account Protection

Verify these safety measures work:

```python
from src.agent.trading_agent import TradingAgent

config = {
    'mt4_host': 'localhost',
    'symbols': ['EURUSD'],
    'max_positions': 1,
    'max_risk_per_trade': 0.05,  # 5% max
    'risk_per_trade': 0.10,       # Try 10% (should be capped)
}

agent = TradingAgent(config)
# Verify that actual risk used is 5%, not 10%
```

**Expected:**
- [ ] Risk capped at max_risk_per_trade
- [ ] Cannot exceed position limits
- [ ] Invalid configs are caught

### Test 12: Market Closed Handling

Test on weekend or outside trading hours:

```bash
python main.py --mode analyze
```

**Expected:**
- [ ] Detects market is closed
- [ ] Doesn't attempt to trade
- [ ] Handles "no price" errors gracefully
- [ ] Logs appropriate messages

## 📈 Performance Tests

### Test 13: Decision Quality

Run backtest and analyze:

```bash
python examples/simple_backtest.py > backtest_results.txt
```

**Review:**
- [ ] Signal ratio is reasonable (not 100% BUY or 100% HOLD)
- [ ] Confidence scores vary appropriately
- [ ] Signals make sense for market conditions
- [ ] No obvious bugs in logic

### Test 14: Speed Test

```python
import time
from src.agent.trading_agent import TradingAgent

config = {'mt4_host': 'localhost', 'symbols': ['EURUSD', 'GBPUSD']}
agent = TradingAgent(config)

if agent.start():
    start = time.time()
    agent.execute_trading_cycle()
    elapsed = time.time() - start
    print(f"Cycle time: {elapsed:.2f}s")
    agent.stop()
```

**Expected:**
- [ ] One cycle completes in < 10 seconds
- [ ] No significant delays
- [ ] Responsive to interrupts

## 🚨 Failure Mode Tests

### Test 15: Disconnect Handling

1. Start agent: `python main.py --mode live`
2. Stop MT4 while agent is running
3. Observe behavior
4. Restart MT4
5. Check if agent recovers

**Expected:**
- [ ] Detects disconnect
- [ ] Logs error messages
- [ ] Doesn't crash
- [ ] Attempts to reconnect (if implemented)
- [ ] Fails gracefully

### Test 16: Invalid Data Handling

```python
from src.agent.decision_engine import DecisionEngine

config = {'min_confidence': 0.6}
engine = DecisionEngine(config)

# Test with empty data
result1 = engine.make_decision({})
print(f"Empty data: {result1['action']}")  # Should be HOLD

# Test with partial data
result2 = engine.make_decision({'indicators': {}})
print(f"Partial data: {result2['action']}")  # Should be HOLD
```

**Expected:**
- [ ] Handles empty data gracefully
- [ ] Returns HOLD for invalid data
- [ ] No crashes or exceptions
- [ ] Appropriate error messages logged

## ✅ Final Checklist Before Live

- [ ] All unit tests pass ✓
- [ ] All connection tests pass ✓
- [ ] All demo tests pass ✓
- [ ] Agent runs stable for 24+ hours on demo ✓
- [ ] No unexpected errors in logs ✓
- [ ] Risk management tested and working ✓
- [ ] Position limits enforced ✓
- [ ] Account protection verified ✓
- [ ] Failure modes handled gracefully ✓
- [ ] Performance is acceptable ✓
- [ ] Reviewed and understand all code ✓
- [ ] Comfortable with strategy logic ✓
- [ ] Monitoring plan in place ✓
- [ ] Stop-loss plan defined ✓
- [ ] Ready to switch to IG-LIVE2 server ✓

## 🎯 Go Live Procedure

Only after ALL tests pass:

1. **Switch to Live Server**
   - In MT4: Login with server **IG-LIVE2**
   - Use LIVE trading password (not demo)
   - Verify account balance is correct

2. **Conservative Settings**
   ```yaml
   max_positions: 1          # Start with just 1
   risk_per_trade: 0.005     # Only 0.5% risk
   symbols: ['EURUSD']       # Just one symbol
   ```

3. **Monitor Closely**
   - Watch for first 24 hours continuously
   - Check every trade manually
   - Verify P&L matches expectations

4. **Gradual Increase**
   - After 1 week stable → increase to 1% risk
   - After 2 weeks stable → add 2nd symbol
   - After 1 month stable → increase to 3 positions

## 🆘 Emergency Stop

If anything goes wrong:

1. **Immediate**: Stop the Python agent (Ctrl+C)
2. **MT4**: Remove EA from chart
3. **Manual**: Close all positions in MT4 Terminal
4. **Review**: Check logs to understand what happened
5. **Fix**: Address issues before restarting

---

**Remember: Never skip testing! Your capital depends on it.** 🛡️
