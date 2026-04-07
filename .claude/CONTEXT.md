# Must follow
- Work inside existing repo. No new project from scratch.
- Do NOT change strategy entry/exit logic. Only add AI gate: SKIP/HALF/FULL (risk *= 0/0.5/1.0).
- No data leakage: features use only data <= candidate timestamp.
- Validation is walk-forward time split only (no shuffle).
- Make real file edits. Prefer small diffs.

# Data locations
See @.claude/rules/data-paths.md and @.claude/rules/ai-gate.md



# FloTrader Trading System - Complete Architecture & Algorithm Analysis


## Executive Summary

**FloTrader** is a production-ready algorithmic trading framework for Bybit cryptocurrency futures. It implements an **event-driven architecture** supporting both backtesting and live trading from a single codebase. The system is built on an orderflow-based scalping strategy (1-minute timeframe) with sophisticated risk management, state reconciliation, and real-time notifications.

**Key Statistics:**
- 35 Python modules organized in 8 main packages
- Event-driven architecture with priority queue processing
- Supports both historical backtesting and live trading
- Real-time data recording and persistent trade journal

---

## 1. PROJECT STRUCTURE & MAIN COMPONENTS

```
app/
├── __main__.py                 # CLI entry point with 4 commands
├── config.py                   # Pydantic-based configuration
├── core/
│   ├── engine.py              # Main orchestration engine (shared for backtest/live)
│   ├── event_bus.py           # Priority queue-based event system
│   └── events.py              # 12 event type definitions
├── adapters/                  # Pluggable market data & exchange interfaces
│   ├── data_feed.py           # Abstract base for data sources
│   ├── live_data_feed.py      # Bybit WebSocket real-time data
│   ├── historical_data_feed.py # CSV/Parquet backtest data
│   ├── exchange_adapter.py    # Exchange trading interface
│   ├── bybit_adapter.py       # Live Bybit REST + Private WS
│   └── simulated_adapter.py   # Backtest exchange simulation
├── strategies/
│   ├── base.py                # Abstract strategy interface
│   └── orderflow_1m.py        # Orderflow scalping implementation
├── trading/
│   ├── order_manager.py       # Order lifecycle management
│   ├── portfolio.py           # Account state & position tracking
│   ├── risk_manager.py        # Position sizing & risk validation
│   └── signals.py             # Signal type definitions
├── storage/
│   ├── trade_store.py         # SQLite + CSV trade journal
│   ├── data_recorder.py       # Live market data recording
│   └── state_store.py         # (state persistence)
├── notifications/
│   └── telegram.py            # Entry/exit/error alerts
├── reporting/
│   ├── reporter.py            # Backtest report generation
│   └── metrics.py             # Performance metric calculations
└── ui/
    ├── server.py              # FastAPI web calendar UI
    ├── bybit_client.py        # Historical PnL retrieval
    └── models.py              # UI data models
```

**Supporting Files:**
- `config.yaml` - Runtime configuration (symbols, strategy params, risk limits)
- `config.working33.yaml` - Alternative config template
- `pyproject.toml` - Package metadata, dependencies, tool config
- `live_trades.db` - SQLite database for persistent trade storage
- `live_data/` - Market data recordings (OHLC, orderbook, trades)

---

## 2. APPLICATION ENTRY POINTS & STARTUP FLOW

### CLI Commands (via `flotrader` command or `python -m app`)

#### **Command 1: `flotrader live`** - Live Trading Mode
```bash
python -m app live --config config.yaml --log-level INFO
```
**Flow:**
1. Parse config.yaml → Create `Config` object
2. Set mode to `Mode.LIVE`
3. Call `_run_live(config)`
   - Create `EventBus` for pub/sub
   - Initialize `LiveDataFeed` (Bybit WebSocket)
   - Initialize `BybitAdapter` (REST + Private WS)
   - Initialize `OrderflowStrategy`
   - Create `Engine` with shared event bus
   - Start services: Telegram, DataRecorder, TradeStore
   - Run `engine.run_live()`

#### **Command 2: `flotrader backtest`** - Historical Backtesting
```bash
python -m app backtest --config config.yaml --from 2026-01-19 --to 2026-01-29
```
**Flow:**
1. Parse config, override symbols/dates from CLI args
2. Set mode to `Mode.BACKTEST`
3. Call `_run_backtest(config)`
   - Create `EventBus`
   - Initialize `HistoricalDataFeed` (loads CSV files)
   - Initialize `SimulatedExchangeAdapter` (market simulation)
   - Initialize `OrderflowStrategy`
   - Create `Engine`
   - Run `engine.run_backtest()`
   - Generate report via `Reporter`
   - Print summary statistics

