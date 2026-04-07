# Live Trading

## Setup

### 1. Bybit API Keys

Create API keys at [Bybit](https://www.bybit.com/) with trading permissions. Add to `config.yaml`:

```yaml
bybit:
  testnet: false        # true for paper trading
  category: linear
  leverage: 10
  api_key: '<your_key>'
  api_secret: '<your_secret>'
```

### 2. Telegram Alerts (optional)

Create a Telegram bot via [@BotFather](https://t.me/BotFather), get your chat ID:

```yaml
telegram:
  enabled: true
  token: '<bot_token>'
  chat_id: '<chat_id>'
```

### 3. Start Live Trading

```bash
python -m app live
python -m app live --log-level DEBUG
python -m app live -c config_live.yaml
```

## How Live Mode Works

1. **Startup**: connects to Bybit WS, reconciles open positions from exchange
2. **Data**: receives real-time kline, trade, and orderbook events via WebSocket
3. **Strategy**: same `OrderflowStrategy` as backtest processes events
4. **Execution**: `BybitAdapter` places real orders via REST API
5. **SL/TP**: set as conditional orders on Bybit after entry fill
6. **Persistence**: trades saved to SQLite (`live_trades.db`), state survives restarts

## Data Recording

Live mode records all incoming data to CSV files for future backtesting:

```
live_data/
  SOLUSDT/
    kline_1_1m/2026-04-07_12.csv
    publicTrade/2026-04-07_12.csv
    orderbook_1/2026-04-07_12.csv
```

Files rotate hourly. Use these as backtest data by setting `data.base_dir: live_data`.

## State Recovery

On restart, the engine:
1. Reads open positions from Bybit API
2. Reconstructs daily SL count from trade store
3. Resumes strategy with fresh market data
4. **Does not** replay missed events -- assumes SL/TP orders on exchange handle protection

## Web Dashboard

```bash
python -m app ui
# Opens at http://localhost:8000
```

Shows live positions, recent trades, equity curve, and system status.

## Monitoring

- **Logs**: `logs/flotrader_YYYY-MM-DD.log` (rotated daily, kept 30 days)
- **Telegram**: real-time entry/exit/error alerts
- **Dashboard**: web UI for visual monitoring

## Safety Features

| Feature | Description |
|---------|-------------|
| Adaptive DD | Scales down risk as drawdown grows, pauses at hard limit |
| Daily SL limit | Stops trading after N SL exits per day |
| Cost guard | Rejects trades where fees > 30% of SL distance |
| Global SL cooldown | Pauses all trading for N minutes after any SL |
| Concurrent trade limit | Max 1 position at a time (configurable) |
| Breakeven SL | Moves SL to entry after price reaches 1.5x RR |
