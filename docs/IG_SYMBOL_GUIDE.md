# IG MT4 Symbol Guide

Guide for trading symbols available on IG's MT4 platform (Account: 96940).

## 📊 Symbol Naming Convention

IG uses specific symbol naming in MT4. The exact names may vary, so always check Market Watch.

### Common Forex Pairs

| Market | IG MT4 Symbol | Description |
|--------|---------------|-------------|
| EUR/USD | EURUSD | Euro vs US Dollar |
| GBP/USD | GBPUSD | British Pound vs US Dollar |
| USD/JPY | USDJPY | US Dollar vs Japanese Yen |
| AUD/USD | AUDUSD | Australian Dollar vs US Dollar |
| USD/CHF | USDCHF | US Dollar vs Swiss Franc |
| NZD/USD | NZDUSD | New Zealand Dollar vs US Dollar |
| USD/CAD | USDCAD | US Dollar vs Canadian Dollar |
| EUR/GBP | EURGBP | Euro vs British Pound |
| EUR/JPY | EURJPY | Euro vs Japanese Yen |
| GBP/JPY | GBPJPY | British Pound vs Japanese Yen |

### Indices (CFDs)

IG may use different suffixes for indices. Common formats:

| Index | Possible MT4 Symbols | Description |
|-------|---------------------|-------------|
| US 500 | US500, US500.I | S&P 500 |
| Wall Street | US30, WALLSTREET | Dow Jones |
| US Tech 100 | USTECH100, NAS100 | NASDAQ 100 |
| Germany 40 | GER40, DAX | DAX Index |
| UK 100 | UK100, FTSE | FTSE 100 |
| France 40 | FRA40, CAC40 | CAC 40 |
| Australia 200 | AUS200 | ASX 200 |

### Commodities

| Commodity | Possible MT4 Symbols | Description |
|-----------|---------------------|-------------|
| Gold | GOLD, XAUUSD | Gold vs USD |
| Silver | SILVER, XAGUSD | Silver vs USD |
| Oil (US) | OILUSCRUDE, XTIUSD | WTI Crude Oil |
| Oil (Brent) | OILUKOIL, XBRUSD | Brent Crude Oil |
| Natural Gas | NATURALGAS, XNGUSD | Natural Gas |

## 🔍 How to Find Available Symbols

### Method 1: Market Watch in MT4

1. **View → Market Watch** (Ctrl+M)
2. **Right-click → Show All**
3. Browse through all available symbols
4. Note the exact spelling and capitalization

### Method 2: Symbols Window

1. **View → Symbols** (Ctrl+U)
2. Expand categories to see all symbols
3. Check "Show symbol" to add to Market Watch
4. Note symbol properties (contract size, tick value, etc.)

### Method 3: Via Python Script

Use this script to query available symbols:

```python
from src.mt4_bridge.connector import MT4Connector

connector = MT4Connector()
if connector.connect():
    # Request symbol list (you'll need to add this function)
    symbols = connector.get_symbols()
    print("Available symbols:")
    for symbol in symbols:
        print(f"  - {symbol}")
    connector.disconnect()
```

## ⚙️ Configuring Symbols in Trading Agent

Edit `config/config.yaml`:

```yaml
# Use exact symbol names as shown in MT4 Market Watch
symbols:
  # Major Forex Pairs (Most liquid, lowest spreads)
  - EURUSD
  - GBPUSD
  - USDJPY
  - AUDUSD
  
  # Indices (Check exact names in your MT4)
  # - US500
  # - GER40
  # - UK100
  
  # Commodities (Check exact names in your MT4)
  # - GOLD
  # - SILVER
```

## 📏 Symbol Specifications

### Lot Sizes

Different instruments have different minimum lot sizes on IG:

| Instrument Type | Min Lot | Standard Lot Value |
|-----------------|---------|-------------------|
| Major Forex | 0.01 | 1,000 units |
| Minor Forex | 0.01 | 1,000 units |
| Indices | Varies | Points × Contract Size |
| Commodities | Varies | Contract Size |

### Spreads

IG spreads vary by:
- Time of day (tighter during main trading hours)
- Market volatility
- Account type