#### **Command 3: `flotrader ui`** - Web Calendar Interface
```bash
python -m app ui --host 127.0.0.1 --port 8000
```
- Starts FastAPI server with Jinja2 templates
- Displays trading calendar with daily P&L
- Fetches historical closed PnL from Bybit

#### **Command 4: `flotrader report`** - Regenerate Report (TODO)
- Planned feature to regenerate reports from existing trades.csv files

### Logging Setup
- **Console**: Colored output to stderr with timestamp, level, module, function, line
- **File**: Daily rotation at 00:00 UTC, 30-day retention in `logs/flotrader_YYYY-MM-DD.log`
- Configurable via `--log-level` (INFO, DEBUG, TRACE)

---

## 3. TRADING ALGORITHM - ORDERFLOW SCALPING STRATEGY

### Strategy: `OrderflowStrategy` (1-minute timeframe)

Located: `app/strategies/orderflow_1m.py`

#### **Core Concept**
Trades order imbalances on 1m candles using:
1. **Volume Imbalance**: Buy/sell volume ratio from market trades
2. **Orderbook Delta**: Bid/ask imbalance change over time
3. **Trend Filter**: 15-minute EMA-style trend (comparing closes)
4. **Stop Loss**: ATR-based (1.5x default)
5. **Take Profit**: Risk/Reward ratio (3:1 default)

#### **Entry Logic**

**Trend Determination (15m klines):**
```python
Recent 3 x 15m closes:
  if close[-1] > close[-2] > close[-3]:   → "bullish"
  elif close[-1] < close[-2] < close[-3]: → "bearish"
  else:                                   → "neutral"
```

**Long Signal (BULLISH trend):**
```python
volume_imbalance = (buy_volume - sell_volume) / (buy_volume + sell_volume)
orderbook_delta = recent_imbalance - earlier_imbalance

if volume_imbalance ≥ imbalance_threshold (0.2 by default)
   AND orderbook_delta ≥ delta_threshold (0.1 by default):
   → ENTRY SIGNAL
```

**Short Signal (BEARISH trend):**
```python
if volume_imbalance ≤ -imbalance_threshold
   AND orderbook_delta ≤ -delta_threshold:
   → ENTRY SIGNAL
```

#### **Position Entry Parameters**

**For LONG:**
- Entry price = Ask price (orderbook best offer)
- SL = Entry - (ATR × multiplier)
- TP = Entry + (ATR × multiplier × RR_ratio)

**For SHORT:**
- Entry price = Bid price (orderbook best bid)
- SL = Entry + (ATR × multiplier)
- TP = Entry - (ATR × multiplier × RR_ratio)

#### **ATR Calculation**
```python
True Range = max(
    High - Low,
    |High - PrevClose|,
    |Low - PrevClose|
)
ATR = Simple average of last 14 TR values
```

#### **Exit Logic**

1. **Stop Loss Hit** - Triggered by exchange when price reaches SL
2. **Take Profit Hit** - Triggered by exchange when price reaches TP
3. **Time Exit** - Close if held for 24+ hours (max_hold_seconds = 86400)
4. **Manual Exit** - Strategy emits exit signal based on conditions

#### **Cooldown Mechanism**
After each signal, minimum 60 seconds cooldown before next entry (prevents over-trading).

#### **State per Symbol** (`SymbolState`)
- `trades`: Last 1000 market trades (FIFO deque)
- `buy_volume`, `sell_volume`: Volume tracked per candle
- `klines_1m`, `klines_15m`: Last 60x 1m and 20x 15m candles
- `atr`, `atr_values`: ATR calculation buffer
- `trend`: Current trend state
- `last_signal_time`: Timestamp of last signal (for cooldown)

#### **Event Handlers**

| Event | Handler | Action |
|-------|---------|--------|
| `KlineEvent` (1m) | `on_kline()` | Update ATR, estimate volumes, reset trade flow |
| `KlineEvent` (15m) | `on_kline()` | Update trend |
| `MarketTradeEvent` | `on_trade()` | Accumulate buy/sell volume |
| `OrderBookEvent` | `on_orderbook()` | Calculate delta, check entry/exit conditions |

