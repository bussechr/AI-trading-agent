# IG MT4 Setup Guide

Complete guide for connecting the AI Trading Agent to your IG CFD MT4 account.

## 📋 Your IG MT4 Account Details

- **IG Account Ref:** BXAWM *(for IG portal use)*
- **MT4 Login (Account Number):** 96940
- **MT4 Server (Live):** IG-LIVE2 or `mt4a2.ig.com:443`
- **MT4 Server (Demo):** IG-DEMO or `demo-mt4.ig.com:443`
- **MT4 Password:** *(You need to get/reset this)*

## 🔐 Step 1: Get Your MT4 Password

Your MT4 password is **different** from your My IG portal password!

### Option A: Find Your Original Password
Check the email IG sent when your MT4 account (96940) was created. Subject likely contains "MetaTrader 4 Account Details".

### Option B: Reset Your MT4 Password
1. Log into **My IG** portal
2. Go to **Settings → MT4**
3. Find account **96940** (BXAWM)
4. Click **Reset Password** or **Change Password**
5. You'll receive a new password via email

### Option C: Set Investor (Read-Only) Password
If you only want read-only access for analysis:
1. In MT4: **Tools → Options → Server**
2. Click **Change** next to "Investor (read-only mode)"
3. Set a custom investor password
4. Use this for analytics without trading capability

## 🖥️ Step 2: Login to MT4

