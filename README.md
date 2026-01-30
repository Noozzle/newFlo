# FloTrader

Production-ready algo-trading framework for Bybit with event-driven architecture supporting both backtest and live trading modes.

## Features

- **Event-Driven Architecture**: Single codebase for backtest and live trading
- **Bybit Integration**: REST API + WebSocket using pybit SDK
- **Historical Data**: Reads CSV/Parquet files with auto schema detection
- **Live Data Recording**: Async writer for recording market data
- **Strategy Framework**: Plugin-based strategies with orderflow analysis
- **Risk Management**: Position sizing, daily loss limits, max drawdown
- **Order Management**: SL/TP on exchange, reconciliation on restart
- **Reporting**: Comprehensive metrics, equity curves, trade journals
- **Telegram Notifications**: Entry/exit alerts, errors, daily summaries

## Quick Start

### Installation

```bash
# Clone or create project directory
cd D:\Trading\newFlo

# Create virtual environment
python -m venv .venv
.venv\Scripts\activate  # Windows
# source .venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -e .

# For development
pip install -e ".[dev]"
```

### Configuration

Edit `config.yaml` to customize:

```yaml
mode: backtest  # or "live"

symbols:
  trade: [BTCUSDT]
  record: [ETHUSDT, SOLUSDT]

strategy:
  name: orderflow_1m
  params:
    rr_ratio: 3.0
    imbalance_threshold: 0.6

risk:
  max_position_pct: 2.0
  max_daily_loss_pct: 5.0
```

### Environment Variables

For live trading, set these environment variables:

```bash
# Bybit API (required for live trading)
export BYBIT_API_KEY=your_api_key
export BYBIT_API_SECRET=your_api_secret
export BYBIT_TESTNET=true  # Set to false for mainnet

# Telegram (optional)
export TELEGRAM_TOKEN=your_bot_token
export TELEGRAM_CHAT_ID=your_chat_id
```

## Usage

### Run Backtest

```bash
# Basic backtest with config
python -m app backtest --config config.yaml

# Backtest specific symbol and date range
python -m app backtest -c config.yaml -s BTCUSDT --from 2026-01-26 --to 2026-01-27

# With debug logging
python -m app backtest -c config.yaml --log-level DEBUG
```

### Run Live Trading

**WARNING**: Live trading involves real money. Always test on testnet first!

```bash
# Start live trading (testnet)
python -m app live --config config.yaml

# With debug logging
python -m app live -c config.yaml --log-level DEBUG
```

### Verify Data Loading

```bash
# Test CSV schema auto-detection
python sample_data_loader.py
```

## Data Format

The system auto-detects CSV schema. Supported formats:

### Kline/Candlestick (1m.csv, 15m.csv)
```csv
timestamp,open,high,low,close,volume
2026-01-26 19:27:00.000000,88114.8,88150.1,88110.5,88137.6,15.893
```

### Order Book (orderbook.csv)
```csv
timestamp,bid_price,bid_size,ask_price,ask_size,mid_price
2026-01-26 19:27:54.895932,88143.2,0.174,88143.3,2.454,88143.25
```

### Trades (trades.csv)
```csv
timestamp,price,amount,side
2026-01-26 19:27:56.252000,88143.3,0.001,buy
```

## Directory Structure

```
live_data/
├── BTCUSDT/
│   ├── 1m.csv
│   ├── 15m.csv
│   ├── orderbook.csv
│   └── trades.csv
├── ETHUSDT/
│   └── ...
```

## Backtest Reports

Reports are saved to `reports/<run_id>/`:

```
reports/20260130_120000/
├── config.yaml      # Configuration snapshot
├── trades.csv       # All trades
├── equity_curve.csv # Equity over time
├── metrics.json     # Calculated metrics
└── summary.md       # Human-readable summary
```

### Metrics Included

- Win rate (overall, long, short)
- Risk/Reward ratio
- Profit factor
- Expectancy
- Max drawdown
- Average trade
- Average hold time
- Fees and slippage impact
- Win/loss streaks

## Strategy Development

Create new strategies by inheriting from `BaseStrategy`:

```python
from app.strategies.base import BaseStrategy
from app.core.events import KlineEvent, MarketTradeEvent, OrderBookEvent

class MyStrategy(BaseStrategy):
    async def on_kline(self, event: KlineEvent) -> None:
        # Analyze candlestick data
        pass

    async def on_trade(self, event: MarketTradeEvent) -> None:
        # Analyze trade flow
        pass

    async def on_orderbook(self, event: OrderBookEvent) -> None:
        # Analyze order book and generate signals
        if self.should_enter_long():
            await self._emit_entry_signal(
                event=event,
                side="buy",
                entry_price=float(event.ask_price),
                sl_price=float(event.ask_price * 0.99),
                tp_price=float(event.ask_price * 1.03),
                reason="my_signal",
            )
```

## Telegram Notifications

Example messages:

### Entry
```
🟢 Entry: BTCUSDT

Direction: LONG
Price: 88143.3
Size: 0.001
Stop Loss: 87262.0
Take Profit: 90786.9
Time: 2026-01-26 19:27:56 UTC
```

### Exit
```
🎯 Exit: BTCUSDT

Reason: Take Profit
Entry: 88143.3
Exit: 90786.9
Size: 0.001

📈 P&L: $26.44
Gross: $26.44
Fees: $0.11
Hold Time: 45.2 min
```

## Testing

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=app

# Run specific test file
pytest tests/test_event_bus.py -v
```

## Safety Features

- API keys never logged or written to files
- Testnet mode by default
- Daily loss limits
- Max drawdown protection
- SL/TP always set on exchange
- State reconciliation on restart

## Testnet vs Mainnet

1. **Testnet** (recommended for testing):
   - Set `bybit.testnet: true` in config
   - Get testnet API keys from https://testnet.bybit.com
   - Free test funds available

2. **Mainnet** (real trading):
   - Set `bybit.testnet: false`
   - Get API keys from https://www.bybit.com
   - Start with small amounts

## License

MIT