---

## 4. DATA FLOW - FROM MARKET DATA TO EXECUTION

### **Live Trading Data Flow**

```
Bybit WebSocket (Public)
    ↓
    ├─→ publicTrade (market trades)      → MarketTradeEvent
    ├─→ kline.1 (1m candles)             → KlineEvent
    ├─→ kline.15 (15m candles)           → KlineEvent
    └─→ orderbook.1 (L1 orderbook)       → OrderBookEvent

LiveDataFeed
    ↓ (publishes to EventBus)

EventBus (Priority Queue by timestamp)
    ↓ (pub/sub with async handlers)

Engine._on_kline()
  ├─→ Update current prices
  ├─→ Check new trading day
  ├─→ Update equity curve
  └─→ Forward to strategy

Engine._on_trade() & Engine._on_orderbook()
  ├─→ Update current prices
  └─→ Forward to strategy

Strategy.on_kline/on_trade/on_orderbook()
  ├─→ Analyze market data
  ├─→ Update internal state
  └─→ Emit SignalEvent if conditions met

Engine._on_signal()
  ├─→ Convert SignalEvent to EntrySignal/ExitSignal
  └─→ OrderManager.execute_entry/execute_exit()

OrderManager
  ├─→ RiskManager.validate_signal() & calculate_position_size()
  ├─→ Place order via BybitAdapter
  ├─→ Mark symbol as pending
  └─→ Wait for FillEvent

FillEvent (from BybitAdapter private WS)
  ├─→ Portfolio.open_position() [on entry]
  └─→ Portfolio.close_position() [on exit]

DataRecorder (parallel)
  └─→ Write market data to CSV files
```

### **Backtest Data Flow**

```
CSV Files (pre-loaded in memory)
    ├─→ 1m.csv (OHLCV)
    ├─→ 15m.csv (OHLCV)
    ├─→ trades.csv (tick data)
    └─→ orderbook.csv (L1 snapshots)

HistoricalDataFeed.iter_events()
  ├─→ Detect CSV schema (auto-map columns)
  ├─→ Sort all events by timestamp
  └─→ Yield events in chronological order

Engine.run_backtest()
  ├─→ For each event:
  │   ├─→ Update SimulatedExchangeAdapter.time
  │   ├─→ Check new trading day
  │   └─→ Publish event to EventBus
  │
  ├─→ Process events synchronously
  └─→ Generate report on completion
```

### **Key Data Structures**

**Events (in `app/core/events.py`):**
- `KlineEvent`: OHLCV + interval (1m, 5m, 15m, etc.)
- `MarketTradeEvent`: Price, amount, side
- `OrderBookEvent`: Bid/ask prices and sizes
- `SignalEvent`: Entry/exit signals from strategy
- `OrderUpdateEvent`: Order status changes
- `FillEvent`: Order executions
- `PositionEvent`: Position updates
- `BalanceEvent`: Account balance updates

---

## 5. RISK MANAGEMENT SYSTEM

Located: `app/trading/risk_manager.py`

### **Position Sizing Formula**

```python
# 1. Calculate risk distance (entry to SL)
risk_distance = |entry_price - sl_price|

# 2. Check round-trip costs
cost_pct = 2 × (fees_bps + slippage_bps) / 10000
cost_vs_risk = (cost_pct × entry_price) / risk_distance
if cost_vs_risk > 0.3:  # Costs > 30% of risk? REJECT
    → Position rejected

# 3. Calculate position size based on equity risk
equity = current_balance + unrealized_PnL
risk_amount = equity × (max_position_pct / 100)  # e.g., 2% of equity
position_size = risk_amount / risk_distance

# 4. Apply notional limits
if notional > equity × 10:  # Max 10x leverage
    → Size reduced to max_notional / entry_price
```

### **Risk Validation Checks**

**Pre-Entry Validations:**
1. ✓ Signal has valid SL/TP prices (proper side/direction)
2. ✓ Risk/Reward ratio ≥ 1.0 (minimum requirement)
3. ✓ Number of open positions < `max_concurrent_trades` (default: 3)
4. ✓ No existing position in symbol
5. ✓ Daily loss (negative PnL) < `max_daily_loss_pct` (default: 6%)
6. ✓ Current drawdown < `max_drawdown_pct` (default: 50%)
7. ✓ Cost vs risk acceptable