Check current spreads in Market Watch.

## ⚠️ Important Notes for IG MT4

### 1. Symbol Suffix Variations

IG may add suffixes to some symbols:
- `.i` or `.I` for index CFDs
- `.m` for mini contracts
- Always check your specific MT4 installation

### 2. Trading Hours

Different symbols have different trading hours:

```python
# Add this to your config to specify trading hours
trading_hours:
  EURUSD:
    start: "00:00"
    end: "23:59"
    closed: ["Saturday", "Sunday"]
  
  US500:
    start: "00:00"
    end: "23:00"
    closed: ["Saturday", "Sunday"]
```

### 3. Market Closed Periods

- **Forex:** Typically closes Friday 10pm GMT to Sunday 10pm GMT
- **Indices:** May have daily breaks
- **Commodities:** Specific trading hours vary

### 4. Contract Rollover

CFD contracts may rollover on specific dates:
- Watch for rollover dates in IG communications
- May affect positions held overnight
- EA should handle this gracefully

## 🛠️ Testing Symbols

Before adding a symbol to your config, test it:

### 1. Manual Test in MT4

1. Open a chart for the symbol
2. Try placing a manual order
3. Check the spread and execution
4. Verify stop loss/take profit can be set

### 2. Test via Python

```python
from src.mt4_bridge.connector import MT4Connector

connector = MT4Connector()
if connector.connect():
    # Test getting price
    symbol = "EURUSD"
    price = connector.get_current_price(symbol)
    print(f"{symbol} - Bid: {price['bid']}, Ask: {price['ask']}")
    
    # Test getting market data
    data = connector.get_market_data(symbol, timeframe="H1", bars=10)
    print(f"Retrieved {len(data.get('close', []))} bars")
    
    connector.disconnect()
```

## 📋 Recommended Symbol Configuration

### For Beginners (Low Risk)
```yaml
symbols:
  - EURUSD  # Most liquid pair
  - GBPUSD  # High liquidity, larger movements
```

### For Intermediate (Balanced)
```yaml
symbols:
  - EURUSD
  - GBPUSD
  - USDJPY
  - AUDUSD
```

### For Advanced (Diversified)
```yaml
symbols:
  # Major Forex
  - EURUSD
  - GBPUSD
  - USDJPY
  
  # Commodities
  - GOLD
  
  # Index
  - US500
```

## 🔧 Symbol-Specific Settings

You can configure different settings per symbol:

```yaml
symbol_settings:
  EURUSD:
    max_spread: 3  # pips
    min_price_change: 0.0001
    contract_size: 100000
    
  GOLD:
    max_spread: 50  # points
    min_price_change: 0.01
    contract_size: 100
```

## 📊 Monitoring Symbol Performance

Track which symbols perform best with your strategy:

```python
# Add to your analysis
symbol_performance = {
    'EURUSD': {'trades': 10, 'win_rate': 0.6, 'avg_profit': 50},
    'GBPUSD': {'trades': 8, 'win_rate': 0.5, 'avg_profit': 30},
}
```

## 🆘 Troubleshooting

### "Symbol not found"
- Check exact spelling in Market Watch
- Use **Show All** in Market Watch
- Symbol might not be available on your account

### "Trade is disabled"
- Market might be closed
- Check symbol trading hours
- Ensure AutoTrading is enabled

### "Invalid price"
- Symbol might not be streaming prices
- Check internet connection
- Reconnect to IG server

### "No prices" or "Off quotes"
- Market is closed
- Symbol is suspended
- Check IG platform status

## 📝 Quick Checklist

- [ ] Opened Market Watch (Ctrl+M)
- [ ] Used "Show All" to see all symbols
- [ ] Noted exact symbol spellings
- [ ] Checked contract sizes
- [ ] Verified trading hours
- [ ] Updated config.yaml with correct symbols
- [ ] Tested price retrieval with Python
- [ ] Confirmed symbols work with EA
- [ ] Documented any symbol-specific quirks

---

**Tip:** Start with just EURUSD to test your setup, then gradually add more symbols as you gain confidence! 📈
