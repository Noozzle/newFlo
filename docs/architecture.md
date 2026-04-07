# Architecture

## Overview

FloTrader is an event-driven trading system. The same `Engine` orchestrates both backtest and live modes -- strategies, risk management, and order execution are mode-agnostic.

```
                    +-----------------+
                    |   config.yaml   |
                    +--------+--------+
                             |
                    +--------v--------+
                    |     Engine      |
                    |  (orchestrator) |
                    +--------+--------+
                             |
        +--------------------+--------------------+
        |                    |                    |
+-------v-------+   +-------v-------+   +-------v-------+
|   DataFeed    |   |   Strategy    |   |   Exchange    |
| (historical/  |   | (orderflow)   |   | (simulated/   |
|  live WS)     |   |               |   |  bybit)       |
+-------+-------+   +-------+-------+   +-------+-------+
        |                    |                    |
        +--------+-----------+-----------+--------+
                 |                       |
          +------v------+        +------v------+
          |  EventBus   |        |  Portfolio  |
          | (pub/sub)   |        | (state)     |
          +-------------+        +-------------+
```

## Module Map

```
app/
  __main__.py              CLI entry (click): backtest, live, ui, report
  config.py                Pydantic config model, loads config.yaml

  core/
    engine.py              Main engine -- runs backtest loop or live event loop
    event_bus.py           Pub/sub event bus (sync for backtest, async for live)
    events.py              All event types: Kline, Trade, OrderBook, Signal, Fill...

  strategies/
    base.py                BaseStrategy ABC -- on_kline, on_trade, on_orderbook
    orderflow_1m.py        OrderFlow scalping strategy (1m timeframe)

  trading/
    signals.py             EntrySignal, ExitSignal, ModifySignal, Trade, OpenPosition
    risk_manager.py        Position sizing, cost guard, adaptive drawdown
    order_manager.py       Order lifecycle: entry, exit, modify, SL/TP placement
    portfolio.py           Balance, equity, positions, trades, equity curve
    ai_gate.py             ML gate: SKIP / HALF / FULL risk scaling

  adapters/
    data_feed.py           DataFeed ABC
    historical_data_feed.py  CSV/Parquet loader, k-way merge, OB downsampling
    live_data_feed.py      Bybit WebSocket (kline, trade, orderbook)
    exchange_adapter.py    ExchangeAdapter ABC
    simulated_adapter.py   Backtest fills: market/limit orders, SL/TP triggers
    bybit_adapter.py       Live Bybit REST/WS order execution

  notifications/
    telegram.py            Trade alerts via Telegram bot

  storage/
    trade_store.py         SQLite trade persistence
    state_store.py         State persistence for live restarts
    data_recorder.py       Record live data to CSV for future backtests

  reporting/
    reporter.py            Generate backtest reports (markdown + CSV + JSON)

  ui/
    server.py              FastAPI web dashboard
    models.py              UI data models
    bybit_client.py        REST client for UI

scripts/
  convert_to_parquet.py    CSV -> Parquet conversion
  aggregate_orderbook.py   OB tick aggregation
  check_atr.py             ATR analysis utility
  ai_gate/
    normalize.py           Normalize raw data for ML
    generate_signals.py    Build features & labels from backtest signals
    train.py               Train AI gate model (LightGBM)
    infer.py               Inference utility
    build_mytrades_dataset.py  Build dataset from live trade history
```

## Event Flow (Backtest)

```
HistoricalDataFeed.iter_events()  (k-way merge, time-sorted)
       |
       v
  Engine fast-path dispatch:
       |
       +-- KlineEvent -----> Strategy.on_kline()    -> update ATR, trend
       +-- MarketTradeEvent -> Strategy.on_trade()   -> accumulate volume
       +-- OrderBookEvent --> Strategy.on_orderbook() -> check signal
       |                         |
       |                    SignalEvent (via EventBus)
       |                         |
       |                    Engine._on_signal()
       |                         |
       |                    AI Gate -> SKIP/HALF/FULL
       |                         |
       |                    OrderManager.execute_entry()
       |                         |
       |                    RiskManager.calculate_position_size()
       |                         |
       +-- OrderBookEvent --> SimulatedExchange.process_orderbook()
                                 |
                            Check SL/TP triggers -> FillEvent -> Portfolio
```

## Event Flow (Live)

Same pipeline, but:
- `LiveDataFeed` provides events from Bybit WebSocket
- `BybitAdapter` executes real orders via REST API
- `EventBus.run()` processes events asynchronously
- `TelegramNotifier` sends trade alerts
- `TradeStore` persists trades to SQLite

## Key Design Decisions

1. **Single engine for both modes** -- no divergence between backtest and live behavior
2. **Fast-path dispatch** in backtest -- bypasses EventBus for data events, direct handler calls
3. **Decimal for prices/sizes** -- avoids float precision issues in order sizing
4. **Float for strategy math** -- performance-critical calculations use float
5. **Profiling built-in** -- engine tracks time spent in each component