**Daily Loss Tracking:**
- Reset at new UTC day (midnight UTC)
- Tracked as cumulative PnL for the day
- Blocks new entries once limit hit
- Prevents revenge trading after losses

### **Configuration Parameters** (from `config.yaml`)

```yaml
risk:
  max_position_pct: 2.0              # 2% of equity per trade
  max_daily_loss_pct: 6.0            # Max daily loss
  max_concurrent_trades: 3           # Max open positions
  max_drawdown_pct: 50.0             # Max drawdown from peak
  use_trailing_stop: false           # Dynamic SL adjustment
  trailing_stop_pct: 1.0             # Trailing amount if enabled

costs:
  fees_bps: 10                       # 0.1% trading fees
  slippage_bps: 2                    # 0.02% expected slippage
```

### **State Management**
- `_daily_loss`: Cumulative PnL for current day
- `_current_day`: Current date (UTC)
- `_peak_equity`: High water mark for drawdown calculation

---

## 6. ORDER & EXECUTION MANAGEMENT

Located: `app/trading/order_manager.py`

### **Order Lifecycle**

**Entry Order Flow:**
```
Strategy SignalEvent
  ↓
validate_signal() [Risk Manager]
  ↓
calculate_position_size() [Risk Manager]
  ↓
Portfolio.mark_pending_entry(symbol)
  ↓
create OrderRequest
  - symbol, side, qty, SL, TP
  - client_order_id (unique key)
  ↓
_pending_entries[client_order_id] = PendingEntry
  ↓
exchange.place_order()  [BybitAdapter]
  ↓
FillEvent → _on_fill()
  ├─→ Match by client_order_id
  ├─→ Portfolio.open_position()
  ├─→ Send Telegram notification
  ├─→ Save to TradeStore
  └─→ Remove from _pending_entries
```

**Exit Order Flow:**
```
Strategy ExitSignal OR FillEvent (SL/TP hit)
  ↓
exchange.place_order() with reduce_only=True
  ↓
FillEvent → _on_fill()
  ├─→ Detect exit (check stop_order_type or order_id prefix)
  ├─→ Portfolio.close_position()
  ├─→ RiskManager.update_daily_loss(net_pnl)
  ├─→ Send Telegram notification
  ├─→ Save trade to TradeStore
  └─→ Clear pending exit tracking
```

### **Key Design Patterns**

1. **Client Order ID as Key**: Uses `entry_<symbol>_<uuid>` pattern to uniquely identify orders and match fills, critical for live mode where fills arrive asynchronously

2. **Pending Entry Marking**: Portfolio tracks symbols with pending entries to prevent duplicate entries before fill confirms

3. **Reconciliation on Startup**:
   - Fetches current balance from exchange
   - Loads all open positions
   - Syncs local state with exchange
   - Prevents duplicate orders on restart

### **Exchange Adapters**

**`BybitAdapter` (Live Trading):**
- REST API for order placement/cancellation
- Private WebSocket for real-time fills, position updates, balance changes
- Automatic reconnection on disconnect
- Rate limit handling

**`SimulatedExchangeAdapter` (Backtesting):**
- Market orders fill at best bid/ask + slippage
- Limit orders fill when price crosses level
- Simulates SL/TP execution
- Fee calculation
- Position tracking with mark price

---

## 7. PORTFOLIO & ACCOUNT MANAGEMENT

Located: `app/trading/portfolio.py`

### **Portfolio State**

```python
class Portfolio:
    _initial_balance: Decimal       # Starting capital
    _balance: Decimal               # Available balance (after fees)
    _positions: dict[str, OpenPosition]  # Current open positions
    _pending_symbols: set[str]      # Symbols with pending entries
    _trades: list[Trade]            # Completed trades
    _equity_curve: list[EquityPoint] # Equity over time
    _peak_equity: Decimal           # High water mark
    _daily_pnl: Decimal             # Current day P&L
    _total_fees: Decimal            # Cumulative fees paid
```

### **Key Metrics**

**Calculated On-Demand:**
```python
equity = balance + unrealized_pnl
total_pnl = balance - initial_balance
total_return_pct = (total_pnl / initial_balance) × 100
drawdown = ((peak_equity - equity) / peak_equity) × 100
```

**Per Position:**
```python
unrealized_pnl = {
    BUY:  (current_price - entry_price) × size
    SELL: (entry_price - current_price) × size
}
```

