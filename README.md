# AI Trading Agent with MT4 Bridge

An AI-powered hedge fund trading system with MetaTrader 4 (MT4) integration using ZeroMQ for real-time communication.

> **Configured for IG Markets**  
> Account: BXAWM | MT4 Login: 96940 | Server: IG-LIVE2/IG-DEMO  
> See [IG MT4 Setup Guide](docs/IG_MT4_SETUP.md) for detailed instructions

## 🚀 Features

- **MT4 Bridge**: Real-time connection to MetaTrader 4 via ZeroMQ
- **AI Decision Engine**: Intelligent trading decisions based on technical analysis
- **Risk Management**: Built-in position sizing and risk controls
- **Multiple Strategies**: Extensible strategy framework (Momentum, Mean Reversion, etc.)
- **Real-time Analysis**: Live market data processing and signal generation
- **Order Management**: Automated order execution and position tracking
- **Backtesting**: Historical data testing capabilities
- **Monitoring**: Comprehensive logging and performance tracking

## 📋 Prerequisites

- Python 3.8 or higher
- MetaTrader 4 with ZeroMQ library
- ZeroMQ library for MQL4 ([mql-zmq](https://github.com/dingmaotu/mql-zmq))

## 🛠️ Installation

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/trading-agent.git
cd trading-agent
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

Or use the Makefile:

```bash
make install
```

### 3. Setup MT4 Bridge (IG Account)

**For complete IG MT4 setup, see: [docs/IG_MT4_SETUP.md](docs/IG_MT4_SETUP.md)**

Quick steps:
1. Login to MT4 with account **96940** on server **IG-LIVE2** (or IG-DEMO)
2. Install ZeroMQ library for MT4 (download from [mql-zmq](https://github.com/dingmaotu/mql-zmq))
3. Copy `mt4_ea/zmq_bridge.mq4` to your MT4 `Experts` folder
4. Compile the EA in MetaEditor
5. Attach the EA to a chart in MT4
6. Enable **AutoTrading** (must be green)
7. Ensure all symbols are visible: Market Watch → Right-click → Show All

### 4. Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your settings:

```env
MT4_HOST=localhost
MT4_REQ_PORT=5555
MT4_PULL_PORT=5556
```

Edit `config/config.yaml` for trading parameters:

```yaml
symbols:
  - EURUSD
  - GBPUSD
  
max_positions: 5
risk_per_trade: 0.01
```

## 🚀 Usage

### Run in Live Mode

```bash
python main.py --config config/config.yaml --mode live
```

Or using Make:

```bash
make run
```

### Run Analysis Only

```bash
python main.py --mode analyze
```

Or:

```bash
make analyze
```

### Run Demo

```bash
python examples/demo_analysis.py
```

Or:

```bash
make demo
```

### Run Backtest

```bash
python examples/simple_backtest.py
```

Or:

```bash
make backtest
```

## 🏗️ Project Structure

```
trading-agent/
├── src/
│   ├── agent/              # AI trading agent core
│   │   ├── trading_agent.py
│   │   └── decision_engine.py
│   ├── mt4_bridge/         # MT4 connectivity
│   │   ├── connector.py
│   │   └── order_manager.py
│   └── strategies/         # Trading strategies
│       ├── base_strategy.py
│       └── momentum_strategy.py
├── config/                 # Configuration files
│   └── config.yaml
├── tests/                  # Unit tests
├── examples/               # Example scripts
├── mt4_ea/                # MT4 Expert Advisor
│   └── zmq_bridge.mq4
├── data/                  # Data directory
├── main.py                # Main entry point
└── requirements.txt       # Python dependencies
```

## 🧪 Testing

Run tests:

```bash
pytest
```

Or:

```bash
make test
```

Run linting:

```bash
make lint
```

Format code:

```bash
make format
```

## 🐳 Docker Support

Build and run with Docker:

```bash
docker-compose up -d
```

View logs:

```bash
docker-compose logs -f trading-agent
```

Stop:

```bash
docker-compose down
```

## 📊 Components

### MT4 Connector

Handles communication with MetaTrader 4:
- Account information retrieval
- Market data fetching
- Order execution
- Position management

### Decision Engine

AI-powered decision making:
- Technical indicator analysis (RSI, Moving Averages, etc.)
- Signal generation
- Confidence scoring
- Risk/reward calculation

### Order Manager

Manages order lifecycle:
- Position sizing based on risk
- Stop loss and take profit calculation
- Multi-position tracking
- Exposure management

### Trading Strategies

Extensible strategy framework:
- Base strategy class
- Momentum strategy
- Custom strategy support

## ⚙️ Configuration

Key configuration parameters in `config/config.yaml`:

```yaml
# Trading symbols
symbols: [EURUSD, GBPUSD, USDJPY]

# Risk management
max_positions: 5
max_risk_per_trade: 0.02
stop_loss_pct: 0.01
take_profit_pct: 0.02

# Strategy parameters
rsi_oversold: 30
rsi_overbought: 70
fast_ma_period: 20
slow_ma_period: 50
```

## 📈 Performance Monitoring

Logs are written to:
- Console output
- `trading_agent.log` file

Monitor performance:
```bash
tail -f trading_agent.log
```

## ⚠️ Risk Warning

**Trading involves substantial risk of loss. This software is for educational purposes only. Always test thoroughly on a demo account before using real money.**

## 🤝 Contributing

Contributions are welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests
5. Submit a pull request

## 📝 License

MIT License - See LICENSE file for details

## 📧 Support

For issues and questions:
- Open an issue on GitHub
- Check the documentation
- Review example scripts

## 🔄 Changelog

### Version 0.1.0
- Initial release
- MT4 bridge implementation
- Basic AI decision engine
- Momentum strategy
- Risk management system

## 🎯 Roadmap

- [ ] Advanced ML models (LSTM, Transformers)
- [ ] Multiple broker support
- [ ] Web dashboard
- [ ] Telegram notifications
- [ ] Portfolio optimization
- [ ] Multi-timeframe analysis
- [ ] News sentiment analysis
- [ ] Advanced backtesting engine

## 🙏 Acknowledgments

- ZeroMQ for MT4: [mql-zmq](https://github.com/dingmaotu/mql-zmq)
- MetaTrader 4 by MetaQuotes
- Python trading community

---

**Remember**: Past performance does not guarantee future results. Always trade responsibly.