### Install MT4
If you haven't already:
1. Download from [IG's MT4 page](https://www.ig.com/uk/trading-platforms/metatrader-4)
2. Install and launch MT4

### Login to Your Account

1. **File → Login to Trade Account** (or press `Ctrl+U`)

2. Enter your credentials:
   ```
   Login: 96940
   Password: [Your MT4 password from Step 1]
   Server: IG-LIVE2
   ```
   
   **For Demo Account:**
   ```
   Login: 96940
   Password: [Your MT4 demo password]
   Server: IG-DEMO
   ```

3. Click **Login**

### Verify Connection

✅ **Bottom-right corner of MT4:**
- Green bars with ping time (e.g., "IG-LIVE2 15ms")
- Shows your account balance

✅ **Navigator panel (Ctrl+N):**
- Under "Accounts" → see "96940" under IG-LIVE2

❌ **Common Issues:**

| Error | Solution |
|-------|----------|
| "Invalid account" | Wrong server selected - use IG-LIVE2 or IG-DEMO |
| "Authorization failed" | Wrong password - use MT4 password, not My IG password |
| Server not in list | File → Open an Account to refresh broker list |
| "No connection" | Check internet connection, try `mt4a2.ig.com:443` |

## 🔧 Step 3: Install ZMQ Library for MT4

The trading agent communicates with MT4 via ZeroMQ.

### Install MQL-ZMQ Library

1. **Download:** [mql-zmq library](https://github.com/dingmaotu/mql-zmq)
   
2. **Extract files to MT4 data folder:**
   - In MT4: **File → Open Data Folder**
   - Copy library files to:
     - `MQL4/Include/Zmq/` (header files)
     - `MQL4/Libraries/` (DLL files for both 32/64 bit)

3. **Enable DLL imports:**
   - **Tools → Options → Expert Advisors**
   - ✅ Check **"Allow DLL imports"**
   - ✅ Check **"Allow automated trading"**

## 📁 Step 4: Install the ZMQ Bridge EA

1. **Copy the EA:**
   - From: `trading-agent/mt4_ea/zmq_bridge.mq4`
   - To: MT4's `MQL4/Experts/` folder
   - (File → Open Data Folder → MQL4 → Experts)

2. **Compile the EA:**
   - In MT4: **Tools → MetaQuotes Language Editor** (or press F4)
   - Open `zmq_bridge.mq4`
   - Click **Compile** (F7)
   - Check for errors in "Errors" tab

3. **Attach to a chart:**
   - In MT4, open any chart (e.g., EURUSD)
   - In **Navigator → Expert Advisors**
   - Drag **zmq_bridge** onto the chart
   
4. **Configure EA settings:**
   ```
   REP_PORT: 5555
   PUSH_PORT: 5556
   TICK_INTERVAL: 1000
   ```
   
5. **Enable AutoTrading:**
   - Click **AutoTrading** button in toolbar (or F7)
   - Button should be GREEN
   - Check EA shows a smiley face 😊 in top-right corner

## 🎯 Step 5: Show All Trading Symbols

IG MT4 may not show all symbols by default.

1. **Open Market Watch:** View → Market Watch (or `Ctrl+M`)

2. **Right-click** in Market Watch → **Show All**

3. **Verify symbols** you want to trade are visible:
   - EURUSD
   - GBPUSD
   - USDJPY
   - etc.

## 🚀 Step 6: Test the Connection

### From Python Side:

```bash
# Test the connection
python -c "from src.mt4_bridge.connector import MT4Connector; import logging; logging.basicConfig(level=logging.INFO); c = MT4Connector(); print('Connected!' if c.connect() else 'Failed'); c.disconnect()"
```

### Expected Output:
```
Connected to MT4 at localhost:5555
Connected!
Disconnected from MT4
```

### Run Demo Analysis:
```bash
python examples/demo_analysis.py
```

### Run Live Analysis (reads from MT4):
```bash
python main.py --mode analyze
```

## ⚙️ Step 7: Configure Trading Parameters

Edit `config/config.yaml`:

```yaml
# Symbols to trade (make sure these are in Market Watch)
symbols:
  - EURUSD
  - GBPUSD
  - USDJPY
  - AUDUSD

# Risk management for IG account
max_positions: 3          # Max simultaneous trades
risk_per_trade: 0.01      # Risk 1% per trade
stop_loss_pct: 0.01       # 1% stop loss
take_profit_pct: 0.02     # 2% take profit
```

## 🛡️ Step 8: Safety Checks Before Live Trading

### ⚠️ CRITICAL - Test on Demo First!

1. **Use IG-DEMO server** initially
2. **Test with minimum lot sizes** (0.01 lots)
3. **Monitor for at least 1 week** on demo
4. **Verify all features work:**
   - ✅ Order execution
   - ✅ Stop loss/take profit setting
   - ✅ Position closing
   - ✅ Risk management

### Safety Settings in MT4:

1. **Set Maximum Deviation:**
   - Tools → Options → Trade
   - Set max price deviation (e.g., 5 pips)

2. **Enable Trade Context Busy:**
   - Prevents simultaneous conflicting orders

3. **Check Margin Requirements:**
   - Ensure sufficient margin for positions

### EA Safety Settings:

In `zmq_bridge.mq4`, you can add:
- Maximum daily loss limit
- Maximum trades per day
- Trading time restrictions
- Symbol filters

## 📊 Step 9: Monitor Your Trading

### MT4 Monitoring:
- **Terminal (Ctrl+T):** View open positions, history, logs
- **Experts tab:** See EA log messages
- **Journal tab:** Connection and system logs

### Python Monitoring:
```bash
# View live logs
tail -f trading_agent.log

# Or with live mode
python main.py --mode live
```

### IG Portal Monitoring:
- Check positions in My IG portal
- Verify they match what the agent reports
- Monitor account balance and P&L

## 🔍 Troubleshooting

### EA Not Connecting

**Check EA logs in MT4:**
- Terminal → Experts tab
- Should see: "ZMQ Bridge initialized successfully"

**If you see errors:**
```
"Failed to bind REP socket"
→ Port 5555 already in use, close other MT4 instances

"Library not found"
→ ZMQ library not installed properly

"DLL calls not allowed"
→ Enable DLL imports in Tools → Options → Expert Advisors
```

### Python Can't Connect

**Error: "Failed to connect to MT4"**

1. Check MT4 is running with EA attached
2. Verify ports match (5555, 5556)
3. Check firewall isn't blocking
4. Ensure EA shows smiley face (active)

**Test manually:**
```python
import zmq
context = zmq.Context()
socket = context.socket(zmq.REQ)
socket.connect("tcp://localhost:5555")
print("Connected!")
```

### Symbols Not Trading

1. Check symbol is in Market Watch (Show All)
2. Verify symbol name matches exactly (case-sensitive)
3. Check trading hours (market must be open)
4. Ensure sufficient margin available

### Orders Not Executing

1. **AutoTrading must be enabled** (green button)
2. Check EA settings allow trading
3. Verify account has trading permissions (not investor password)
4. Check IG account is approved for automated trading

## 📝 Quick Reference

### IG MT4 Connection Info
```
Account Ref: BXAWM
MT4 Login: 96940
Live Server: IG-LIVE2 (mt4a2.ig.com:443)
Demo Server: IG-DEMO (demo-mt4.ig.com:443)
```

### ZMQ Configuration
```
REP Port: 5555 (command/response)
PUSH Port: 5556 (market data)
Host: localhost
```

### Important Commands
```bash
# Install dependencies
make install

# Test connection
python examples/demo_analysis.py

# Run analysis mode (safe)
python main.py --mode analyze

# Run live trading
python main.py --mode live
```

## 🆘 Getting Help

1. **Check logs:** `trading_agent.log` and MT4 Experts tab
2. **Test connection:** Run demo scripts first
3. **IG Support:** For MT4 account/password issues
4. **GitHub Issues:** For code/EA issues

## ⚡ Quick Start Checklist

- [ ] MT4 password obtained/reset
- [ ] Logged into MT4 with account 96940
- [ ] Green connection showing in MT4
- [ ] ZMQ library installed in MT4
- [ ] ZMQ bridge EA compiled successfully
- [ ] EA attached to chart with smiley face
- [ ] AutoTrading enabled (green button)
- [ ] All symbols showing in Market Watch
- [ ] Python dependencies installed
- [ ] Connection test successful
- [ ] Demo analysis running
- [ ] Tested on DEMO server first
- [ ] Risk parameters configured
- [ ] Ready for live trading!

---

**Remember:** Always test thoroughly on the IG-DEMO server before switching to IG-LIVE2! 🎯