**Per Trade (Completed):**
```python
gross_pnl = (exit_price - entry_price) × size  [for LONG]
net_pnl = gross_pnl - entry_fees - exit_fees - slippage
```

### **Position Tracking**

**`OpenPosition` object:**
- Symbol, side (BUY/SELL), size
- Entry time, entry price, SL, TP
- Entry fees
- Signal metadata (for debugging)

**`Trade` object (completed):**
- Trade ID (T000001, T000002, ...)
- Symbol, side, entry/exit time, entry/exit price, size
- Gross PnL, fees, slippage estimate, net PnL
- Exit reason (sl, tp, signal, time_exit)
- Hold time in seconds

---

## 8. UI COMPONENTS & WEB INTERFACE

Located: `app/ui/` (FastAPI + Jinja2 templates)

### **Calendar UI** (`server.py`)

**Endpoint: `GET /`**
- Displays trading calendar for selected month/year
- Shows daily P&L as color-coded cells
- Green for profit, red for loss
- Historical data fetched from Bybit API

**Features:**
- Month/year navigation
- Per-day statistics (count, wins, losses, P&L)
- Month summaries
- Timezone support (UTC or local)

**Data Flow:**
1. User selects month → API call
2. `BybitPnLClient.get_closed_pnl()` fetches from Bybit
3. Aggregate by entry date
4. Render HTML with CSS styling

### **Bybit PnL Client** (`bybit_client.py`)

Fetches historical closed PnL records from Bybit API for calendar display.

---

## 9. CONFIGURATION SYSTEM

Located: `app/config.py` (Pydantic v2)

### **Configuration Hierarchy**

**Top-level `Config`:**
```yaml
mode: backtest|live
symbols:
  trade: [BTCUSDT, SOLUSDT]    # Trade these
  record: [ETHUSDT]             # Record only
data:
  base_dir: live_data
  format: csv|parquet
  rotation_hours: 1
strategy:
  name: orderflow_1m
  params:
    trend_period: 15
    imbalance_threshold: 0.2
    delta_threshold: 0.1
    rr_ratio: 3.0
    atr_period: 14
    atr_multiplier: 2.5
risk:
  max_position_pct: 2.0
  max_daily_loss_pct: 6.0
  max_concurrent_trades: 3
costs:
  fees_bps: 10
  slippage_bps: 2
telegram:
  enabled: true
  token: ${TELEGRAM_TOKEN}  # From env var
  chat_id: ${TELEGRAM_CHAT_ID}
bybit:
  testnet: false
  api_key: ${BYBIT_API_KEY}    # From env or config
  api_secret: ${BYBIT_API_SECRET}
  leverage: 10
backtest:
  start_date: 2026-01-19
  end_date: 2026-01-29
  initial_capital: 50
```

### **Environment Variable Resolution**

Pattern: `${VAR_NAME}` in config → resolved to `os.environ["VAR_NAME"]`

Applied to:
- `bybit.api_key`, `bybit.api_secret`
- `telegram.token`, `telegram.chat_id`

### **Validation**

- Pydantic `BaseModel` with `field_validator` for custom logic
- Type conversion (string decimals → `Decimal` objects)
- Required fields validation
- Default values for all parameters

---

## 10. EXTERNAL SERVICES & APIs

### **Bybit Exchange Integration**

**API Library:** `pybit >= 5.7.0`

**REST Endpoints Used:**
- `POST /unified/v3/private/order/create` - Place orders
- `POST /unified/v3/private/order/cancel` - Cancel orders
- `GET /unified/v3/private/order/realtime-order-info` - Query orders
- `GET /unified/v3/private/position` - Get positions
- `GET /unified/v3/private/account/wallet-balance` - Get balance
- `GET /v5/account/closed-pnl` - Get closed PnL (for calendar UI)

**WebSocket Channels:**
- **Private:** `execution`, `position`, `wallet` (for order fills and balance updates)
- **Public:** `publicTrade`, `kline.1`, `kline.15`, `orderbook.1`

### **Telegram Bot Integration**

**Library:** `python-telegram-bot >= 20.7`

**Messages Sent:**
- 🚀 Startup notification
- 🟢 Entry: Symbol, side, price, SL, TP
- 🎯 Exit: Reason, P&L, hold time, fees
- ⚠️ Errors: Connection issues, order rejections
- 📊 Daily summary: Win rate, daily P&L

---

## 11. DATABASE & STORAGE MECHANISMS

### **Trade Journal (SQLite + CSV)**

Located: `app/storage/trade_store.py`

**Database Path:** `live_trades.db`

**Schema:**
```sql
trades:
  trade_id (PK)          - T000001, T000002, ...
  symbol, side           - BTCUSDT, LONG
  entry_time, exit_time  - ISO timestamps
  entry_price, exit_price - Decimal strings
  size                   - Position quantity
  gross_pnl, net_pnl     - Profit/loss
  fees, slippage_estimate - Costs
  exit_reason            - sl|tp|signal|time_exit
  hold_time_seconds      - Duration
  metadata               - JSON with signal details

fills:
  timestamp, symbol      - Event details
  order_id, client_order_id
  side, price, qty, fee  - Execution details
  realized_pnl, fee_asset - PnL and fee currency
```

**Exports:**
- `trades.csv` - Portable trade journal
- `fills.csv` - Detailed execution log (for debugging fills)

### **Live Market Data Recording**

Located: `app/storage/data_recorder.py`

**Directory Structure:**
```
live_data/
├── BTCUSDT/
│   ├── 1m.csv       # 1-minute klines
│   ├── 15m.csv      # 15-minute klines
│   ├── trades.csv   # Market trades (ticks)
│   └── orderbook.csv # Order book snapshots
├── SOLUSDT/
│   └── ...
```

**CSV Formats:**

*1m.csv / 15m.csv:*
```csv
timestamp,open,high,low,close,volume
2026-01-26 19:27:00.000000,88114.8,88150.1,88110.5,88137.6,15.893
```

*trades.csv:*
```csv
timestamp,price,amount,side
2026-01-26 19:27:56.252000,88143.3,0.001,buy
```

*orderbook.csv:*
```csv
timestamp,bid_price,bid_size,ask_price,ask_size,mid_price
2026-01-26 19:27:54.895932,88143.2,0.174,88143.3,2.454,88143.25
```

**Writing:**
- Async immediate writes (no buffering for real-time accuracy)
- Event-driven: `record_kline()`, `record_trade()`, `record_orderbook()`
- Lock-based synchronization

---

## 12. REAL-TIME FEATURES & LIVE MODE

Recent commits indicate focus on real-time capabilities:
- `2a40980` - "Realtime added" - WebSocket integration
- `7cc9fbc` - "Add TP and SL" - Stop loss/take profit on exchange
- `70b9dde` - "Fix UI/Open positions" - Position display updates

### **Real-Time Architecture**

**Bybit Private WebSocket (authenticated):**
```python
WebSocket(
    testnet=config.testnet,
    channel_type="private",
    api_key=config.api_key,
    api_secret=config.api_secret
)

# Subscribed channels:
- execution      → FillEvent on order fills
- position       → PositionEvent on position changes
- wallet         → BalanceEvent on balance updates
```

**Reconnection Strategy:**
- Automatic reconnect on disconnect
- Queue events during reconnect
- Reconcile state after reconnect

**Thread Safety:**
- Store asyncio event loop reference for thread-safe callbacks
- Use `call_soon_threadsafe()` when needed

---

## 13. BACKTEST & REPORTING

Located: `app/reporting/`

### **Report Generation**

**Report Directory:** `reports/<timestamp>/`

**Generated Files:**
1. **config.yaml** - Configuration snapshot
2. **trades.csv** - All trades with P&L
3. **equity_curve.csv** - Equity over time (for charting)
4. **metrics.json** - Calculated performance metrics
5. **summary.md** - Human-readable summary

### **Metrics Calculated**

**Win Rate:**
- Overall win rate (% of profitable trades)
- Win rate long
- Win rate short

**Profitability:**
- Gross P&L
- Net P&L (after fees + slippage)
- Total return %
- Max drawdown %

**Efficiency:**
- Profit factor (gross wins / gross losses)
- Expectancy (avg P&L per trade)
- Average win / average loss
- Risk/reward ratio

**Trade Analysis:**
- Consecutive wins/losses streaks
- Average hold time
- Total fees paid
- Impact of slippage
- Trades by exit reason (SL vs TP vs signal)

---

## 14. EVENT-DRIVEN ARCHITECTURE DEEP DIVE

Located: `app/core/event_bus.py`

### **Event Bus Design**

**Priority Queue System:**
```python
class EventBus:
    _queue: list[PrioritizedEvent]  # heap-based priority queue
    _handlers: dict[type, list[EventHandler]]  # type → [async handlers]
    _sequence: int  # Prevent out-of-order events with same timestamp
```

**Processing Modes:**

1. **`publish(event)`** - Add to priority queue
   - Events sorted by timestamp
   - Sequential numbering for same-timestamp stability

2. **`publish_immediate(event)`** - Process immediately
   - Used for real-time fills and signals
   - No queue delay

3. **`process_one()`** - Pop and dispatch next event

4. **`process_until(until_time)`** - Batch process to timestamp

5. **`run()`** - Continuous processing loop (live mode)
   - Processes queue while running
   - Sleeps 1ms when queue empty

### **Handler Subscription**

```python
# Strategy-specific
event_bus.subscribe(KlineEvent, strategy.on_kline)
event_bus.subscribe(MarketTradeEvent, strategy.on_trade)

# Engine-wide
event_bus.subscribe(SignalEvent, engine._on_signal)

# Data recording
event_bus.subscribe(KlineEvent, recorder.record_kline)

# Global handlers
event_bus.subscribe(None, global_handler)  # Receives all events
```

### **Advantages for Backtest vs Live**

- **Backtest:** Processes events in chronological order from queue
- **Live:** Handles async WebSocket updates with predictable ordering
- **Same Code:** Strategy doesn't know which mode it's running in

---

## 15. DATA SCHEMA AUTO-DETECTION

Located: `app/adapters/historical_data_feed.py`

### **CSVSchemaDetector**

Handles flexible CSV formats:
- Case-insensitive column matching
- Common aliases (e.g., "px" → price, "qty" → amount)
- Auto-detects file type (OHLCV, orderbook, trades)

**Detection Logic:**
```python
if ≥4 OHLCV columns found:
    type = "kline"
elif ≥4 orderbook columns found:
    type = "orderbook"
elif ≥2 trade columns found:
    type = "trades"
```

**Benefits:**
- Supports data from multiple sources
- No manual schema configuration needed
- Flexible CSV layouts

---

## 16. KEY ALGORITHMIC DECISIONS

### 1. **Orderflow Over Price Action**
- Uses volume imbalance and orderbook delta
- More responsive than moving averages
- Scalps 1m timeframe (high frequency)

### 2. **Trend Filter**
- 15m trend provides direction bias
- Reduces false signals on choppy markets
- Simple 3-candle close comparison (lightweight)

### 3. **Risk-First Sizing**
- Fixed fractional sizing (% of equity)
- Position size derived from risk distance
- Costs checked before entry to prevent "death by thousand cuts"

### 4. **ATR for Dynamic Stops**
- Adapts to volatility
- Multiplier (1.5-2.5x default) adjustable
- Used for both SL and TP calculation

### 5. **Event-Driven Simulation**
- Backtest uses same code as live
- Historical data treated as event stream
- Removes discrepancies between backtest and live

### 6. **Continuous Cooldown**
- 60-second minimum between signals
- Prevents over-trading and order spam
- Can be disabled if needed

---

## 17. SAFETY & PRODUCTION FEATURES

### **Risk Controls**
✓ Daily loss limits (stops trading after X% daily loss)
✓ Max drawdown limits (disables trading at peak drawdown)
✓ Max concurrent trades (prevents over-leverage)
✓ Costs validation (rejects trades where costs > 30% of risk)
✓ Risk/reward minimum (only trades with RR ≥ 1.0)

### **Operational Safety**
✓ State reconciliation on startup (syncs with exchange)
✓ Pending entry marking (prevents duplicate entries)
✓ SL/TP always set on exchange (not just local tracking)
✓ API keys from environment variables (never logged)
✓ Testnet mode by default

### **Data Integrity**
✓ All trades stored in SQLite + CSV
✓ Fills logged for audit trail
✓ Equity curve recorded for forensics
✓ Trade journal immutable once created
✓ Configuration snapshots in reports

### **Observability**
✓ Structured logging (loguru with timestamps)
✓ Colored console output for debugging
✓ Daily log rotation with 30-day retention
✓ Telegram alerts for critical events
✓ Progress logging during backtest

---

## 18. RECENT DEVELOPMENT (Git History)

```
7cc9fbc - Add TP and SL            [Current]
70b9dde - Fix UI/Open positions
2a40980 - Realtime added           ← WebSocket integration
b68fbe7 - Risk management
b304733 - Risk management
701137f - Risk management
e266b89 - Fix params for ATR etc
376b1ef - Fix issue with new day    ← Daily reset fix
```

**Recent Focus:**
- Stop loss and take profit implementation
- Real-time trading mode (WebSocket)
- Risk management refinements
- UI improvements (open positions display)
- Daily P&L tracking fixes

---

## 19. TECHNICAL STACK

**Language:** Python 3.11+

**Key Libraries:**
- **pybit 5.7.0+** - Bybit REST + WebSocket API
- **pandas 2.1.0+** - Data processing
- **pydantic 2.5.0+** - Configuration validation
- **aiosqlite 0.19.0+** - Async SQLite
- **fastapi 0.109.0+** - Web UI framework
- **python-telegram-bot 20.7+** - Telegram notifications
- **loguru 0.7.2+** - Structured logging

**Dev Tools:**
- pytest + pytest-asyncio - Testing
- ruff - Linting
- black - Formatting
- mypy - Type checking

---

## 20. QUICK REFERENCE - KEY FILES TO MODIFY

**To Add New Strategy:**
- Copy `app/strategies/base.py` structure
- Implement `on_kline()`, `on_trade()`, `on_orderbook()`
- Register in `__main__.py` live/backtest functions
- Create config entry in `app/config.py` `StrategyConfig`

**To Add New Risk Rule:**
- Edit `app/trading/risk_manager.py` `validate_signal()` or `calculate_position_size()`
- Add config parameter to `app/config.py` `RiskConfig`
- Document in `config.yaml`

**To Add New Event Type:**
- Add to `app/core/events.py` as new `@dataclass`
- Update type alias `Event` at bottom
- Subscribe handlers in `app/core/engine.py` `_register_handlers()`

**To Add New Adapter:**
- Inherit from `app/adapters/exchange_adapter.py` or `app/adapters/data_feed.py`
- Implement abstract methods
- Register in `__main__.py` initialization

**To Modify Trading Parameters:**
- Edit `config.yaml` (loaded at runtime)
- Or pass via `--config` CLI flag for different configs

---

## 21. COMMON WORKFLOWS

### **Backtest a Strategy**
```bash
python -m app backtest -c config.yaml --from 2026-01-01 --to 2026-01-31
# Outputs: reports/20260203_HHMMSS/
```

### **Check Live Performance**
```bash
python -m app live -c config.yaml --log-level INFO
# Real-time trading with WebSocket data
# Telegram alerts on entry/exit
```

### **View Trading Calendar**
```bash
python -m app ui --port 8000
# Browse to http://localhost:8000
# See monthly P&L heatmap
```

### **Optimize Parameters**
```python
# Use optimize.py with hyperparameter grid
# Modifies strategy params, runs backtest, measures returns
```

### **Debug Live Session**
```bash
python -m app live -c config.yaml --log-level DEBUG
# Verbose output in console and logs/flotrader_YYYY-MM-DD.log
```

---

## 22. KNOWN LIMITATIONS & TODOs

From code inspection:

1. **UI Features:**
   - `flotrader report` command not implemented (regenerate reports)

2. **Strategy Limitations:**
   - Only 1-minute orderflow strategy implemented
   - Could add trend-following, mean-reversion variants

3. **Execution:**
   - Market orders only (no limit orders in production code)
   - Fixed leverage (not dynamic)

4. **Data:**
   - CSV schema detection works for common formats only
   - Parquet support defined but not fully tested

5. **Risk:**
   - Unrealized P&L calculation incomplete (marked as TODO in portfolio)
   - Trailing stop logic defined but not integrated

---

## CONCLUSION

FloTrader is a **production-ready algo-trading framework** built on solid architectural principles:

✓ **Event-driven** for seamless backtest/live parity
✓ **Risk-first** with multiple safeguards
✓ **Extensible** via plugin strategies and adapters
✓ **Observable** with structured logging and metrics
✓ **Scalable** from single symbol to portfolio

The **orderflow scalping strategy** is sophisticated, combining volume analysis, orderbook imbalance, and trend filtering to trade 1m candles. The **risk management system** is comprehensive, protecting against over-leverage, daily losses, and catastrophic drawdowns.

The codebase is clean, well-documented, and ready for production deployment on Bybit testnet/mainnet with proper configuration.
